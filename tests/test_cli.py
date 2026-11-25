import io
import json
import os
import plistlib
import stat
import subprocess
import sys
import unittest
from unittest import mock

from fake_keygrab_helper import read_log, write_fake_helper
from fake_qmp_server import FakeQMPServer, error_reply, reply
from fakes import FakeBackend, RecordingQMPClient, share_item, short_temporary_directory, write_config
from wn.cli import main
from wn.errors import WnError
from wn.keys import combo_to_input_events
from wn.qmp import QMPConnectionError
from wn.shares import FIRST_PORT
from wn.utm_config import UTM_GROUP_CONTAINER, default_battery_file, qmp_socket_file


class TerminalInput(io.StringIO):
	def isatty(self):
		return True


class CLITestCase(unittest.TestCase):
	def setUp(self):
		self.home = short_temporary_directory()
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(self.home.cleanup)
		self.backend = FakeBackend()
		self.qmp_log = []
		sleep_patcher = mock.patch("wn.keys.time.sleep")
		sleep_patcher.start()
		self.addCleanup(sleep_patcher.stop)

	def run_real_cli(self, *argv, stdin=None):
		stdout = io.StringIO()
		stderr = io.StringIO()
		exit_code = main(list(argv), stdout=stdout, stderr=stderr, stdin=stdin)
		return exit_code, stdout.getvalue(), stderr.getvalue()

	def run_cli(self, *argv, stdin=None):
		stdout = io.StringIO()
		stderr = io.StringIO()
		exit_code = main(
			list(argv),
			backend_factory=lambda config: self.backend,
			qmp_client_factory=RecordingQMPClient(self.qmp_log),
			stdout=stdout,
			stderr=stderr,
			stdin=stdin,
		)
		return exit_code, stdout.getvalue(), stderr.getvalue()


class LifecycleCommandTests(CLITestCase):
	def test_start(self):
		self.assertEqual(self.run_cli("start")[0], 0)
		self.assertEqual(self.backend.calls, [("start",)])

	def test_stop(self):
		self.run_cli("stop")
		self.assertEqual(self.backend.calls, [("qmp_socket_path", False), ("stop", False), ("status",)])

	def test_stop_presses_shift_first_so_the_display_is_on(self):
		with mock.patch("wn.cli.time.sleep") as sleep:
			exit_code, _, stderr = self.run_cli("stop")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.qmp_log, [("connect", "/fake/qmp.sock")] + [("input-send-event", arguments) for arguments in combo_to_input_events("shift")] + [("close",)])
		sleep.assert_any_call(2)

	def test_stop_sends_the_power_button_when_qmp_is_unreachable(self):
		def unreachable(address):
			raise QMPConnectionError("cannot connect to QMP socket {}".format(address))
		stderr = io.StringIO()
		exit_code = main(["stop"], backend_factory=lambda config: self.backend, qmp_client_factory=unreachable, stdout=io.StringIO(), stderr=stderr)
		self.assertEqual(exit_code, 0)
		self.assertIn("wn: warning: cannot wake the display", stderr.getvalue())
		self.assertEqual(self.backend.calls, [("qmp_socket_path", False), ("stop", False), ("status",)])

	def test_stop_no_wait(self):
		exit_code, stdout, _ = self.run_cli("stop", "--no-wait")
		self.assertEqual(exit_code, 0)
		self.assertEqual(stdout, "Windows: shutdown requested\n")
		self.assertEqual(self.backend.calls, [("qmp_socket_path", False), ("stop", False)])

	def test_stop_force(self):
		self.run_cli("stop", "--force", "--yes")
		self.assertEqual(self.backend.calls, [("stop", True)])
		self.assertEqual(self.qmp_log, [])

	def test_stop_force_asks_first(self):
		for answer in ("y\n", "yes\n", "YES\n"):
			with self.subTest(answer=answer):
				self.backend.calls = []
				exit_code, stdout, _ = self.run_cli("stop", "--force", stdin=TerminalInput(answer))
				self.assertEqual(exit_code, 0)
				self.assertEqual(stdout, "Power off Windows without shutting it down? Unsaved work is lost. [y/N]: ")
				self.assertEqual(self.backend.calls, [("stop", True)])

	def test_stop_force_declined(self):
		for answer in ("n\n", "\n", "maybe\n"):
			with self.subTest(answer=answer):
				exit_code, stdout, _ = self.run_cli("stop", "--force", stdin=TerminalInput(answer))
				self.assertEqual(exit_code, 1)
				self.assertEqual(stdout, "Power off Windows without shutting it down? Unsaved work is lost. [y/N]: Windows: not powered off\n")
				self.assertEqual(self.backend.calls, [])

	def test_stop_force_without_terminal_needs_yes(self):
		exit_code, stdout, stderr = self.run_cli("stop", "--force", stdin=io.StringIO("y\n"))
		self.assertEqual(exit_code, 1)
		self.assertEqual(stdout, "")
		self.assertIn("Add --yes", stderr)
		self.assertEqual(self.backend.calls, [])

	def test_stop_force_yes_does_not_ask(self):
		exit_code, stdout, _ = self.run_cli("stop", "--force", "--yes", stdin=TerminalInput(""))
		self.assertEqual(exit_code, 0)
		self.assertEqual(stdout, "")
		self.assertEqual(self.backend.calls, [("stop", True)])

	def test_status(self):
		exit_code, stdout, _ = self.run_cli("status")
		self.assertEqual(exit_code, 0)
		self.assertEqual(stdout, "Windows: running\n")

	def test_ip(self):
		self.assertEqual(self.run_cli("ip")[1], "192.168.64.5\n")

	def test_ip_unknown(self):
		self.backend.ip_address = None
		exit_code, _, stderr = self.run_cli("ip")
		self.assertEqual(exit_code, 1)
		self.assertIn("IP address", stderr)

	def test_unimplemented_commands_are_hidden(self):
		with mock.patch("sys.stdout", io.StringIO()) as help_output, self.assertRaises(SystemExit):
			self.run_cli("--help")
		for command in ("net",):
			self.assertNotRegex(help_output.getvalue(), r"\b{}\b".format(command))
			with mock.patch("sys.stderr", io.StringIO()) as error_output, self.assertRaises(SystemExit) as context:
				self.run_cli(command)
			self.assertEqual(context.exception.code, 2)
			self.assertIn("invalid choice", error_output.getvalue())

	def test_missing_subcommand_is_usage_error(self):
		with mock.patch("sys.stderr", io.StringIO()):
			with self.assertRaises(SystemExit) as context:
				self.run_cli()
		self.assertEqual(context.exception.code, 2)


