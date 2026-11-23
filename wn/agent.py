"""Write the Mac's battery status to the native battery in the guest, and manage the launch agent that runs it."""

import ctypes
import datetime
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from xml.sax.saxutils import escape

from wn.appbundle import build_app, camera_executable_path, executable_path, remove_app
from wn.backend import create_backend
from wn.battery_file import BatteryFile, battery_file_path, battery_writer
from wn.battery_sync import LOG_PREFIX, MINUTES_PER_HOUR, POLL_SECONDS, WRITE_INTERVAL_SECONDS, battery_reading, run_sync_loop, sync_message
from wn.config import load_config_skipping_bad_shares
from wn.errors import WnError
from wn.qmp import qmp_client_for
from wn.share_sftp import SERVING_STATE, SftpServerSupervisor, read_mac_state, share_paths, utc_now
from wn.shares import HOST_ADDRESS, REMOVE_COMMAND, shares_for_vm
from wn.utm_config import camera_socket_file

PMSET_COMMAND = ["/usr/bin/pmset", "-g", "batt"]
LAUNCHCTL_PATH = "/bin/launchctl"
AC_POWER_TEXT = "'AC Power'"
CHARGING_STATES = ("charging", "finishing charge")
CHARGED_STATE = "charged"
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
LIBSYSTEM_PATH = "/usr/lib/libSystem.B.dylib"
POWER_SOURCES_NOTIFICATION = b"com.apple.system.powersources"
NOTIFY_STATUS_OK = 0
NOTIFY_READ_BYTES = 4096
NOTIFIED_POLL_SECONDS = WRITE_INTERVAL_SECONDS
CAMERA_PID_FILE = os.path.join("~", "Library", "Application Support", "wn", "camera.pid")
CAMERA_RESTART_SECONDS = 5
CAMERA_STOP_SECONDS = 3
CAMERA_LOG_PREFIX = "camera: "
CAMERA_ACCESS_PREFIX = "wn-camera: access: "
CAMERA_ACCESS_ALLOWED = "allowed"
LOG_TAIL_BYTES = 64 * 1024
IDLE_WAIT_SECONDS = 3600
BOOTOUT_POLL_SECONDS = 0.1
BOOTOUT_WAIT_SECONDS = 10
INVALID_SHARE_LOG_PREFIX = "shares: ignoring an invalid share until it is fixed: "


class AgentError(WnError):
	"""Raised when the agent cannot read the battery or manage its launch agent."""


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


def camera_pid_file_path():
	return os.path.expanduser(CAMERA_PID_FILE)


def _write_pid_file(path, pid):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w") as pid_file:
		pid_file.write("{}\n".format(pid))


def _live_pid(path):
	"""Return the PID in ``path``, or None if there is no file or that process has exited."""
	try:
		with open(path) as pid_file:
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


def foreground_pid():
	return _live_pid(pid_file_path())


def camera_pid():
	return _live_pid(camera_pid_file_path())


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


def install_launch_agent(python, working_directory, run=None, uid=None, sleep=time.sleep):
	"""Build the agent app, write the launch agent plist that runs it, and load it. Return the plist path.

	The plist starts Python through the app's stub, so macOS asks once whether wn-agent may use UTM's data.
	``launchctl bootout`` returns before the old job is gone, and a bootstrap before then fails with error 5, so wait for it.
	"""
	run = run or subprocess.run
	uid = os.getuid() if uid is None else uid
	path = launch_agent_path()
	if os.path.exists(path):
		_launchctl(run, "bootout", "gui/{}/{}".format(uid, LAUNCHD_LABEL))
		for _ in range(round(BOOTOUT_WAIT_SECONDS / BOOTOUT_POLL_SECONDS)):
			if not launchd_state(run, uid)[0]:
				break
			sleep(BOOTOUT_POLL_SECONDS)
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


def _register_power_notifications():
	library = ctypes.CDLL(LIBSYSTEM_PATH)
	library.notify_register_file_descriptor.restype = ctypes.c_uint32
	notify_fd = ctypes.c_int()
	token = ctypes.c_int()
	status = library.notify_register_file_descriptor(POWER_SOURCES_NOTIFICATION, ctypes.byref(notify_fd), 0, ctypes.byref(token))
	if status != NOTIFY_STATUS_OK:
		raise AgentError("notify_register_file_descriptor returned status {}".format(status))
	return notify_fd.value


