"""Write the Mac's battery status to the native battery in the guest, and manage the launch agent that runs it."""

import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time
from xml.sax.saxutils import escape

from wn.appbundle import build_app, executable_path, remove_app
from wn.backend import create_backend
from wn.battery_file import BatteryFile, battery_file_path, battery_writer
from wn.battery_sync import BATTERY_CHANNEL_FILE, LOG_PREFIX, battery_reading, run_sync_loop, sync_message
from wn.errors import WnError
from wn.gdbstub import DEFAULT_HOST as GDB_HOST
from wn.qmp import qmp_client_for

PMSET_COMMAND = ["/usr/bin/pmset", "-g", "batt"]
LAUNCHCTL_PATH = "/bin/launchctl"
AC_POWER_TEXT = "'AC Power'"
CHARGING_STATES = ("charging", "finishing charge")
CHARGED_STATE = "charged"
MINUTES_PER_HOUR = 60
LAUNCHD_LABEL = "com.wn.agent"
LAUNCHD_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launchd", LAUNCHD_LABEL + ".plist")
LAUNCH_AGENTS_DIRECTORY = os.path.join("~", "Library", "LaunchAgents")
LOG_PATH = os.path.join("~", "Library", "Logs", "wn-agent.log")
PID_FILE = os.path.join("~", "Library", "Application Support", "wn", "agent.pid")
REPOSITORY_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATTERY_LINE = re.compile(r"-InternalBattery-\d+.*?\t(?P<percent>\d+)%;\s*(?P<state>[^;]+);\s*(?P<rest>.*)")
TIME_REMAINING = re.compile(r"(?P<hours>\d+):(?P<minutes>\d{2}) remaining")
LAUNCHCTL_PID = re.compile(r"^\s*pid = (\d+)\s*$", re.MULTILINE)
EXIT_RUNNING = 0
EXIT_NOT_RUNNING = 1


class AgentError(WnError):
	"""Raised when the agent cannot read the battery or manage its launch agent."""


def utc_now():
	return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_pmset(text, updated):
	"""Turn ``pmset -g batt`` output into the battery status object that the battery sync reads."""
	lines = text.splitlines()
	on_ac = bool(lines) and AC_POWER_TEXT in lines[0]
	status = {"present": False, "percent": None, "charging": False, "on_ac": on_ac, "minutes_remaining": None, "updated": updated}
	for line in lines[1:]:
		match = BATTERY_LINE.search(line)
		if not match:
			continue
		state = match.group("state").strip()
		time_match = TIME_REMAINING.search(match.group("rest"))
		minutes = None
		if time_match and state != CHARGED_STATE:
			minutes = int(time_match.group("hours")) * MINUTES_PER_HOUR + int(time_match.group("minutes"))
		status.update(present=True, percent=int(match.group("percent")), charging=state in CHARGING_STATES, minutes_remaining=minutes)
		break
	return status


def battery_status(run=None):
	run = run or subprocess.run
	result = run(PMSET_COMMAND, capture_output=True, text=True)
	if result.returncode != 0:
		raise AgentError("pmset failed with exit code {}: {}".format(result.returncode, (result.stderr or "").strip() or "no output"))
	return parse_pmset(result.stdout, utc_now())


def qmp_address(config, backend_factory=create_backend):
	return config.qmp_socket or backend_factory(config).qmp_socket_path()


def sync_once(sync, status):
	"""Write one reading to the guest and return a line that says what happened."""
	try:
		return sync_message(sync.write(battery_reading(status)))
	except WnError as error:
		return LOG_PREFIX + str(error)


def launch_agent_path():
	return os.path.join(os.path.expanduser(LAUNCH_AGENTS_DIRECTORY), LAUNCHD_LABEL + ".plist")


def pid_file_path():
	return os.path.expanduser(PID_FILE)


def _launchctl(run, *arguments):
	return run([LAUNCHCTL_PATH] + list(arguments), capture_output=True, text=True)


def _require_success(result, verb):
	if result.returncode != 0:
		detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "no output"
		raise AgentError("launchctl {} failed with exit code {}: {}".format(verb, result.returncode, detail))


