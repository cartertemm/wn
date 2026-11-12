import argparse
import io
import json
import os
import plistlib
import signal
import subprocess
import tempfile
import socket
import sys
import time
import unittest
from unittest import mock

from fake_gdb_server import FakeGdbServer
from fake_qmp_server import TCPFakeQMPServer, reply
from wn import agent
from wn.agent import (
	LAUNCHD_LABEL,
	AgentError,
	battery_status,
	install_launch_agent,
	launch_agent_path,
	parse_pmset,
	pid_file_path,
	run_agent_command,
	uninstall_launch_agent,
)
from wn.appbundle import AppBundleError, app_path, executable_path
from wn.battery_file import BatteryFile
from wn.battery_sync import BLOCK_SIZE, BatterySync, decode_block
from wn.cli import main
from wn.config import Config

UPDATED = "2026-11-11T12:00:00Z"
CHARGING_NO_ESTIMATE = (
	"Now drawing from 'AC Power'\n"
	" -InternalBattery-0 (id=22675555)\t96%; charging; (no estimate) present: true\n"
)
CHARGING_WITH_TIME = (
	"Now drawing from 'AC Power'\n"
	" -InternalBattery-0 (id=22675555)\t55%; charging; 1:23 remaining present: true\n"
)
DISCHARGING = (
	"Now drawing from 'Battery Power'\n"
	" -InternalBattery-0 (id=22675555)\t83%; discharging; 4:12 remaining present: true\n"
)
DISCHARGING_NO_ESTIMATE = (
	"Now drawing from 'Battery Power'\n"
	" -InternalBattery-0 (id=22675555)\t83%; discharging; (no estimate) present: true\n"
)
CHARGED = (
	"Now drawing from 'AC Power'\n"
	" -InternalBattery-0 (id=22675555)\t100%; charged; 0:00 remaining present: true\n"
)
NOT_CHARGING = (
	"Now drawing from 'AC Power'\n"
	" -InternalBattery-0 (id=22675555)\t80%; AC attached; not charging present: true\n"
)
FINISHING_CHARGE = (
	"Now drawing from 'AC Power'\n"
	" -InternalBattery-0 (id=22675555)\t98%; finishing charge; 0:05 remaining present: true\n"
)
DESKTOP = "Now drawing from 'AC Power'\n"
DESKTOP_WITH_UPS = (
	"Now drawing from 'AC Power'\n"
	" -Back-UPS ES 550 (id=123)\t100%; charged; 0:00 remaining present: true\n"
)
BLOCK_ADDRESS = 0x50000000
REGION_SIZE = 0x1000
UID = os.getuid()
TARGET = "gui/{}/{}".format(UID, LAUNCHD_LABEL)
LAUNCHCTL_NOT_LOADED = 113
LAUNCHCTL_PRINT_RUNNING = (
	"gui/501/com.wn.agent = {\n"
	"\tactive count = 1\n"
	"\tstate = running\n"
	"\tpid = 4242\n"
	"\tlast exit code = (never exited)\n"
	"}\n"
)
LAUNCHCTL_PRINT_STOPPED = (
	"gui/501/com.wn.agent = {\n"
	"\tactive count = 0\n"
	"\tstate = not running\n"
	"\tlast exit code = 0\n"
	"}\n"
)


def completed(stdout="", returncode=0):
	return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class FakeLaunchctl:
	"""Stands in for subprocess.run. Records each launchctl argv and answers with canned results per verb."""

	def __init__(self, print_result=None, results=None):
		self.calls = []
		self.results = {"print": print_result or completed("", returncode=LAUNCHCTL_NOT_LOADED)}
		self.results.update(results or {})

	def __call__(self, argv, **_):
		if argv[0] == "/usr/bin/pmset":
			return completed(DISCHARGING)
		self.calls.append(argv[1:])
		return self.results.get(argv[1], completed())


def running_qmp_server():
	return TCPFakeQMPServer({"query-status": lambda request: [reply(request, {"running": True, "status": "running"})]})


def unused_port():
	with socket.socket() as probe:
		probe.bind(("127.0.0.1", 0))
		return probe.getsockname()[1]