def power_change_waiter(log, register=_register_power_notifications):
	"""Return a wait function that ends early when macOS reports a power source change, and its poll interval.

	When the notification cannot be registered, log why and return time.sleep with the short poll interval.
	"""
	try:
		notify_fd = register()
	except (OSError, AttributeError, AgentError) as error:
		log.write("{}no power source notifications ({}), polling every {} s\n".format(LOG_PREFIX, error, POLL_SECONDS))
		log.flush()
		return time.sleep, POLL_SECONDS

	def wait(seconds):
		readable, _, _ = select.select([notify_fd], [], [], seconds)
		if readable:
			os.read(notify_fd, NOTIFY_READ_BYTES)

	return wait, NOTIFIED_POLL_SECONDS


def _stop_on_sigterm(signum, frame):
	raise KeyboardInterrupt


def camera_command(config):
	return [camera_executable_path(), "--socket", camera_socket_file(config.vm_name)]


class CameraSupervisor:
	"""Runs wn-camera as a child, starts it again 5 s after it exits, and stops it with the agent.

	wn-camera inherits the agent's stdout and stderr, so its lines land in the agent log. The supervisor logs a message only when it differs from the last one.
	"""

	def __init__(self, command, log, popen=subprocess.Popen, wait=None):
		self.command = command
		self.log = log
		self.popen = popen
		self.stopping = threading.Event()
		self.wait = wait or self.stopping.wait
		self.lock = threading.Lock()
		self.process = None
		self.thread = None
		self.last_message = None

	def _write(self, message):
		if message == self.last_message:
			return
		self.last_message = message
		self.log.write(CAMERA_LOG_PREFIX + message + "\n")
		self.log.flush()

	def _spawn(self):
		with self.lock:
			if self.stopping.is_set():
				return None
			try:
				self.process = self.popen(self.command)
			except OSError as error:
				self._write("cannot start {}: {}".format(self.command[0], error))
				return None
			_write_pid_file(camera_pid_file_path(), self.process.pid)
			return self.process

	def run(self):
		while not self.stopping.is_set():
			process = self._spawn()
			if process is not None:
				code = process.wait()
				if os.path.exists(camera_pid_file_path()):
					os.remove(camera_pid_file_path())
				if self.stopping.is_set():
					return
				self._write("wn-camera exited with code {}. Starting it again in {} s".format(code, CAMERA_RESTART_SECONDS))
			self.wait(CAMERA_RESTART_SECONDS)

	def start(self):
		self.thread = threading.Thread(target=self.run, daemon=True)
		self.thread.start()

	def stop(self):
		"""Send SIGTERM to wn-camera, and SIGKILL if it has not exited 3 s later."""
		with self.lock:
			self.stopping.set()
			process = self.process
		if process is not None:
			process.terminate()
			try:
				process.wait(timeout=CAMERA_STOP_SECONDS)
			except subprocess.TimeoutExpired:
				process.kill()
				process.wait()
		if self.thread is not None:
			self.thread.join()


def load_serving_config(log, path=None):
	"""Return the config without its invalid shares, and log one line for each share left out."""
	config, problems = load_config_skipping_bad_shares(path)
	for problem in problems:
		log.write(INVALID_SHARE_LOG_PREFIX + problem + "\n")
	log.flush()
	return config


def serve(config, output, wait=None, popen=subprocess.Popen):
	"""Run the battery sync loop, the camera helper and the share servers until Ctrl+C or SIGTERM, with the PID in the pid file while it runs."""
	shares = shares_for_vm(config.shares, config.vm_name)
	if not config.battery_sync and not config.camera and not shares:
		raise AgentError("battery_sync and camera are off and the config has no shares for VM '{}', so the agent has nothing to do".format(config.vm_name))
	running = foreground_pid()
	if running is not None:
		raise AgentError("the agent is already running with PID {}. Run wn agent stop first".format(running))
	path = pid_file_path()
	_write_pid_file(path, os.getpid())
	previous_handler = signal.signal(signal.SIGTERM, _stop_on_sigterm)
	camera = CameraSupervisor(camera_command(config), output, popen) if config.camera else None
	share_supervisor = SftpServerSupervisor(output, popen=popen, load=lambda path: load_serving_config(output, path))
	try:
		output.write("Running the agent in the foreground with PID {}. Press Ctrl+C to stop\n".format(os.getpid()))
		output.flush()
		if camera is not None:
			camera.start()
		share_supervisor.start()
		if config.battery_sync:
			poll_seconds = POLL_SECONDS
			if wait is None:
				wait, poll_seconds = power_change_waiter(output)
			run_sync_loop(battery_writer(config), battery_status, output, wait, poll_seconds)
		else:
			wait = wait or time.sleep
			while not wait(IDLE_WAIT_SECONDS):
				pass
	except KeyboardInterrupt:
		pass
	finally:
		share_supervisor.stop()
		if camera is not None:
			camera.stop()
		signal.signal(signal.SIGTERM, previous_handler)
		os.remove(path)