def _service_target(uid):
	return "gui/{}/{}".format(uid, LAUNCHD_LABEL)


def launchd_state(run, uid):
	"""Return (loaded, pid) from ``launchctl print``. The PID is None while the job is loaded but not running."""
	result = _launchctl(run, "print", _service_target(uid))
	if result.returncode != 0:
		return False, None
	match = LAUNCHCTL_PID.search(result.stdout or "")
	return True, int(match.group(1)) if match else None


def foreground_pid():
	"""Return the PID from the pid file, or None if there is no file or that process has exited."""
	try:
		with open(pid_file_path()) as pid_file:
			pid = int(pid_file.read().strip())
	except (OSError, ValueError):
		return None
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return None
	except PermissionError:
		pass
	return pid


def render_launch_agent(python, working_directory):
	with open(LAUNCHD_TEMPLATE) as template_file:
		text = template_file.read()
	for placeholder, value in (
		("@LABEL@", LAUNCHD_LABEL),
		("@AGENT@", executable_path()),
		("@PYTHON@", python),
		("@WORKING_DIRECTORY@", working_directory),
		("@LOG@", os.path.expanduser(LOG_PATH)),
	):
		text = text.replace(placeholder, escape(value))
	return text


def install_launch_agent(python, working_directory, run=None, uid=None):
	"""Build the agent app, write the launch agent plist that runs it, and load it. Return the plist path.

	The plist starts Python through the app's stub, so macOS asks once whether wn-agent may use UTM's data.
	"""
	run = run or subprocess.run
	uid = os.getuid() if uid is None else uid
	path = launch_agent_path()
	if os.path.exists(path):
		_launchctl(run, "bootout", "gui/{}/{}".format(uid, LAUNCHD_LABEL))
	build_app(run)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	os.makedirs(os.path.dirname(os.path.expanduser(LOG_PATH)), exist_ok=True)
	with open(path, "w") as plist_file:
		plist_file.write(render_launch_agent(python, working_directory))
	_require_success(_launchctl(run, "bootstrap", "gui/{}".format(uid), path), "bootstrap")
	return path


def uninstall_launch_agent(run=None, uid=None):
	"""Unload and delete the launch agent. Return False if it was not installed."""
	run = run or subprocess.run
	uid = os.getuid() if uid is None else uid
	path = launch_agent_path()
	if not os.path.exists(path):
		return False
	_launchctl(run, "bootout", "gui/{}/{}".format(uid, LAUNCHD_LABEL))
	os.remove(path)
	remove_app()
	return True


def _stop_on_sigterm(signum, frame):
	raise KeyboardInterrupt


def serve(config, output, wait=time.sleep):
	"""Run the sync loop until Ctrl+C or SIGTERM, with the PID in the pid file while it runs."""
	if not config.battery_sync:
		raise AgentError("battery_sync is off in the config, so the agent has nothing to do")
	running = foreground_pid()
	if running is not None:
		raise AgentError("the agent is already running with PID {}. Run wn agent stop first".format(running))
	path = pid_file_path()
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w") as pid_file:
		pid_file.write("{}\n".format(os.getpid()))
	previous_handler = signal.signal(signal.SIGTERM, _stop_on_sigterm)
	try:
		output.write("Running the agent in the foreground with PID {}. Press Ctrl+C to stop\n".format(os.getpid()))
		output.flush()
		run_sync_loop(battery_writer(config, lambda: qmp_address(config)), battery_status, output, wait)
	except KeyboardInterrupt:
		pass
	finally:
		signal.signal(signal.SIGTERM, previous_handler)
		os.remove(path)


def start_agent(config, output, foreground=False, run=None, uid=None, wait=time.sleep):
	"""Start the launchd job if it is installed, else serve in the foreground."""
	run = run or subprocess.run
	uid = os.getuid() if uid is None else uid
	path = launch_agent_path()
	if foreground or not os.path.exists(path):
		serve(config, output, wait)
		return
	loaded, pid = launchd_state(run, uid)
	if pid is not None:
		output.write("{} is already running with PID {}\n".format(LAUNCHD_LABEL, pid))
	elif loaded:
		_require_success(_launchctl(run, "kickstart", _service_target(uid)), "kickstart")
		output.write("Started {} with launchctl kickstart\n".format(LAUNCHD_LABEL))
	else:
		_require_success(_launchctl(run, "bootstrap", "gui/{}".format(uid), path), "bootstrap")
		output.write("Loaded and started {} with launchctl bootstrap\n".format(LAUNCHD_LABEL))