class ShareCommandTests(CLITestCase):
	def help_text(self, *argv):
		with mock.patch("sys.stdout", io.StringIO()) as help_output, self.assertRaises(SystemExit):
			self.run_cli(*argv, "--help")
		return help_output.getvalue()

	def share_commands(self):
		patcher = mock.patch("wn.cli.ShareCommands")
		share_commands = patcher.start()
		self.addCleanup(patcher.stop)
		return share_commands.return_value

	def test_share_commands_are_listed_in_help(self):
		help_text = self.help_text()
		for command in ("share", "shares", "unshare"):
			self.assertRegex(help_text, r"\b{}\b".format(command))

	def test_share_help_shows_the_paths_the_options_and_the_limits(self):
		help_text = self.help_text("share")
		for expected in (
			"dir-on-mac",
			"dir-on-vm",
			"wn share ~/Projects 'C:\\Users\\you\\Projects'",
			"single quotes",
			"Forward slashes also work, such as C:/Users/you/Projects",
			"The folder that holds it must exist in Windows",
			"--read-only",
			"--name",
			"about 5 s after a file is closed",
			"Windows apps that watch folders do not see Mac changes",
			"are not kept",
			"wn shares",
			"wn unshare",
		):
			self.assertIn(expected, help_text)

	def test_shares_help_is_short(self):
		help_text = self.help_text("shares")
		self.assertIn("List the shared folders and their state", help_text)
		self.assertLess(len(help_text.splitlines()), 10)

	def test_unshare_help_names_the_share_argument(self):
		help_text = self.help_text("unshare")
		self.assertIn("usage: wn unshare [-h] [--discard] share", help_text)
		self.assertIn("as wn shares shows it", help_text)
		self.assertIn("The files stay on the Mac", help_text)

	def test_share_calls_add_share(self):
		share_commands = self.share_commands()
		share_commands.add_share.return_value = None
		exit_code, _, stderr = self.run_cli("share", "/a", "C:\\x", "--read-only", "--name", "X")
		self.assertEqual(exit_code, 0, stderr)
		share_commands.add_share.assert_called_once_with("/a", "C:\\x", True, "X")

	def test_share_passes_windows_paths_intact(self):
		share_commands = self.share_commands()
		share_commands.add_share.return_value = None
		for vm_dir in ("C:\\Users\\you\\My Mac Files", "C:\\Users\\you\\Bob's Mac", "D:\\-x\\--name"):
			share_commands.add_share.reset_mock()
			exit_code, _, stderr = self.run_cli("share", "/Users/you/My Files", vm_dir)
			self.assertEqual(exit_code, 0, stderr)
			share_commands.add_share.assert_called_once_with("/Users/you/My Files", vm_dir, False, None)

	def test_shares_calls_list_shares(self):
		share_commands = self.share_commands()
		share_commands.list_shares.return_value = 0
		self.assertEqual(self.run_cli("shares")[0], 0)
		share_commands.list_shares.assert_called_once_with()

	def test_shares_with_no_shares_says_so(self):
		exit_code, stdout, stderr = self.run_cli("shares")
		self.assertEqual(exit_code, 0, stderr)
		self.assertIn("No shares", stdout)

	def test_unshare_calls_remove_share(self):
		share_commands = self.share_commands()
		share_commands.remove_share.return_value = None
		self.assertEqual(self.run_cli("unshare", "Projects")[0], 0)
		share_commands.remove_share.assert_called_once_with("Projects", False)
		share_commands.remove_share.reset_mock()
		self.assertEqual(self.run_cli("unshare", "Old", "--discard")[0], 0)
		share_commands.remove_share.assert_called_once_with("Old", True)

	def test_unshare_passes_on_its_exit_code(self):
		self.share_commands().remove_share.return_value = 1
		self.assertEqual(self.run_cli("unshare", "Projects")[0], 1)

	def test_ctrl_c_during_unshare_while_the_share_is_in_the_config(self):
		write_config(self.home.name, {"shares": [share_item()]})
		self.share_commands().remove_share.side_effect = KeyboardInterrupt
		exit_code, _, stderr = self.run_cli("unshare", "projects")
		self.assertEqual(exit_code, 1)
		self.assertEqual(stderr, "\nwn: stopped. projects is still shared on the Mac, and Windows may still remove it after its uploads. Run wn unshare projects again to finish\n")

	def test_ctrl_c_during_unshare_after_the_mac_side_is_removed(self):
		self.share_commands().remove_share.side_effect = KeyboardInterrupt
		exit_code, _, stderr = self.run_cli("unshare", "Projects")
		self.assertEqual(exit_code, 1)
		self.assertEqual(stderr, "\nwn: stopped. Projects is removed from the Mac, but wn may not have finished removing its files from Windows\n")

	def test_ctrl_c_during_share_prints_one_line(self):
		self.share_commands().add_share.side_effect = KeyboardInterrupt
		exit_code, stdout, stderr = self.run_cli("share", "/a", "C:\\x")
		self.assertEqual(exit_code, 1)
		self.assertEqual(stderr, "\nwn: stopped. Run wn shares to see whether the share exists\n")

	def test_ctrl_c_during_shares_prints_one_line(self):
		self.share_commands().list_shares.side_effect = KeyboardInterrupt
		exit_code, _, stderr = self.run_cli("shares")
		self.assertEqual(exit_code, 1)
		self.assertEqual(stderr, "\nwn: stopped\n")

	def test_os_error_in_a_share_command_is_printed(self):
		share_commands = self.share_commands()
		share_commands.add_share.side_effect = OSError(48, "Address already in use")
		share_commands.list_shares.side_effect = PermissionError(13, "Permission denied")
		share_commands.remove_share.side_effect = FileNotFoundError(2, "No such file or directory")
		for argv, message in (
			(("share", "/a", "C:\\x"), "wn: error: [Errno 48] Address already in use\n"),
			(("shares",), "wn: error: [Errno 13] Permission denied\n"),
			(("unshare", "Projects"), "wn: error: [Errno 2] No such file or directory\n"),
		):
			exit_code, _, stderr = self.run_cli(*argv)
			self.assertEqual(exit_code, 1)
			self.assertEqual(stderr, message)

	def test_share_error_is_printed(self):
		self.share_commands().add_share.side_effect = WnError("boom")
		exit_code, _, stderr = self.run_cli("share", "/a", "C:\\x")
		self.assertEqual(exit_code, 1)
		self.assertIn("wn: error: boom", stderr)

	def test_agent_start_without_a_launchd_job_skips_a_bad_share(self):
		good = share_item()
		write_config(self.home.name, {"shares": [good, dict(good, name="Bad", mac_path="/b", vm_path="C:\\y", port=1)]})
		with mock.patch("wn.agent.serve") as serve:
			exit_code, _, stderr = self.run_cli("agent", "start")
		self.assertEqual((exit_code, stderr), (0, ""))
		self.assertEqual([share.name for share in serve.call_args[0][0].shares], ["Projects"])


FAKE_UTMCTL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_utmctl.py")