class ParsePmsetTests(unittest.TestCase):
	def parse(self, text):
		return parse_pmset(text, UPDATED)

	def test_charging_without_estimate(self):
		self.assertEqual(self.parse(CHARGING_NO_ESTIMATE), {
			"present": True,
			"percent": 96,
			"charging": True,
			"on_ac": True,
			"minutes_remaining": None,
			"updated": UPDATED,
		})

	def test_charging_with_time(self):
		status = self.parse(CHARGING_WITH_TIME)
		self.assertEqual((status["percent"], status["charging"], status["minutes_remaining"]), (55, True, 83))

	def test_discharging(self):
		status = self.parse(DISCHARGING)
		self.assertEqual((status["percent"], status["charging"], status["on_ac"], status["minutes_remaining"]), (83, False, False, 252))

	def test_discharging_without_estimate(self):
		self.assertIsNone(self.parse(DISCHARGING_NO_ESTIMATE)["minutes_remaining"])

	def test_charged_has_no_time(self):
		status = self.parse(CHARGED)
		self.assertEqual((status["percent"], status["charging"], status["on_ac"], status["minutes_remaining"]), (100, False, True, None))

	def test_ac_attached_not_charging(self):
		status = self.parse(NOT_CHARGING)
		self.assertEqual((status["percent"], status["charging"], status["on_ac"], status["minutes_remaining"]), (80, False, True, None))

	def test_finishing_charge_counts_as_charging(self):
		status = self.parse(FINISHING_CHARGE)
		self.assertEqual((status["charging"], status["minutes_remaining"]), (True, 5))

	def test_desktop_without_battery(self):
		self.assertEqual(self.parse(DESKTOP), {
			"present": False,
			"percent": None,
			"charging": False,
			"on_ac": True,
			"minutes_remaining": None,
			"updated": UPDATED,
		})

	def test_ups_is_not_the_mac_battery(self):
		self.assertFalse(self.parse(DESKTOP_WITH_UPS)["present"])


class BatteryStatusTests(unittest.TestCase):
	def test_runs_pmset(self):
		run = mock.Mock(return_value=completed(DISCHARGING))
		status = battery_status(run=run)
		run.assert_called_once_with(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True)
		self.assertEqual(status["percent"], 83)
		self.assertTrue(status["updated"].endswith("Z"))

	def test_pmset_failure(self):
		run = mock.Mock(return_value=completed("", returncode=1))
		with self.assertRaises(AgentError):
			battery_status(run=run)


class LaunchAgentTests(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		self.addCleanup(self.home.cleanup)
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.run = mock.Mock(return_value=completed())
		self.path = os.path.join(self.home.name, "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist")

	def test_path(self):
		self.assertEqual(launch_agent_path(), self.path)

	def test_install_writes_plist_and_loads_it(self):
		written = install_launch_agent("/usr/bin/python3", "/repo dir", run=self.run, uid=501)
		self.assertEqual(written, self.path)
		with open(self.path, "rb") as plist_file:
			plist = plistlib.load(plist_file)
		self.assertEqual(plist["Label"], LAUNCHD_LABEL)
		self.assertEqual(plist["ProgramArguments"], [executable_path(), "/usr/bin/python3", "-m", "wn", "agent", "start", "--foreground"])
		self.assertEqual(plist["WorkingDirectory"], "/repo dir")
		self.assertTrue(plist["RunAtLoad"])
		self.assertTrue(plist["KeepAlive"])
		self.assertEqual(plist["StandardErrorPath"], os.path.join(self.home.name, "Library", "Logs", "wn-agent.log"))
		self.assertEqual(self.run.call_args_list[-1], mock.call(["/bin/launchctl", "bootstrap", "gui/501", self.path], capture_output=True, text=True))

	def test_install_builds_the_app_before_loading_it(self):
		install_launch_agent("/usr/bin/python3", "/repo", run=self.run, uid=501)
		commands = [call.args[0][0] for call in self.run.call_args_list]
		self.assertEqual(commands, ["/usr/bin/clang", "/usr/bin/codesign", "/bin/launchctl"])
		self.assertTrue(os.path.isfile(os.path.join(app_path(), "Contents", "Info.plist")))

	def test_install_reports_a_build_failure_without_loading(self):
		self.run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="no developer tools")
		with self.assertRaises(AppBundleError):
			install_launch_agent("/usr/bin/python3", "/repo", run=self.run, uid=501)
		self.assertFalse(os.path.exists(self.path))

	def test_install_escapes_xml(self):
		install_launch_agent("/usr/bin/python3", "/a&b<c>", run=self.run, uid=501)
		with open(self.path, "rb") as plist_file:
			self.assertEqual(plistlib.load(plist_file)["WorkingDirectory"], "/a&b<c>")

	def test_install_replaces_a_loaded_agent(self):
		install_launch_agent("/usr/bin/python3", "/repo", run=self.run, uid=501)
		install_launch_agent("/usr/bin/python3", "/repo", run=self.run, uid=501)
		commands = [call.args[0][1] for call in self.run.call_args_list if call.args[0][0] == "/bin/launchctl"]
		self.assertEqual(commands, ["bootstrap", "bootout", "bootstrap"])

	def test_install_reports_launchctl_failure(self):
		self.run.side_effect = [completed(), completed(), subprocess.CompletedProcess([], 5, stdout="", stderr="Bootstrap failed: 5: Input/output error")]
		with self.assertRaises(AgentError) as caught:
			install_launch_agent("/usr/bin/python3", "/repo", run=self.run, uid=501)
		self.assertIn("Input/output error", str(caught.exception))

	def test_uninstall(self):
		install_launch_agent("/usr/bin/python3", "/repo", run=self.run, uid=501)
		self.run.reset_mock()
		self.assertTrue(uninstall_launch_agent(run=self.run, uid=501))
		self.assertFalse(os.path.exists(self.path))
		self.assertFalse(os.path.exists(app_path()))
		self.run.assert_called_once_with(["/bin/launchctl", "bootout", "gui/501/" + LAUNCHD_LABEL], capture_output=True, text=True)

	def test_uninstall_when_absent(self):
		self.assertFalse(uninstall_launch_agent(run=self.run, uid=501))
		self.run.assert_not_called()