def stop_agent(output, run=None, uid=None):
	"""Unload the launchd job, or send SIGTERM to the agent in the pid file.

	The job has KeepAlive set, so launchd would restart it after a plain SIGTERM. ``bootout`` stops it until
	the next login or ``wn agent start``, and leaves the plist in place.
	"""
	run = run or subprocess.run
	uid = os.getuid() if uid is None else uid
	if os.path.exists(launch_agent_path()) and launchd_state(run, uid)[0]:
		_require_success(_launchctl(run, "bootout", _service_target(uid)), "bootout")
		output.write("Stopped {} with launchctl bootout. It starts again when you log in or run wn agent start\n".format(LAUNCHD_LABEL))
		return
	pid = foreground_pid()
	if pid is None:
		output.write("not running\n")
		return
	os.kill(pid, signal.SIGTERM)
	output.write("Sent SIGTERM to the agent with PID {}\n".format(pid))


def _qmp_line(address):
	try:
		with qmp_client_for(address) as client:
			state = client.execute("query-status")["status"]
	except (WnError, OSError, KeyError, TypeError):
		return "QMP: {} (not reachable)".format(address)
	return "QMP: {} (reachable, VM {})".format(address, state)


def _battery_file_line(path):
	if not os.path.exists(path):
		return "Battery file: {} (missing)".format(path)
	try:
		_, block = BatteryFile(path).read()
	except WnError as error:
		return "Battery file: {} ({})".format(path, error)
	if block is None:
		return "Battery file: {} (no battery block yet)".format(path)
	updated = datetime.datetime.fromtimestamp(block["updated_unix"], datetime.timezone.utc)
	return "Battery file: {} (updated {})".format(path, updated.strftime("%Y-%m-%dT%H:%M:%SZ"))


def agent_status(config, output, run=None, uid=None):
	"""Print whether the agent is installed and running, and what it talks to. Return 0 when it runs, else 1."""
	run = run or subprocess.run
	uid = os.getuid() if uid is None else uid
	path = launch_agent_path()
	installed = os.path.exists(path)
	pid = launchd_state(run, uid)[1] if installed else None
	if pid is None:
		pid = foreground_pid()
	channel = config.battery_channel if config.battery_channel == BATTERY_CHANNEL_FILE else "{} ({}:{})".format(config.battery_channel, GDB_HOST, config.gdb_port)
	lines = [
		"Installed: yes ({})".format(path) if installed else "Installed: no",
		"Running: yes, PID {}".format(pid) if pid is not None else "Running: no",
		_qmp_line(qmp_address(config)),
		"Battery channel: {}".format(channel),
		_battery_file_line(battery_file_path(config)),
	]
	output.write("".join(line + "\n" for line in lines))
	return EXIT_RUNNING if pid is not None else EXIT_NOT_RUNNING


def run_agent_command(arguments, config, output, messages=None, wait=time.sleep):
	messages = messages or sys.stderr
	if arguments.once:
		status = battery_status()
		output.write(json.dumps(status, indent="\t") + "\n")
		output.flush()
		if config.battery_sync:
			messages.write(sync_once(battery_writer(config, lambda: qmp_address(config)), status) + "\n")
	elif arguments.install:
		path = install_launch_agent(sys.executable, REPOSITORY_DIRECTORY)
		output.write("Installed {}. The agent now starts when you log in. Log: {}\n".format(path, os.path.expanduser(LOG_PATH)))
	elif arguments.uninstall:
		if uninstall_launch_agent():
			output.write("Removed {}\n".format(launch_agent_path()))
		else:
			output.write("The launch agent is not installed\n")
	elif arguments.agent_command == "start":
		start_agent(config, output, arguments.foreground, wait=wait)
	elif arguments.agent_command == "stop":
		stop_agent(output)
	elif arguments.agent_command == "status":
		return agent_status(config, output)