class StopCommandTests(CLITestCase):
	"""``wn stop`` against the fake utmctl, with a fake clock so the waits take no time."""

	def setUp(self):
		super().setUp()
		self.state_path = os.path.join(self.home.name, "utmctl.json")
		self.write_vm(status="started")
		patcher = mock.patch.dict(os.environ, {"FAKE_UTMCTL_STATE": self.state_path})
		patcher.start()
		self.addCleanup(patcher.stop)
		wrapper = os.path.join(self.home.name, "utmctl")
		with open(wrapper, "w") as wrapper_file:
			wrapper_file.write("#!/bin/sh\nexec '{}' '{}' \"$@\"\n".format(sys.executable, FAKE_UTMCTL))
		os.chmod(wrapper, stat.S_IRWXU)
		write_config(self.home.name, {"utmctl_path": wrapper})
		self.now = [0.0]
		for target, replacement in (("wn.cli.time.monotonic", lambda: self.now[0]), ("wn.cli.time.sleep", self.advance)):
			patcher = mock.patch(target, side_effect=replacement)
			patcher.start()
			self.addCleanup(patcher.stop)

	def advance(self, seconds):
		self.now[0] += seconds

	def write_vm(self, **fields):
		vm = {"uuid": "9E1A6F38-2D0B-4C41-8F7E-3B5A1C2D0001", "name": "Windows", "status": "stopped", "ip_addresses": []}
		vm.update(fields)
		with open(self.state_path, "w") as state_file:
			json.dump({"vms": [vm]}, state_file)

	def vm_state(self):
		with open(self.state_path) as state_file:
			return json.load(state_file)["vms"][0]

	def test_waits_until_windows_has_stopped(self):
		self.write_vm(status="started", request_stop_lists=3)
		exit_code, stdout, stderr = self.run_real_cli("stop")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(stdout, "Windows: waiting up to 120 s for Windows to shut down\nWindows: stopped\n")
		self.assertEqual(self.vm_state()["last_stop_flags"], ["--request"])
		self.assertIn("cannot wake the display", stderr)
		self.assertEqual(self.now[0], 4.0)

	def test_fails_when_windows_keeps_running(self):
		self.write_vm(status="started", request_stop_lists=-1)
		exit_code, stdout, stderr = self.run_real_cli("stop")
		self.assertEqual(exit_code, 1)
		self.assertIn("Windows is still running 120 s after the shutdown request", stderr)
		self.assertIn("wn stop --force", stderr)
		self.assertIn("--force-after-timeout", stderr)
		self.assertEqual(stdout, "Windows: waiting up to 120 s for Windows to shut down\n")
		self.assertEqual(self.vm_state()["status"], "started")
		self.assertGreaterEqual(self.now[0], 120)

	def test_force_after_timeout_powers_off(self):
		self.write_vm(status="started", request_stop_lists=-1)
		exit_code, stdout, stderr = self.run_real_cli("stop", "--timeout", "10", "--force-after-timeout", stdin=io.StringIO())
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(stdout, "Windows: waiting up to 10 s for Windows to shut down\nWindows: still running after 10 s, powered off\nWindows: stopped\n")
		self.assertEqual(self.vm_state()["last_stop_flags"], ["--force"])
		self.assertEqual(self.vm_state()["status"], "stopped")

	def test_force_after_timeout_does_nothing_extra_when_windows_stops(self):
		self.write_vm(status="started", request_stop_lists=3)
		exit_code, stdout, stderr = self.run_real_cli("stop", "--force-after-timeout")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(stdout, "Windows: waiting up to 120 s for Windows to shut down\nWindows: stopped\n")
		self.assertEqual(self.vm_state()["last_stop_flags"], ["--request"])

	def test_no_wait_returns_after_the_request(self):
		self.write_vm(status="started", request_stop_lists=-1)
		exit_code, stdout, stderr = self.run_real_cli("stop", "--no-wait")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(stdout, "Windows: shutdown requested\n")
		self.assertEqual(self.vm_state()["last_stop_flags"], ["--request"])
		self.assertIn("cannot wake the display", stderr)
		self.assertEqual(self.now[0], 0.0)

	def test_no_wait_rejects_wait_options(self):
		for extra in (["--timeout", "10"], ["--force-after-timeout"]):
			with self.subTest(extra=extra), mock.patch("sys.stderr", io.StringIO()) as stderr:
				with self.assertRaises(SystemExit) as context:
					self.run_real_cli("stop", "--no-wait", *extra)
				self.assertEqual(context.exception.code, 2)
				self.assertIn("--no-wait cannot be used", stderr.getvalue())
				self.assertNotIn("last_stop_flags", self.vm_state())

	def test_timeout_option(self):
		self.write_vm(status="started", request_stop_lists=-1)
		exit_code, _, stderr = self.run_real_cli("stop", "--timeout", "10")
		self.assertEqual(exit_code, 1)
		self.assertIn("10 s", stderr)
		self.assertEqual(self.now[0], 10.0)

	def test_force_powers_off_without_waiting(self):
		self.write_vm(status="started", request_stop_lists=-1)
		exit_code, stdout, stderr = self.run_real_cli("stop", "--force", "--yes")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(stdout, "")
		self.assertEqual(self.vm_state()["last_stop_flags"], ["--force"])
		self.assertEqual(self.now[0], 0.0)


def input_commands(*combos):
	return [{"execute": "input-send-event", "arguments": arguments} for combo in combos for arguments in combo_to_input_events(combo)]


def printed_commands(stdout):
	return [json.loads(line) for line in stdout.splitlines()]


class KeyCommandTests(CLITestCase):
	def test_dry_run_prints_commands_without_connecting(self):
		exit_code, stdout, _ = self.run_cli("key", "ctrl+alt+del", "--dry-run")
		self.assertEqual(exit_code, 0)
		self.assertEqual(printed_commands(stdout), input_commands("ctrl+alt+del"))
		self.assertEqual(self.qmp_log, [])
		self.assertEqual(self.backend.calls, [])

	def test_dry_run_with_several_combos(self):
		_, stdout, _ = self.run_cli("key", "win+r", "enter", "--hold-ms", "100", "--dry-run")
		self.assertEqual(printed_commands(stdout), input_commands("win+r", "enter"))

	def test_repeated_key_is_pressed_twice(self):
		_, stdout, _ = self.run_cli("key", "down", "down", "--dry-run")
		self.assertEqual(printed_commands(stdout), input_commands("down", "down"))

	def test_sends_through_backend_socket(self):
		self.assertEqual(self.run_cli("key", "win+r")[0], 0)
		press, release = combo_to_input_events("win+r")
		self.assertEqual(self.qmp_log, [
			("connect", "/fake/qmp.sock"),
			("input-send-event", press),
			("input-send-event", release),
			("close",),
		])

	def test_hold_and_delay_set_the_pauses(self):
		with mock.patch("wn.keys.time.sleep") as sleep:
			self.run_cli("key", "a", "--hold-ms", "70", "--delay-ms", "30")
		self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.07, 0.03])

	def test_config_socket_overrides_backend(self):
		write_config(self.home.name, {"qmp_socket": "/configured/qmp.sock"})
		self.run_cli("key", "a")
		self.assertEqual(self.qmp_log[0], ("connect", "/configured/qmp.sock"))
		self.assertEqual(self.backend.calls, [])

	def test_unknown_key_is_reported(self):
		exit_code, _, stderr = self.run_cli("key", "ctrl+entr")
		self.assertEqual(exit_code, 1)
		self.assertIn("enter", stderr)
		self.assertEqual(self.qmp_log, [])