class AgentCommandTests(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		self.addCleanup(self.home.cleanup)
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)

	def run_cli(self, *argv):
		stdout = io.StringIO()
		stderr = io.StringIO()
		exit_code = main(["agent"] + list(argv), stdout=stdout, stderr=stderr)
		return exit_code, stdout.getvalue(), stderr.getvalue()

	def write_config(self, values):
		os.makedirs(os.path.join(self.home.name, ".config", "wn"), exist_ok=True)
		with open(os.path.join(self.home.name, ".config", "wn", "config.json"), "w") as config_file:
			json.dump(values, config_file)

	def test_once_prints_json(self):
		with mock.patch("wn.agent.subprocess.run", return_value=completed(DISCHARGING)), mock.patch("wn.agent.sync_once", return_value="sync result"):
			exit_code, stdout, stderr = self.run_cli("--once")
		self.assertEqual(exit_code, 0)
		self.assertEqual(json.loads(stdout)["percent"], 83)
		self.assertEqual(stderr, "sync result\n")

	def test_once_writes_the_battery_block(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}) as server, running_qmp_server() as qmp:
			server.write(BLOCK_ADDRESS, b"WNBT")
			self.write_config({"battery_channel": "gdb", "gdb_port": server.port, "qmp_socket": qmp.address})
			with mock.patch("wn.agent.subprocess.run", return_value=completed(DISCHARGING)):
				exit_code, stdout, stderr = self.run_cli("--once")
			block = server.read(BLOCK_ADDRESS, BLOCK_SIZE)
		self.assertEqual(exit_code, 0)
		self.assertEqual(json.loads(stdout)["percent"], 83)
		self.assertEqual(stderr, "battery block found at 0x50000000 and written\n")
		self.assertEqual(decode_block(block)["percent"], 83)

	def test_once_reports_a_missing_block(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}) as server, running_qmp_server() as qmp:
			self.write_config({"battery_channel": "gdb", "gdb_port": server.port, "qmp_socket": qmp.address})
			with mock.patch("wn.agent.subprocess.run", return_value=completed(DISCHARGING)):
				exit_code, _, stderr = self.run_cli("--once")
		self.assertEqual(exit_code, 0)
		self.assertEqual(stderr, "no battery block (firmware driver not installed)\n")

	def test_once_reports_an_unreachable_stub(self):
		with running_qmp_server() as qmp:
			self.write_config({"battery_channel": "gdb", "gdb_port": unused_port(), "qmp_socket": qmp.address})
			with mock.patch("wn.agent.subprocess.run", return_value=completed(DISCHARGING)):
				exit_code, stdout, stderr = self.run_cli("--once")
		self.assertEqual(exit_code, 0)
		self.assertEqual(json.loads(stdout)["percent"], 83)
		self.assertIn("battery sync: cannot connect to the GDB stub", stderr)

	def test_once_skips_sync_when_qmp_is_unreachable(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}) as server:
			server.write(BLOCK_ADDRESS, b"WNBT")
			self.write_config({"battery_channel": "gdb", "gdb_port": server.port, "qmp_socket": "tcp:127.0.0.1:{}".format(unused_port())})
			with mock.patch("wn.agent.subprocess.run", return_value=completed(DISCHARGING)):
				exit_code, _, stderr = self.run_cli("--once")
		self.assertEqual(exit_code, 0)
		self.assertIn("battery sync: cannot check the VM state over QMP", stderr)
		self.assertEqual(server.connections, 0)

	def test_once_writes_the_battery_file(self):
		path = os.path.join(self.home.name, "wn-battery.bin")
		self.write_config({"battery_file": path, "qmp_socket": "tcp:127.0.0.1:{}".format(unused_port())})
		with mock.patch("wn.agent.subprocess.run", return_value=completed(DISCHARGING)):
			exit_code, _, stderr = self.run_cli("--once")
		self.assertEqual(exit_code, 0)
		self.assertEqual(stderr, "battery file {} written\n".format(path))
		with open(path, "rb") as data_file:
			self.assertEqual(decode_block(data_file.read(BLOCK_SIZE))["percent"], 83)

	def test_once_skips_sync_when_turned_off(self):
		self.write_config({"battery_sync": False})
		with mock.patch("wn.agent.subprocess.run", return_value=completed(DISCHARGING)), mock.patch("wn.agent.sync_once") as sync:
			_, _, stderr = self.run_cli("--once")
		sync.assert_not_called()
		self.assertEqual(stderr, "")

	def test_install_uses_this_python_and_repo(self):
		with mock.patch("wn.agent.subprocess.run", return_value=completed()) as run:
			exit_code, stdout, _ = self.run_cli("--install")
		self.assertEqual(exit_code, 0)
		with open(launch_agent_path(), "rb") as plist_file:
			plist = plistlib.load(plist_file)
		self.assertEqual(plist["WorkingDirectory"], os.path.dirname(os.path.dirname(os.path.abspath(agent.__file__))))
		self.assertEqual(plist["ProgramArguments"][1], sys.executable)
		self.assertEqual(run.call_args.args[0][:2], ["/bin/launchctl", "bootstrap"])
		self.assertIn(launch_agent_path(), stdout)

	def test_uninstall(self):
		with mock.patch("wn.agent.subprocess.run", return_value=completed()):
			exit_code, stdout, _ = self.run_cli("--uninstall")
		self.assertEqual(exit_code, 0)
		self.assertIn("not installed", stdout)

	def test_options_are_exclusive(self):
		with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
			self.run_cli("--once", "--install")

	def test_no_action_prints_usage_and_exits_2(self):
		with mock.patch("sys.stderr", io.StringIO()) as stderr, self.assertRaises(SystemExit) as caught:
			self.run_cli()
		self.assertEqual(caught.exception.code, 2)
		self.assertIn("usage: wn agent", stderr.getvalue())

	def test_flag_and_action_together_exit_2(self):
		with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
			self.run_cli("--once", "status")
		self.assertEqual(caught.exception.code, 2)

	def test_foreground_flag_is_hidden(self):
		with mock.patch("sys.stdout", io.StringIO()) as stdout, self.assertRaises(SystemExit):
			self.run_cli("start", "--help")
		self.assertNotIn("--foreground", stdout.getvalue())

	def test_runs_the_sync_loop_with_the_battery_file(self):
		with mock.patch("wn.agent.run_sync_loop") as loop:
			exit_code, _, _ = self.run_cli("start")
		self.assertEqual(exit_code, 0)
		writer, read_status = loop.call_args.args[:2]
		self.assertIsInstance(writer, BatteryFile)
		self.assertEqual(writer.path, os.path.join(self.home.name, "Library", "Group Containers", "WDNLXAD4W8.com.utmapp.UTM", "wn-battery-Windows.bin"))
		self.assertIs(read_status, battery_status)

	def test_uses_the_gdb_channel_from_config(self):
		self.write_config({"battery_channel": "gdb", "gdb_port": 4500, "qmp_port": 4600})
		with mock.patch("wn.agent.run_sync_loop") as loop:
			self.run_cli("start")
		writer = loop.call_args.args[0]
		self.assertIsInstance(writer, BatterySync)
		self.assertEqual((writer.port, writer.qmp_address), (4500, "tcp:127.0.0.1:4600"))

	def test_uses_the_configured_battery_file(self):
		self.write_config({"battery_file": "/tmp/wn-battery.bin"})
		with mock.patch("wn.agent.run_sync_loop") as loop:
			self.run_cli("start")
		self.assertEqual(loop.call_args.args[0].path, "/tmp/wn-battery.bin")

	def test_fails_without_battery_sync(self):
		self.write_config({"battery_sync": False})
		with mock.patch("wn.agent.run_sync_loop") as loop:
			exit_code, _, stderr = self.run_cli("start")
		self.assertEqual(exit_code, 1)
		self.assertIn("battery_sync", stderr)
		loop.assert_not_called()

	def test_ctrl_c_stops_the_agent(self):
		with mock.patch("wn.agent.run_sync_loop", side_effect=KeyboardInterrupt):
			exit_code, _, _ = self.run_cli("start")
		self.assertEqual(exit_code, 0)