def start_agent(config, output, foreground=False, run=None, uid=None, wait=None):
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


def last_camera_access(log_path):
	"""Return the text of the last camera access line wn-camera wrote to the log, or None."""
	try:
		with open(log_path, "rb") as log_file:
			log_file.seek(0, os.SEEK_END)
			log_file.seek(max(0, log_file.tell() - LOG_TAIL_BYTES))
			text = log_file.read().decode("utf-8", "replace")
	except OSError:
		return None
	states = [line[len(CAMERA_ACCESS_PREFIX):] for line in text.splitlines() if line.startswith(CAMERA_ACCESS_PREFIX)]
	return states[-1] if states else None


def _camera_lines(config):
	if not config.camera:
		return ["Camera: off in the config"]
	pid = camera_pid()
	lines = ["Camera: running, PID {}".format(pid) if pid is not None else "Camera: stopped"]
	access = last_camera_access(os.path.expanduser(LOG_PATH))
	if access is not None and access != CAMERA_ACCESS_ALLOWED:
		lines.append("Camera access: {}".format(access))
	return lines


def _share_lines(config, running):
	shares = shares_for_vm(config.shares, config.vm_name)
	if not shares:
		return ["Shares: none in the config"]
	lines = ["Shares: {0} ({0} Mac to Windows)".format(len(shares))]
	states = read_mac_state(share_paths())["shares"] if running else {}
	for share in shares:
		state = states.get(share.name)
		if not running:
			lines.append("Share {}: not served, the agent is not running".format(share.name))
		elif state is None:
			lines.append("Share {}: no state yet".format(share.name))
		elif state.get("state") == SERVING_STATE:
			lines.append("Share {}: serving {} on {}:{}, PID {}".format(share.name, share.mac_path, HOST_ADDRESS, share.port, state.get("pid")))
		else:
			lines.append("Share {}: {}, {}".format(share.name, state.get("state"), state.get("detail")))
	return lines


def _uninstall_share_warning(count):
	if count == 1:
		return "1 share is still in the config. It stops working until you install the agent again. To remove it for good, run wn {} NAME before you uninstall.\n".format(REMOVE_COMMAND)
	return "{} shares are still in the config. They stop working until you install the agent again. To remove them for good, run wn {} NAME for each before you uninstall.\n".format(count, REMOVE_COMMAND)


def agent_status(config, output, run=None, uid=None):
	"""Print whether the agent is installed and running, and what it talks to. Return 0 when it runs, else 1."""
	run = run or subprocess.run
	uid = os.getuid() if uid is None else uid
	path = launch_agent_path()
	installed = os.path.exists(path)
	pid = launchd_state(run, uid)[1] if installed else None
	if pid is None:
		pid = foreground_pid()
	lines = [
		"Installed: yes ({})".format(path) if installed else "Installed: no",
		"Running: yes, PID {}".format(pid) if pid is not None else "Running: no",
		_qmp_line(qmp_address(config)),
		_battery_file_line(battery_file_path(config)),
	] + _camera_lines(config) + _share_lines(config, pid is not None)
	output.write("".join(line + "\n" for line in lines))
	return EXIT_RUNNING if pid is not None else EXIT_NOT_RUNNING


def run_agent_command(arguments, config, output, messages=None, wait=None):
	messages = messages or sys.stderr
	if arguments.once:
		status = battery_status()
		output.write(json.dumps(status, indent="\t") + "\n")
		output.flush()
		if config.battery_sync:
			messages.write(sync_once(battery_writer(config), status) + "\n")
	elif arguments.install:
		path = install_launch_agent(sys.executable, REPOSITORY_DIRECTORY)
		output.write("Installed {}. The agent now starts when you log in. Log: {}\n".format(path, os.path.expanduser(LOG_PATH)))
	elif arguments.uninstall:
		shares = shares_for_vm(config.shares, config.vm_name)
		if shares and os.path.exists(launch_agent_path()):
			output.write(_uninstall_share_warning(len(shares)))
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