class TypeCommandTests(CLITestCase):
	def test_dry_run(self):
		_, stdout, _ = self.run_cli("type", "Hi", "--dry-run")
		self.assertEqual(printed_commands(stdout), input_commands("shift+h", "i"))

	def test_repeated_letters_are_pressed_twice(self):
		_, stdout, _ = self.run_cli("type", "hello", "--dry-run")
		self.assertEqual(printed_commands(stdout), input_commands("h", "e", "l", "l", "o"))

	def test_sends_text(self):
		self.run_cli("type", "ok")
		self.assertEqual([entry[0] for entry in self.qmp_log], ["connect"] + ["input-send-event"] * 4 + ["close"])

	def test_unsupported_character(self):
		exit_code, _, stderr = self.run_cli("type", "café", "--dry-run")
		self.assertEqual(exit_code, 1)
		self.assertIn("U+00E9", stderr)


def abs_move(x, y):
	return {"events": [{"type": "abs", "data": {"axis": "x", "value": x}}, {"type": "abs", "data": {"axis": "y", "value": y}}]}


def button(name, down):
	return {"events": [{"type": "btn", "data": {"down": down, "button": name}}]}


class MouseCommandTests(CLITestCase):
	def mouse_dry_run(self, *argv):
		exit_code, stdout, stderr = self.run_cli("mouse", *argv, "--dry-run")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.qmp_log, [])
		return [command["arguments"] for command in printed_commands(stdout)]

	def test_move_absolute(self):
		self.assertEqual(self.mouse_dry_run("move", "16384", "100"), [abs_move(16384, 100)])

	def test_move_with_screen_size(self):
		self.assertEqual(self.mouse_dry_run("move", "640", "360", "--screen", "1280x720"), [abs_move(16383, 16383)])

	def test_move_uses_config_screen_size(self):
		write_config(self.home.name, {"screen_size": "1920x1080"})
		self.assertEqual(self.mouse_dry_run("move", "960", "540"), [abs_move(16383, 16383)])

	def test_move_out_of_range(self):
		exit_code, _, stderr = self.run_cli("mouse", "move", "40000", "0", "--dry-run")
		self.assertEqual(exit_code, 1)
		self.assertIn("32767", stderr)

	def test_move_outside_screen(self):
		exit_code, _, stderr = self.run_cli("mouse", "move", "1280", "0", "--screen", "1280x720", "--dry-run")
		self.assertEqual(exit_code, 1)
		self.assertIn("1280x720", stderr)

	def test_bad_screen_size(self):
		exit_code, _, stderr = self.run_cli("mouse", "move", "1", "1", "--screen", "big", "--dry-run")
		self.assertEqual(exit_code, 1)
		self.assertIn("WIDTHxHEIGHT", stderr)

	def test_click_defaults_to_left(self):
		self.assertEqual(self.mouse_dry_run("click"), [button("left", True), button("left", False)])

	def test_click_right(self):
		self.assertEqual(self.mouse_dry_run("click", "right"), [button("right", True), button("right", False)])

	def test_doubleclick(self):
		self.assertEqual(self.mouse_dry_run("doubleclick"), [button("left", True), button("left", False)] * 2)

	def test_down_and_up(self):
		self.assertEqual(self.mouse_dry_run("down", "middle"), [button("middle", True)])
		self.assertEqual(self.mouse_dry_run("up"), [button("left", False)])

	def test_scroll(self):
		self.assertEqual(self.mouse_dry_run("scroll", "down", "2"), [button("wheel-down", True), button("wheel-down", False)] * 2)
		self.assertEqual(self.mouse_dry_run("scroll", "up"), [button("wheel-up", True), button("wheel-up", False)])

	def test_bad_button_is_usage_error(self):
		with mock.patch("sys.stderr", io.StringIO()):
			with self.assertRaises(SystemExit):
				self.run_cli("mouse", "click", "side")

	def test_sends_through_one_shot_socket(self):
		self.assertEqual(self.run_cli("mouse", "click")[0], 0)
		self.assertEqual(self.qmp_log, [
			("connect", "/fake/qmp.sock"),
			("input-send-event", button("left", True)),
			("input-send-event", button("left", False)),
			("close",),
		])


class UTMWiringTests(CLITestCase):
	def vm_qmp_server(self, handlers):
		os.makedirs(os.path.expanduser(UTM_GROUP_CONTAINER), exist_ok=True)
		return FakeQMPServer(handlers, socket_path=qmp_socket_file("Windows"))

	def test_key_reaches_the_vm_qmp_socket(self):
		handlers = {"input-send-event": lambda request: [reply(request, {})]}
		with self.vm_qmp_server(handlers) as server:
			write_config(self.home.name, {"backend": "utm", "vm_name": "Windows"})
			exit_code, _, stderr = self.run_real_cli("key", "win+r")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual([request["execute"] for request in server.received], ["qmp_capabilities", "input-send-event", "input-send-event"])
		self.assertEqual([request["arguments"] for request in server.received[1:]], list(combo_to_input_events("win+r")))

	def screendump_handlers(self):
		container = os.path.expanduser(UTM_GROUP_CONTAINER)
		os.makedirs(container, exist_ok=True)

		def screendump(request):
			with open(os.path.join(container, request["arguments"]["filename"]), "wb") as dump:
				dump.write(b"P6\n1 1\n255\n\x00\x00\x00")
			return [reply(request, {})]
		return {"screendump": screendump}

	def screenshot(self, server, converter):
		write_config(self.home.name, {"backend": "utm", "vm_name": "Windows"})
		with mock.patch("wn.screendump.subprocess.run", side_effect=converter):
			return self.run_real_cli("screenshot", os.path.join(self.home.name, "screen.png"))

	def test_screenshot_writes_png_and_prints_its_path(self):
		output = os.path.join(self.home.name, "screen.png")
		commands = []
		def converter(command, **_):
			commands.append(command)
			return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
		with self.vm_qmp_server(self.screendump_handlers()) as server:
			exit_code, stdout, stderr = self.screenshot(server, converter)
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(stdout, output + "\n")
		self.assertEqual([request["execute"] for request in server.received], ["qmp_capabilities", "screendump"])
		self.assertEqual(commands[0][-2:], ["--out", output])

	def test_screenshot_reports_a_qmp_error(self):
		handlers = {"screendump": lambda request: [error_reply(request, "GenericError", "no display")]}
		with self.vm_qmp_server(handlers) as server:
			exit_code, _, stderr = self.screenshot(server, lambda command, **_: self.fail("nothing to convert"))
		self.assertEqual(exit_code, 1)
		self.assertIn("wn: error:", stderr)
		self.assertIn("no display", stderr)

	def test_screenshot_reports_a_conversion_error(self):
		def converter(command, **_):
			return subprocess.CompletedProcess(command, 1, stdout="", stderr="Error: unable to write")
		with self.vm_qmp_server(self.screendump_handlers()) as server:
			exit_code, _, stderr = self.screenshot(server, converter)
		self.assertEqual(exit_code, 1)
		self.assertIn("sips could not write", stderr)
		self.assertIn("Error: unable to write", stderr)

	def test_screenshot_needs_an_output_path(self):
		with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
			self.run_cli("screenshot")
		self.assertEqual(caught.exception.code, 2)

	def test_status_reports_missing_utmctl(self):
		write_config(self.home.name, {"utmctl_path": "/no/such/utmctl"})
		exit_code, _, stderr = self.run_real_cli("status")
		self.assertEqual(exit_code, 1)
		self.assertIn("/no/such/utmctl", stderr)