class AgentControlTests(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		self.addCleanup(self.home.cleanup)
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.plist = os.path.join(self.home.name, "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist")

	def run_cli(self, launchctl, *argv):
		stdout = io.StringIO()
		stderr = io.StringIO()
		with mock.patch("wn.agent.subprocess.run", launchctl):
			exit_code = main(["agent"] + list(argv), stdout=stdout, stderr=stderr)
		return exit_code, stdout.getvalue(), stderr.getvalue()

	def write_config(self, values):
		os.makedirs(os.path.join(self.home.name, ".config", "wn"), exist_ok=True)
		with open(os.path.join(self.home.name, ".config", "wn", "config.json"), "w") as config_file:
			json.dump(values, config_file)

	def install(self):
		os.makedirs(os.path.dirname(self.plist), exist_ok=True)
		with open(self.plist, "w") as plist_file:
			plist_file.write("<plist/>")

	def write_pid_file(self, pid):
		os.makedirs(os.path.dirname(pid_file_path()), exist_ok=True)
		with open(pid_file_path(), "w") as pid_file:
			pid_file.write("{}\n".format(pid))

	def spawn_sleeper(self):
		process = subprocess.Popen(["/bin/sleep", "30"])
		self.addCleanup(process.wait)
		self.addCleanup(process.kill)
		return process

	def test_pid_file_path(self):
		self.assertEqual(pid_file_path(), os.path.join(self.home.name, "Library", "Application Support", "wn", "agent.pid"))

	def test_start_kickstarts_a_loaded_job(self):
		self.install()
		launchctl = FakeLaunchctl(completed(LAUNCHCTL_PRINT_STOPPED))
		with mock.patch("wn.agent.run_sync_loop") as loop:
			exit_code, stdout, _ = self.run_cli(launchctl, "start")
		self.assertEqual(exit_code, 0)
		self.assertEqual(launchctl.calls, [["print", TARGET], ["kickstart", TARGET]])
		self.assertIn("launchctl kickstart", stdout)
		loop.assert_not_called()

	def test_start_bootstraps_an_unloaded_job(self):
		self.install()
		launchctl = FakeLaunchctl()
		with mock.patch("wn.agent.run_sync_loop") as loop:
			exit_code, stdout, _ = self.run_cli(launchctl, "start")
		self.assertEqual(exit_code, 0)
		self.assertEqual(launchctl.calls, [["print", TARGET], ["bootstrap", "gui/{}".format(UID), self.plist]])
		self.assertIn("launchctl bootstrap", stdout)
		loop.assert_not_called()

	def test_start_leaves_a_running_job_alone(self):
		self.install()
		launchctl = FakeLaunchctl(completed(LAUNCHCTL_PRINT_RUNNING))
		exit_code, stdout, _ = self.run_cli(launchctl, "start")
		self.assertEqual(exit_code, 0)
		self.assertEqual(launchctl.calls, [["print", TARGET]])
		self.assertIn("already running with PID 4242", stdout)

	def test_start_reports_a_launchctl_failure(self):
		self.install()
		launchctl = FakeLaunchctl(completed(LAUNCHCTL_PRINT_STOPPED), {"kickstart": subprocess.CompletedProcess([], 5, stdout="", stderr="Could not kickstart")})
		exit_code, _, stderr = self.run_cli(launchctl, "start")
		self.assertEqual(exit_code, 1)
		self.assertIn("launchctl kickstart failed with exit code 5: Could not kickstart", stderr)

	def test_start_runs_in_the_foreground_when_not_installed(self):
		launchctl = FakeLaunchctl()
		seen = []
		def loop_body(*_):
			with open(pid_file_path()) as pid_file:
				seen.append(pid_file.read())
		with mock.patch("wn.agent.run_sync_loop", side_effect=loop_body) as loop:
			exit_code, stdout, _ = self.run_cli(launchctl, "start")
		self.assertEqual(exit_code, 0)
		loop.assert_called_once()
		self.assertEqual(launchctl.calls, [])
		self.assertEqual(seen, ["{}\n".format(os.getpid())])
		self.assertFalse(os.path.exists(pid_file_path()))
		self.assertIn("foreground", stdout)

	def test_foreground_flag_skips_launchd(self):
		self.install()
		launchctl = FakeLaunchctl(completed(LAUNCHCTL_PRINT_STOPPED))
		with mock.patch("wn.agent.run_sync_loop") as loop:
			exit_code, _, _ = self.run_cli(launchctl, "start", "--foreground")
		self.assertEqual(exit_code, 0)
		loop.assert_called_once()
		self.assertEqual(launchctl.calls, [])

	def test_sigterm_stops_the_foreground_agent_and_removes_the_pid_file(self):
		def loop(*_):
			os.kill(os.getpid(), signal.SIGTERM)
			time.sleep(1)
		previous = signal.getsignal(signal.SIGTERM)
		with mock.patch("wn.agent.run_sync_loop", side_effect=loop):
			exit_code, _, _ = self.run_cli(FakeLaunchctl(), "start")
		self.assertEqual(exit_code, 0)
		self.assertFalse(os.path.exists(pid_file_path()))
		self.assertIs(signal.getsignal(signal.SIGTERM), previous)

	def test_start_refuses_a_second_foreground_agent(self):
		self.write_pid_file(os.getpid())
		with mock.patch("wn.agent.run_sync_loop") as loop:
			exit_code, _, stderr = self.run_cli(FakeLaunchctl(), "start")
		self.assertEqual(exit_code, 1)
		self.assertIn("already running with PID {}".format(os.getpid()), stderr)
		loop.assert_not_called()

	def test_start_ignores_a_stale_pid_file(self):
		process = self.spawn_sleeper()
		process.kill()
		process.wait()
		self.write_pid_file(process.pid)
		with mock.patch("wn.agent.run_sync_loop") as loop:
			exit_code, _, _ = self.run_cli(FakeLaunchctl(), "start")
		self.assertEqual(exit_code, 0)
		loop.assert_called_once()

	def test_stop_boots_out_a_loaded_job(self):
		self.install()
		launchctl = FakeLaunchctl(completed(LAUNCHCTL_PRINT_RUNNING))
		exit_code, stdout, _ = self.run_cli(launchctl, "stop")
		self.assertEqual(exit_code, 0)
		self.assertEqual(launchctl.calls, [["print", TARGET], ["bootout", TARGET]])
		self.assertIn("launchctl bootout", stdout)
		self.assertTrue(os.path.exists(self.plist))

	def test_stop_sends_sigterm_to_the_foreground_agent(self):
		process = self.spawn_sleeper()
		self.write_pid_file(process.pid)
		exit_code, stdout, _ = self.run_cli(FakeLaunchctl(), "stop")
		self.assertEqual(exit_code, 0)
		self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
		self.assertIn("SIGTERM", stdout)
		self.assertIn(str(process.pid), stdout)

	def test_stop_when_installed_but_not_loaded_checks_the_pid_file(self):
		self.install()
		process = self.spawn_sleeper()
		self.write_pid_file(process.pid)
		launchctl = FakeLaunchctl()
		self.run_cli(launchctl, "stop")
		self.assertEqual(launchctl.calls, [["print", TARGET]])
		self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)

	def test_stop_when_nothing_runs(self):
		launchctl = FakeLaunchctl()
		exit_code, stdout, _ = self.run_cli(launchctl, "stop")
		self.assertEqual(exit_code, 0)
		self.assertEqual(stdout, "not running\n")
		self.assertEqual(launchctl.calls, [])

	def test_status_of_a_running_launchd_job(self):
		self.install()
		battery_path = os.path.join(self.home.name, "wn-battery.bin")
		BatteryFile(battery_path, clock=lambda: 1794398400).write(agent.battery_reading(parse_pmset(DISCHARGING, UPDATED)))
		with running_qmp_server() as qmp:
			self.write_config({"battery_file": battery_path, "qmp_socket": qmp.address})
			launchctl = FakeLaunchctl(completed(LAUNCHCTL_PRINT_RUNNING))
			exit_code, stdout, _ = self.run_cli(launchctl, "status")
		self.assertEqual(exit_code, 0)
		self.assertEqual(launchctl.calls, [["print", TARGET]])
		self.assertIn("Installed: yes ({})".format(self.plist), stdout)
		self.assertIn("Running: yes, PID 4242", stdout)
		self.assertIn("QMP: {} (reachable, VM running)".format(qmp.address), stdout)
		self.assertIn("Battery file: {} (updated 2026-11-11T12:00:00Z)".format(battery_path), stdout)

	def test_status_when_nothing_runs(self):
		address = "tcp:127.0.0.1:{}".format(unused_port())
		battery_path = os.path.join(self.home.name, "missing.bin")
		self.write_config({"battery_file": battery_path, "qmp_socket": address})
		exit_code, stdout, _ = self.run_cli(FakeLaunchctl(), "status")
		self.assertEqual(exit_code, 1)
		self.assertIn("Installed: no", stdout)
		self.assertIn("Running: no", stdout)
		self.assertIn("QMP: {} (not reachable)".format(address), stdout)
		self.assertIn("Battery file: {} (missing)".format(battery_path), stdout)

	def test_status_reads_the_pid_file(self):
		self.write_pid_file(os.getpid())
		self.write_config({"qmp_socket": "tcp:127.0.0.1:{}".format(unused_port())})
		exit_code, stdout, _ = self.run_cli(FakeLaunchctl(), "status")
		self.assertEqual(exit_code, 0)
		self.assertIn("Running: yes, PID {}".format(os.getpid()), stdout)

	def test_status_of_an_empty_battery_file(self):
		battery_path = os.path.join(self.home.name, "empty.bin")
		open(battery_path, "wb").close()
		self.write_config({"battery_file": battery_path, "qmp_socket": "tcp:127.0.0.1:{}".format(unused_port())})
		_, stdout, _ = self.run_cli(FakeLaunchctl(), "status")
		self.assertIn("Battery file: {} (no battery block yet)".format(battery_path), stdout)

	def test_status_shows_the_gdb_stub_for_the_gdb_channel(self):
		self.write_config({"battery_channel": "gdb", "gdb_port": 4500, "qmp_socket": "tcp:127.0.0.1:{}".format(unused_port())})
		_, stdout, _ = self.run_cli(FakeLaunchctl(), "status")
		self.assertIn("Battery channel: gdb (127.0.0.1:4500)", stdout)