GRAB_LINES = [
	{"type": "grab", "active": True},
	{"type": "down", "keycode": 55, "flags": 0, "ts": 1.0},
	{"type": "up", "keycode": 55, "flags": 0, "ts": 1.0},
]
META_DOWN = {"events": [{"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": "meta_l"}}}]}
META_UP = {"events": [{"type": "key", "data": {"down": False, "key": {"type": "qcode", "data": "meta_l"}}}]}


class GrabCommandTests(CLITestCase):
	def setUp(self):
		super().setUp()
		self.helper_path, self.helper_log = write_fake_helper(self.home.name, GRAB_LINES)
		write_config(self.home.name, {"keygrab_binary": self.helper_path})

	def test_grab_sends_input_events(self):
		exit_code, stdout, stderr = self.run_cli("grab")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.qmp_log, [
			("connect", "/fake/qmp-session.sock"),
			("input-send-event", META_DOWN),
			("input-send-event", META_UP),
			("close",),
		])
		self.assertIn("grab on", stdout)

	def test_dry_run_prints_commands_without_connecting(self):
		exit_code, stdout, _ = self.run_cli("grab", "--dry-run")
		self.assertEqual(exit_code, 0)
		commands = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
		self.assertEqual(commands, [
			{"execute": "input-send-event", "arguments": META_DOWN},
			{"execute": "input-send-event", "arguments": META_UP},
		])
		self.assertEqual(self.qmp_log, [])
		self.assertEqual(self.backend.calls, [])

	def test_options_reach_helper(self):
		self.run_cli("grab", "--start-released", "--release-hotkey", "control,shift", "--dry-run")
		self.assertEqual(read_log(self.helper_log)[0], ["--release-hotkey", "control,shift", "--start-released"])

	def test_default_release_hotkey(self):
		self.run_cli("grab", "--dry-run")
		self.assertEqual(read_log(self.helper_log)[0], ["--release-hotkey", "control,option,command"])

	def test_missing_helper_is_reported(self):
		write_config(self.home.name, {"keygrab_binary": "/no/such/wn-keygrab"})
		exit_code, _, stderr = self.run_cli("grab", "--dry-run")
		self.assertEqual(exit_code, 1)
		self.assertIn("cannot run /no/such/wn-keygrab", stderr)

	def test_keycodes_override(self):
		keycodes_path = os.path.join(self.home.name, "keycodes.json")
		with open(keycodes_path, "w") as keycodes_file:
			json.dump({"keycodes": {"55": "meta_r"}}, keycodes_file)
		write_config(self.home.name, {"keygrab_binary": self.helper_path, "keygrab_keycodes": keycodes_path})
		_, stdout, _ = self.run_cli("grab", "--dry-run")
		self.assertIn('"meta_r"', stdout)


OLD_ARGUMENTS = ["-qmp", "tcp:127.0.0.1:4444,server=on,wait=off", "-audiodev", "coreaudio,id=ca0,out.buffer-length=20000", "-device", "intel-hda", "-device", "hda-output,audiodev=ca0"]


TUNED_ARGUMENTS = OLD_ARGUMENTS[:2] + ["-qmp", "tcp:127.0.0.1:4445,server=on,wait=off"] + [
	"-audiodev", "coreaudio,id=ca0,timer-period=2500,out.buffer-length=5000,out.buffer-count=3,out.frequency=48000,out.fixed-settings=on",
	"-device", "intel-hda", "-device", "hda-output,audiodev=ca0",
]


class UpdateVMArgsTests(CLITestCase):
	def setUp(self):
		super().setUp()
		self.bundle = os.path.join(self.home.name, "Windows.utm")
		os.makedirs(self.bundle)
		self.plist_path = os.path.join(self.bundle, "config.plist")
		with open(self.plist_path, "wb") as plist_file:
			plistlib.dump({"Information": {"Name": "Windows"}, "QEMU": {"AdditionalArguments": OLD_ARGUMENTS, "UEFIBoot": True}}, plist_file)
		self.backend.state = "stopped"

	def read_plist(self):
		with open(self.plist_path, "rb") as plist_file:
			return plistlib.load(plist_file)

	def test_qmp_moves_the_servers_to_unix_sockets(self):
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--qmp")
		self.assertEqual(exit_code, 0, stderr)
		config = self.read_plist()
		self.assertEqual(config["QEMU"]["AdditionalArguments"], ["-qmp", "unix:wn-Windows.qmp,server=on,wait=off", "-qmp", "unix:wn-Windows-session.qmp,server=on,wait=off"] + OLD_ARGUMENTS[2:])
		self.assertTrue(config["QEMU"]["UEFIBoot"])
		self.assertIn("wn-Windows-session.qmp", stdout)

	def test_audio_args_replace_audio_block(self):
		audio_args = "-audiodev coreaudio,id=ca0,timer-period=2500 -device intel-hda -device hda-output,audiodev=ca0"
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--audio-args", audio_args)
		self.assertEqual(exit_code, 0, stderr)
		arguments = self.read_plist()["QEMU"]["AdditionalArguments"]
		self.assertEqual(arguments, OLD_ARGUMENTS[:2] + audio_args.split())
		self.assertIn("+-audiodev coreaudio,id=ca0,timer-period=2500", stdout)
		self.assertIn("--audiodev coreaudio,id=ca0,out.buffer-length=20000", stdout)

	def test_extra_args_follow_audio_args(self):
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--audio-args", "-device intel-hda", "--extra-args", "-device virtio-keyboard-pci,id=vkbd")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.read_plist()["QEMU"]["AdditionalArguments"][2:], ["-device", "intel-hda", "-device", "virtio-keyboard-pci,id=vkbd"])
		self.assertIn("vkbd", stdout)

	def test_default_bundle_path_uses_vm_name(self):
		with mock.patch("wn.cli.update_additional_arguments", return_value=([], [])) as update:
			self.assertEqual(self.run_cli("update-vm-args")[0], 0)
		expected = os.path.join(self.home.name, "Library", "Containers", "com.utmapp.UTM", "Data", "Documents", "Windows.utm")
		self.assertEqual(update.call_args.args[0], expected)

	def add_memory_size(self, memory_mib):
		config = self.read_plist()
		config["System"] = {"MemorySize": memory_mib}
		with open(self.plist_path, "wb") as plist_file:
			plistlib.dump(config, plist_file)
		os.makedirs(os.path.join(self.bundle, "Data"))
		os.makedirs(self.group_container)

	@property
	def group_container(self):
		return os.path.join(self.home.name, "Library", "Group Containers", "WDNLXAD4W8.com.utmapp.UTM")

	def test_battery_adds_the_nvdimm_and_prepares_the_file(self):
		self.add_memory_size(8192)
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--battery")
		self.assertEqual(exit_code, 0, stderr)
		path = os.path.join(self.group_container, "wn-battery-Windows.bin")
		arguments = self.read_plist()["QEMU"]["AdditionalArguments"]
		self.assertIn("memory-backend-file,id=wnbattery,share=on,mem-path=wn-battery-Windows.bin,size=1M", arguments)
		self.assertIn("maxmem=8193M,slots=1", arguments)
		with open(path, "rb") as data_file:
			self.assertEqual(data_file.read(4), b"WNBT")
		self.assertIn(path, stdout)

	def test_battery_keeps_an_existing_file(self):
		self.add_memory_size(8192)
		path = os.path.join(self.group_container, "wn-battery-Windows.bin")
		with open(path, "wb") as data_file:
			data_file.write(b"keep")
		self.assertEqual(self.run_cli("update-vm-args", "--bundle", self.bundle, "--battery")[0], 0)
		with open(path, "rb") as data_file:
			self.assertEqual(data_file.read(4), b"keep")

	def test_battery_uses_the_configured_file(self):
		self.add_memory_size(4096)
		path = os.path.join(self.home.name, "battery.bin")
		write_config(self.home.name, {"battery_file": path})
		exit_code, _, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--battery")
		self.assertEqual(exit_code, 0, stderr)
		self.assertIn("memory-backend-file,id=wnbattery,share=on,mem-path={},size=1M".format(path), self.read_plist()["QEMU"]["AdditionalArguments"])
		self.assertTrue(os.path.exists(path))

	def write_arguments(self, arguments):
		config = self.read_plist()
		config["QEMU"]["AdditionalArguments"] = arguments
		with open(self.plist_path, "wb") as plist_file:
			plistlib.dump(config, plist_file)

	def test_no_flags_keeps_custom_audio(self):
		self.write_arguments(TUNED_ARGUMENTS)
		with open(self.plist_path, "rb") as plist_file:
			original = plist_file.read()
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle)
		self.assertEqual(exit_code, 0, stderr)
		with open(self.plist_path, "rb") as plist_file:
			self.assertEqual(plist_file.read(), original)
		self.assertIn("did not change", stdout)

	def test_battery_keeps_custom_audio_and_prints_a_diff(self):
		self.add_memory_size(8192)
		self.write_arguments(TUNED_ARGUMENTS)
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--battery")
		self.assertEqual(exit_code, 0, stderr)
		arguments = self.read_plist()["QEMU"]["AdditionalArguments"]
		self.assertEqual(arguments[:len(TUNED_ARGUMENTS)], TUNED_ARGUMENTS)
		self.assertIn("+-machine nvdimm=on", stdout)
		self.assertNotIn("\n-", stdout.split("+++", 1)[1])

	def test_no_battery_removes_the_nvdimm(self):
		self.add_memory_size(8192)
		self.write_arguments(TUNED_ARGUMENTS)
		self.assertEqual(self.run_cli("update-vm-args", "--bundle", self.bundle, "--battery")[0], 0)
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--no-battery")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.read_plist()["QEMU"]["AdditionalArguments"], TUNED_ARGUMENTS)
		self.assertIn("--machine nvdimm=on", stdout)

	def test_camera_adds_the_usb_webcam_and_turns_it_on_in_the_config(self):
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--camera")
		self.assertEqual(exit_code, 0, stderr)
		arguments = self.read_plist()["QEMU"]["AdditionalArguments"]
		self.assertEqual(arguments[len(OLD_ARGUMENTS):], [
			"-device", "qemu-xhci,id=wncam-xhci",
			"-chardev", "socket,id=wncam,path=wn-Windows-camera.sock,server=on,wait=off",
			"-device", "usb-redir,chardev=wncam,id=wncam0,bus=wncam-xhci.0",
		])
		with open(os.path.join(self.home.name, ".config", "wn", "config.json")) as config_file:
			self.assertTrue(json.load(config_file)["camera"])
		self.assertIn("python3 -m wn agent --install", stdout)

	def test_no_camera_removes_the_usb_webcam(self):
		self.assertEqual(self.run_cli("update-vm-args", "--bundle", self.bundle, "--camera")[0], 0)
		exit_code, _, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--no-camera")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.read_plist()["QEMU"]["AdditionalArguments"], OLD_ARGUMENTS)
		with open(os.path.join(self.home.name, ".config", "wn", "config.json")) as config_file:
			self.assertFalse(json.load(config_file)["camera"])

	def test_no_camera_with_battery_sync_on_says_to_install_the_agent_again(self):
		_, stdout, _ = self.run_cli("update-vm-args", "--bundle", self.bundle, "--no-camera")
		self.assertIn("python3 -m wn agent --install", stdout)
		self.assertNotIn("--uninstall", stdout)

	def test_no_camera_with_battery_sync_off_says_to_uninstall_the_agent(self):
		write_config(self.home.name, {"battery_sync": False, "camera": True})
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--no-camera")
		self.assertEqual(exit_code, 0, stderr)
		self.assertIn("python3 -m wn agent --uninstall", stdout)
		self.assertNotIn("--install", stdout)

	def test_no_camera_with_battery_sync_off_and_shares_keeps_the_agent(self):
		share = share_item()
		write_config(self.home.name, {"battery_sync": False, "camera": True, "shares": [share]})
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--no-camera")
		self.assertEqual(exit_code, 0, stderr)
		self.assertIn("python3 -m wn agent --install", stdout)
		self.assertNotIn("--uninstall", stdout)

	def read_config(self):
		with open(os.path.join(self.home.name, ".config", "wn", "config.json")) as config_file:
			return json.load(config_file)

	def test_camera_for_another_vm_leaves_the_config_and_warns_about_the_socket(self):
		write_config(self.home.name, {"vm_name": "Other"})
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--camera")
		self.assertEqual(exit_code, 0, stderr)
		self.assertIn("usb-redir,chardev=wncam,id=wncam0,bus=wncam-xhci.0", self.read_plist()["QEMU"]["AdditionalArguments"])
		self.assertEqual(self.read_config(), {"vm_name": "Other"})
		self.assertIn("Warning: the agent serves VM 'Other', so update-vm-args left camera in {} as it was".format(
			os.path.join(self.home.name, ".config", "wn", "config.json")), stdout)
		self.assertIn("Warning: the VM listens on {}, but the agent connects to {}. Set vm_name in".format(
			os.path.join(self.group_container, "wn-Windows-camera.sock"), os.path.join(self.group_container, "wn-Other-camera.sock")), stdout)
		self.assertNotIn("Set camera to", stdout)

	def test_no_camera_for_another_vm_leaves_the_config(self):
		self.assertEqual(self.run_cli("update-vm-args", "--bundle", self.bundle, "--camera")[0], 0)
		write_config(self.home.name, {"vm_name": "Other", "camera": True})
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--no-camera")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.read_plist()["QEMU"]["AdditionalArguments"], OLD_ARGUMENTS)
		self.assertEqual(self.read_config(), {"vm_name": "Other", "camera": True})
		self.assertIn("Warning: the agent serves VM 'Other', so update-vm-args left camera in", stdout)
		self.assertNotIn("Set camera to", stdout)

	def test_camera_without_a_config_writes_the_camera_key(self):
		bundle = os.path.join(self.home.name, "Other.utm")
		os.makedirs(bundle)
		with open(os.path.join(bundle, "config.plist"), "wb") as plist_file:
			plistlib.dump({"Information": {"Name": "Other"}, "QEMU": {"AdditionalArguments": OLD_ARGUMENTS, "UEFIBoot": True}}, plist_file)
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", bundle, "--camera")
		self.assertEqual(exit_code, 0, stderr)
		self.assertTrue(self.read_config()["camera"])
		self.assertIn("Set camera to true", stdout)

	def test_camera_for_the_configured_vm_has_no_warning(self):
		_, stdout, _ = self.run_cli("update-vm-args", "--bundle", self.bundle, "--camera")
		self.assertNotIn("Warning", stdout)

	def test_camera_flags_are_exclusive(self):
		with self.assertRaises(SystemExit):
			self.run_cli("update-vm-args", "--camera", "--no-camera")

	def test_refuses_running_vm(self):
		self.backend.state = "running"
		exit_code, _, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle)
		self.assertEqual(exit_code, 1)
		self.assertIn("stop", stderr)
		self.assertEqual(self.read_plist()["QEMU"]["AdditionalArguments"], OLD_ARGUMENTS)