class AgentLoopTests(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		self.addCleanup(self.home.cleanup)
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)

	def run_agent(self, config, wait):
		output = io.StringIO()
		arguments = argparse.Namespace(once=False, install=False, uninstall=False, agent_command="start", foreground=True)
		with mock.patch("wn.agent.subprocess.run", return_value=completed(DISCHARGING)):
			run_agent_command(arguments, config, output, wait=wait)
		return output.getvalue()

	def test_loop_writes_the_block(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}) as server, running_qmp_server() as qmp:
			server.write(BLOCK_ADDRESS, b"WNBT")
			self.run_agent(Config(battery_channel="gdb", gdb_port=server.port, qmp_socket=qmp.address), lambda seconds: True)
			self.assertEqual(decode_block(server.read(BLOCK_ADDRESS, BLOCK_SIZE))["percent"], 83)
		self.assertFalse(server.paused)

	def test_loop_keeps_going_when_the_stub_is_down(self):
		waits = []
		def wait(seconds):
			waits.append(seconds)
			return len(waits) > 1
		with running_qmp_server() as qmp:
			output = self.run_agent(Config(battery_channel="gdb", gdb_port=unused_port(), qmp_socket=qmp.address), wait)
		self.assertIn("battery sync: cannot connect to the GDB stub", output)
		self.assertEqual(len(waits), 2)


if __name__ == "__main__":
	unittest.main()