class DeleteCommandTests(CLITestCase):
	def setUp(self):
		super().setUp()
		self.backend.state = "stopped"
		self.battery = default_battery_file("Windows")
		os.makedirs(os.path.dirname(self.battery), exist_ok=True)
		with open(self.battery, "w"):
			pass

	def config_values(self):
		with open(os.path.join(self.home.name, ".config", "wn", "config.json")) as config_file:
			return json.load(config_file)

	def test_yes_deletes_the_vm_its_files_and_its_settings(self):
		write_config(self.home.name, {"vm_name": "Windows", "camera": True, "shares": [share_item()]})
		exit_code, stdout, stderr = self.run_cli("delete", "--yes")
		self.assertEqual(exit_code, 0, stderr)
		self.assertIn(("delete",), self.backend.calls)
		self.assertFalse(os.path.exists(self.battery))
		self.assertEqual(self.config_values(), {"camera": True})
		self.assertIn("Removed the VM Windows in UTM, with its disk\n", stdout)

	def test_running_vm_is_refused(self):
		self.backend.state = "running"
		exit_code, _, stderr = self.run_cli("delete", "--yes")
		self.assertEqual(exit_code, 1)
		self.assertIn("Windows is running. Stop it first with wn stop", stderr)
		self.assertNotIn(("delete",), self.backend.calls)
		self.assertTrue(os.path.exists(self.battery))

	def test_no_keeps_everything(self):
		exit_code, stdout, _ = self.run_cli("delete", stdin=TerminalInput("n\n"))
		self.assertEqual(exit_code, 1)
		self.assertIn("Delete these? [y/N]: Windows: not deleted\n", stdout)
		self.assertNotIn(("delete",), self.backend.calls)
		self.assertTrue(os.path.exists(self.battery))

	def test_yes_answer_deletes(self):
		exit_code, _, stderr = self.run_cli("delete", stdin=TerminalInput("y\n"))
		self.assertEqual(exit_code, 0, stderr)
		self.assertIn(("delete",), self.backend.calls)

	def test_without_terminal_needs_yes(self):
		exit_code, _, stderr = self.run_cli("delete", stdin=io.StringIO("y\n"))
		self.assertEqual(exit_code, 1)
		self.assertIn("Add --yes", stderr)
		self.assertNotIn(("delete",), self.backend.calls)

	def test_another_vm_keeps_vm_name_and_other_shares(self):
		other = share_item(name="Other", vm="Other", mac_path="/Users/c/Other", vm_path="C:\\Users\\c\\Other", port=FIRST_PORT + 1)
		write_config(self.home.name, {"vm_name": "Windows", "shares": [share_item(), other]})
		exit_code, _, stderr = self.run_cli("delete", "Other", "--yes")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.config_values(), {"vm_name": "Windows", "shares": [share_item()]})
		self.assertTrue(os.path.exists(self.battery))

	def test_vm_unknown_to_utm_still_removes_its_files(self):
		self.backend.in_utm = False
		exit_code, stdout, stderr = self.run_cli("delete", "--yes")
		self.assertEqual(exit_code, 0, stderr)
		self.assertIn("UTM has no VM named Windows\n", stdout)
		self.assertNotIn(("status",), self.backend.calls)
		self.assertFalse(os.path.exists(self.battery))

	def test_all_also_removes_the_support_folder(self):
		support = os.path.join(self.home.name, "Library", "Application Support", "wn")
		os.makedirs(os.path.join(support, "downloads"))
		exit_code, stdout, stderr = self.run_cli("delete", "--all", "--yes")
		self.assertEqual(exit_code, 0, stderr)
		self.assertIn(("delete",), self.backend.calls)
		self.assertFalse(os.path.exists(support))
		self.assertIn("Removed the folder {} with the download cache, the share files, and wn-agent.app\n".format(support), stdout)

	def test_all_refuses_a_running_vm(self):
		self.backend.state = "running"
		support = os.path.join(self.home.name, "Library", "Application Support", "wn")
		os.makedirs(support)
		exit_code, _, stderr = self.run_cli("delete", "--all", "--yes")
		self.assertEqual(exit_code, 1)
		self.assertIn("Windows is running. Stop it first with wn stop", stderr)
		self.assertTrue(os.path.exists(support))

	def test_cache_removes_only_the_download_cache(self):
		write_config(self.home.name, {"vm_name": "Windows"})
		cache = os.path.join(self.home.name, "Library", "Application Support", "wn", "downloads")
		os.makedirs(cache)
		exit_code, stdout, stderr = self.run_cli("delete", "--cache", "--yes")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.backend.calls, [])
		self.assertFalse(os.path.exists(cache))
		self.assertTrue(os.path.exists(self.battery))
		self.assertEqual(self.config_values(), {"vm_name": "Windows"})
		self.assertEqual(stdout, "wn delete removes:\n  the download cache {0}\nRemoved the download cache {0}\n".format(cache))

	def test_cache_asks_first(self):
		cache = os.path.join(self.home.name, "Library", "Application Support", "wn", "downloads")
		os.makedirs(cache)
		exit_code, stdout, _ = self.run_cli("delete", "--cache", stdin=TerminalInput("n\n"))
		self.assertEqual(exit_code, 1)
		self.assertIn("Delete these? [y/N]: the download cache: not deleted\n", stdout)
		self.assertTrue(os.path.exists(cache))

	def test_cache_without_a_cache_has_nothing_to_delete(self):
		exit_code, stdout, _ = self.run_cli("delete", "--cache", "--yes")
		self.assertEqual(exit_code, 1)
		self.assertEqual(stdout, "nothing to delete for the download cache\n")
		self.assertEqual(self.backend.calls, [])

	def test_cache_rejects_a_name_and_all(self):
		for extra in (["Windows"], ["--all"]):
			with self.subTest(extra=extra), mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
				self.run_cli("delete", "--cache", "--yes", *extra)
			self.assertEqual(caught.exception.code, 2)
			self.assertEqual(self.backend.calls, [])

	def test_nothing_to_delete(self):
		os.remove(self.battery)
		self.backend.in_utm = False
		exit_code, stdout, _ = self.run_cli("delete", "--yes")
		self.assertEqual(exit_code, 1)
		self.assertIn("nothing to delete for Windows\n", stdout)


if __name__ == "__main__":
	unittest.main()


class SetupCommandTests(CLITestCase):
	def run_setup(self, *argv, answers=""):
		stdout = io.StringIO()
		stderr = io.StringIO()
		with mock.patch("wn.cli.Installer") as installer_class, mock.patch("wn.cli.getpass.getuser", return_value="macuser"):
			exit_code = main(["setup"] + list(argv), stdout=stdout, stderr=stderr, stdin=io.StringIO(answers))
		options = installer_class.call_args[0][0] if installer_class.called else None
		return exit_code, options, stdout.getvalue(), stderr.getvalue()

	def test_yes_uses_defaults(self):
		exit_code, options, _, _ = self.run_setup("--yes")
		self.assertEqual(exit_code, 0)
		self.assertEqual((options.name, options.username, options.password), ("Windows", "macuser", ""))
		self.assertEqual((options.cpus, options.memory_mib, options.disk_gib, options.headless), (8, 8192, 64, False))
		self.assertEqual((options.software, options.edition), (("nvda",), "Pro"))
		self.assertEqual(options.downloads_dir, os.path.join("~", "Library", "Application Support", "wn", "downloads"))

	def test_yes_with_flags(self):
		_, options, _, _ = self.run_setup("--yes", "--name", "Win", "--username", "carter", "--password", "pw", "--cpus", "4",
			"--memory-mib", "4096", "--disk-gib", "80", "--headless", "--software", "none", "--edition", "Home", "--downloads-dir", "/tmp/dl")
		self.assertEqual((options.name, options.username, options.password), ("Win", "carter", "pw"))
		self.assertEqual((options.cpus, options.memory_mib, options.disk_gib, options.headless), (4, 4096, 80, True))
		self.assertEqual((options.software, options.edition, options.downloads_dir), ((), "Home", "/tmp/dl"))

	def test_prompts_accept_defaults(self):
		exit_code, options, stdout, _ = self.run_setup(answers="\n" * 9)
		self.assertEqual(exit_code, 0)
		self.assertEqual((options.name, options.username, options.software), ("Windows", "macuser", ("nvda",)))
		self.assertIn("VM name [Windows]: ", stdout)
		self.assertIn("1. NVDA screen reader", stdout)
		self.assertIn("Numbers separated by commas, 0 for none [1]: ", stdout)

	def test_prompts_take_answers(self):
		answers = "\n".join(["Work", "carter", "secret", "6", "6144", "100", "yes", "0", "Education"]) + "\n"
		_, options, _, _ = self.run_setup(answers=answers)
		self.assertEqual((options.name, options.username, options.password), ("Work", "carter", "secret"))
		self.assertEqual((options.cpus, options.memory_mib, options.disk_gib, options.headless), (6, 6144, 100, True))
		self.assertEqual((options.software, options.edition), ((), "Education"))

	def test_prompt_repeats_bad_number(self):
		answers = "\n".join(["", "", "", "many", "4", "", "", "", "", ""]) + "\n"
		_, options, stdout, _ = self.run_setup(answers=answers)
		self.assertEqual(options.cpus, 4)
		self.assertIn("Enter a whole number", stdout)

	def test_flags_become_prompt_defaults(self):
		_, options, stdout, _ = self.run_setup("--name", "Lab", answers="\n" * 9)
		self.assertEqual(options.name, "Lab")
		self.assertIn("VM name [Lab]: ", stdout)

	def test_unknown_software(self):
		exit_code, _, _, stderr = self.run_setup("--yes", "--software", "jaws")
		self.assertEqual(exit_code, 1)
		self.assertIn("jaws", stderr)

	def test_unknown_edition(self):
		exit_code, _, _, stderr = self.run_setup("--yes", "--edition", "Ultimate")
		self.assertEqual(exit_code, 1)
		self.assertIn("Ultimate", stderr)

	def test_battery_is_on_by_default(self):
		_, options, _, _ = self.run_setup("--yes")
		self.assertTrue(options.battery)
		self.assertIsNone(options.battery_file)

	def test_no_battery(self):
		_, options, _, _ = self.run_setup("--yes", "--no-battery")
		self.assertFalse(options.battery)

	def test_camera_is_on_by_default(self):
		_, options, _, _ = self.run_setup("--yes")
		self.assertTrue(options.camera)

	def test_no_camera(self):
		_, options, _, _ = self.run_setup("--yes", "--no-camera")
		self.assertFalse(options.camera)

	def test_battery_file_comes_from_the_config(self):
		write_config(self.home.name, {"battery_file": "/tmp/wn-battery.bin"})
		_, options, _, _ = self.run_setup("--yes")
		self.assertEqual(options.battery_file, "/tmp/wn-battery.bin")

	def test_end_of_input_uses_defaults(self):
		exit_code, options, _, _ = self.run_setup(answers="")
		self.assertEqual(exit_code, 0)
		self.assertEqual(options.name, "Windows")
