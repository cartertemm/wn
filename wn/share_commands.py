"""The share, list, and remove commands: checks, order of steps, rollback, and output."""

import dataclasses
import hashlib
import json
import os
import subprocess
import time

from wn.agent import LOG_PATH, foreground_pid, launch_agent_path, launchd_state
from wn.backend import BackendError
from wn.backends.utm import UnknownVMError, UTMCtlError, UTMCtlMissingError
from wn.config import load_config, update_config_file
from wn.downloads import RCLONE_WINDOWS_BINARY, WINFSP_SOURCE, fetch_share_tools
from wn.errors import WnError
from wn.guest_runner import BOM_TEXT, DEFAULT_SCRIPT_TIMEOUT_SECONDS, FileOffer, GuestRunner
from wn.setup import install_agent
from wn.share_sftp import (
	FETCH_SCRIPT,
	GUEST_CLIENT_KEY,
	GUEST_STATUS_FILE,
	GUEST_SUPERVISOR,
	PREPARE_SCRIPT,
	SUPERVISOR_SCRIPT,
	TASK_SCRIPT,
	WINDOWS_RCLONE_NAME,
	can_bind,
	ensure_keys,
	guest_script_bytes,
	guest_share_folder,
	install_binaries,
	load_guest_script,
	public_host_key,
	read_mac_state,
	render_known_hosts,
	render_rclone_conf,
	render_share_json,
	share_paths,
)
from wn.shares import (
	MAC_TO_VM,
	SHARE_COMMAND,
	Share,
	ShareError,
	check_mac_path,
	check_new_share,
	check_share_name,
	check_vm_path,
	default_share_name,
	normalize_vm_path,
	pick_port,
)

AGENT_PICKUP_TIMEOUT_SECONDS = 15
MOUNT_TIMEOUT_SECONDS = 30
STATUS_POLL_SECONDS = 1
FETCH_TIMEOUT_SECONDS = 300
WINFSP_INSTALL_TIMEOUT_SECONDS = 120
WRITE_BACK_SECONDS = 5
RESTART_NEEDED_EXIT_CODE = 3010
RUNNING_STATE = "running"
MOUNTED_STATE = "mounted"
SERVING_STATE = "serving"
UNKNOWN_STATE = "unknown"
WINFSP_OK = "ok"
FILES_PATH_STATE = "files"
JUNCTION_PATH_STATE = "junction"
FILE_PATH_STATE = "file"
MOUNT_LOG_TAIL_LINES = 20
RCLONE_CONF_NAME = "rclone.conf"
KNOWN_HOSTS_NAME = "known_hosts"
SHARE_JSON_NAME = "share.json"
MOUNT_LOG_NAME = "mount.log"
REMOVE_MARKER_NAME = "remove"
READ_WRITE_TEXT = "read and write"
READ_ONLY_TEXT = "read only"
GUEST_AGENT_DOWN_MESSAGE = "Windows is still starting (no answer from the guest agent). Try again in a minute"
WINFSP_NOTICE = "Uses WinFsp - Windows File System Proxy, Copyright (C) Bill Zissimopoulos, https://github.com/winfsp/winfsp"


def agent_is_installed():
	return os.path.exists(launch_agent_path())


def agent_is_running(run=None, uid=None):
	if agent_is_installed() and launchd_state(run or subprocess.run, os.getuid() if uid is None else uid)[1] is not None:
		return True
	return foreground_pid() is not None


def _sha256(data):
	return hashlib.sha256(data).hexdigest()


def _state_of(entry):
	return entry.get("state") if isinstance(entry, dict) else None


def _reported(entry):
	"""Return the guest's view of one share as "state, detail", or just the state when there is no detail."""
	if not isinstance(entry, dict):
		return UNKNOWN_STATE
	state = entry.get("state") or UNKNOWN_STATE
	detail = entry.get("detail")
	return "{}, {}".format(state, detail) if detail else state


class ShareCommands:
	"""Runs wn share. The collaborators are parameters so tests can replace them."""

	def __init__(self, config, backend, output, runner=None, paths=None, fetch_tools=fetch_share_tools,
			install_binaries=install_binaries, ensure_keys=ensure_keys, agent_installed=agent_is_installed,
			agent_running=agent_is_running, install_agent=install_agent, offer_factory=FileOffer,
			can_bind=can_bind, clock=time.monotonic, sleep=time.sleep):
		self.config = config
		self.backend = backend
		self.output = output
		self.runner = runner or GuestRunner(backend)
		self.paths = paths or share_paths()
		self.fetch_tools = fetch_tools
		self.install_binaries = install_binaries
		self.ensure_keys = ensure_keys
		self.agent_installed = agent_installed
		self.agent_running = agent_running
		self.install_agent = install_agent
		self.offer_factory = offer_factory
		self.can_bind = can_bind
		self.clock = clock
		self.sleep = sleep

	def _print(self, line):
		self.output.write(line + "\n")

	def _run_script(self, name, params, timeout):
		return self.runner.run_json(load_guest_script(name), params, timeout=timeout, label=name)

	def _check_vm(self, command):
		status = self.backend.status()
		if status.state != RUNNING_STATE:
			raise ShareError("VM '{}' is {}. Run wn start, wait until Windows has started, then run wn {} again".format(status.name, status.state, command))
		try:
			address = self.backend.ip()
		except UnknownVMError:
			raise
		except UTMCtlError:
			address = None
		if address is None:
			raise ShareError(GUEST_AGENT_DOWN_MESSAGE)

	def _guest_status(self):
		"""Return the shares object of the guest's status file, or None while it is missing or cannot be parsed."""
		try:
			status = json.loads(self.runner.pull(GUEST_STATUS_FILE).lstrip(BOM_TEXT))
		except (UnknownVMError, UTMCtlMissingError):
			raise
		except (BackendError, ValueError):
			return None
		shares = status.get("shares") if isinstance(status, dict) else None
		return shares if isinstance(shares, dict) else None

	def _write_shares(self, shares):
		update_config_file({"shares": [share.to_dict() for share in shares]})

	def _wait(self, read, ready, timeout):
		"""Call ``read`` every STATUS_POLL_SECONDS until ``ready`` accepts its value or ``timeout`` passes. Return the last value."""
		deadline = self.clock() + timeout
		value = read()
		while not ready(value) and self.clock() < deadline:
			self.sleep(STATUS_POLL_SECONDS)
			value = read()
		return value

	def _ensure_agent(self):
		if not self.agent_installed():
			path = self.install_agent()
			for line in (
				"Installed the wn agent and started it: {}".format(path),
				"It runs the Mac side of your shared folders.",
				"Log: {}".format(os.path.expanduser(LOG_PATH)),
				"To remove it: python3 -m wn agent --uninstall",
			):
				self._print(line)
		elif not self.agent_running():
			raise ShareError("the wn agent is installed but not running. Run python3 -m wn agent start, then run wn {} again".format(SHARE_COMMAND))

	def _refuse_guest_state(self, state, name, vm_path):
		if state["removing"]:
			raise ShareError("Windows is still removing a share named {}. Wait a minute and try again".format(name))
		if not state["parent_exists"]:
			raise ShareError("the folder that holds {} does not exist in Windows. Create it first".format(vm_path))
		problems = {
			FILES_PATH_STATE: "{} has files in Windows. Pick an empty or new folder",
			JUNCTION_PATH_STATE: "{} is already a mount point in Windows",
			FILE_PATH_STATE: "{} is a file in Windows",
		}
		if state["vm_path"] in problems:
			raise ShareError(problems[state["vm_path"]].format(vm_path))

	def _install_guest_tools(self, state, tools):
		"""Have Windows fetch rclone.exe and the WinFsp installer from the Mac when it lacks them. Return True when WinFsp was installed."""
		need_rclone = state["rclone_sha256"] != RCLONE_WINDOWS_BINARY.sha256
		need_winfsp = state["winfsp"] != WINFSP_OK
		if not (need_rclone or need_winfsp):
			return False
		files = {}
		if need_rclone:
			files[WINDOWS_RCLONE_NAME] = self.paths.windows_rclone
		if need_winfsp:
			files[WINFSP_SOURCE.file_name] = tools.winfsp_msi
		with self.offer_factory(files) as offer:
			params = {
				"rclone_url": offer.url(WINDOWS_RCLONE_NAME) if need_rclone else "",
				"rclone_sha256": RCLONE_WINDOWS_BINARY.sha256 if need_rclone else "",
				"winfsp_url": offer.url(WINFSP_SOURCE.file_name) if need_winfsp else "",
				"winfsp_sha256": WINFSP_SOURCE.sha256 if need_winfsp else "",
				"winfsp_name": WINFSP_SOURCE.file_name if need_winfsp else "",
			}
			result = self._run_script(FETCH_SCRIPT, params, FETCH_TIMEOUT_SECONDS + WINFSP_INSTALL_TIMEOUT_SECONDS)
		if result["mismatch"]:
			raise ShareError("the file Windows received does not match its SHA-256 ({}). Windows deleted it. Run wn {} again".format(result["mismatch"], SHARE_COMMAND))
		winfsp_exit = result["winfsp_exit"]
		if winfsp_exit == RESTART_NEEDED_EXIT_CODE:
			raise ShareError("WinFsp is installed but Windows needs a restart. Restart Windows, then run wn {} again".format(SHARE_COMMAND))
		if winfsp_exit:
			raise ShareError("WinFsp setup failed in Windows with exit code {}:\n".format(winfsp_exit) + "\n".join(result["winfsp_log"]))
		return need_winfsp and winfsp_exit == 0

	def _push_common_files(self, state):
		"""Push the client key and the supervisor script when Windows has other copies. Return True when the script changed."""
		with open(self.paths.client_key, "rb") as handle:
			client_key = handle.read()
		if state["client_key_sha256"] != _sha256(client_key):
			self.runner.push(GUEST_CLIENT_KEY, client_key)
		supervisor = guest_script_bytes(SUPERVISOR_SCRIPT)
		script_changed = state["supervisor_sha256"] != _sha256(supervisor)
		if script_changed:
			self.runner.push(GUEST_SUPERVISOR, supervisor)
		return script_changed

	def _wait_for_agent(self, name):
		entry = self._wait(lambda: read_mac_state(self.paths)["shares"].get(name), lambda entry: _state_of(entry) == SERVING_STATE, AGENT_PICKUP_TIMEOUT_SECONDS)
		if _state_of(entry) == SERVING_STATE:
			return
		if entry is None:
			raise ShareError("the wn agent did not pick up the share within {} s. Check python3 -m wn agent status. If you updated wn, run python3 -m wn agent --install so the agent runs the new code".format(AGENT_PICKUP_TIMEOUT_SECONDS))
		raise ShareError("the Mac side of the share is {}: {}".format(_state_of(entry), entry.get("detail")))

	def _wait_for_mount(self, name, folder):
		entry = self._wait(lambda: (self._guest_status() or {}).get(name), lambda entry: _state_of(entry) == MOUNTED_STATE, MOUNT_TIMEOUT_SECONDS)
		if _state_of(entry) == MOUNTED_STATE:
			return
		log_path = folder + "\\" + MOUNT_LOG_NAME
		try:
			log = self.runner.pull(log_path).lstrip(BOM_TEXT).replace("\r", "")
		except BackendError:
			log = ""
		lines = "\n".join(log.splitlines()[-MOUNT_LOG_TAIL_LINES:])
		raise ShareError("Windows did not mount the share within {} s. Windows reports: {}. Last lines of {}:\n{}".format(MOUNT_TIMEOUT_SECONDS, _reported(entry), log_path, lines))

	def _roll_back(self, name, folder, config_written):
		"""Ask Windows to remove the share and take it out of the config. Return what could not be done, as message lines."""
		problems = []
		try:
			self.runner.push(folder + "\\" + REMOVE_MARKER_NAME, b"")
		except WnError as error:
			problems.append("Also could not ask Windows to remove the share: {}".format(error))
		if config_written:
			try:
				self._write_shares(tuple(share for share in load_config().shares if share.name != name))
			except (WnError, OSError) as error:
				problems.append("Also could not remove share '{}' from the config: {}".format(name, error))
		return problems

	def add_share(self, mac_dir, vm_dir, read_only=False, name=None):
		mac_path = check_mac_path(mac_dir, read_only)
		vm_path = normalize_vm_path(vm_dir)
		check_vm_path(vm_path)
		name = name or default_share_name(mac_path)
		check_share_name(name)
		current = load_config().shares
		share = Share(name, self.config.vm_name, MAC_TO_VM, mac_path, vm_path, read_only)
		check_new_share(share, current)
		self._check_vm(SHARE_COMMAND)
		self._ensure_agent()
		tools = self.fetch_tools(report=self._print)
		self.install_binaries(tools, self.paths)
		self.ensure_keys(self.paths)
		share = dataclasses.replace(share, port=pick_port(current, self.can_bind))
		state = self._run_script(PREPARE_SCRIPT, {"name": name, "vm_path": vm_path}, DEFAULT_SCRIPT_TIMEOUT_SECONDS)
		self._refuse_guest_state(state, name, vm_path)
		installed_winfsp = self._install_guest_tools(state, tools)
		script_changed = self._push_common_files(state)
		folder = guest_share_folder(name)
		self.runner.push(folder + "\\" + RCLONE_CONF_NAME, render_rclone_conf(share).encode("utf-8"))
		self.runner.push(folder + "\\" + KNOWN_HOSTS_NAME, render_known_hosts(share, public_host_key(self.paths)).encode("utf-8"))
		config_written = False
		try:
			self.runner.push(folder + "\\" + SHARE_JSON_NAME, render_share_json(share).encode("utf-8"))
			self._write_shares(load_config().shares + (share,))
			config_written = True
			self._wait_for_agent(name)
			self._run_script(TASK_SCRIPT, {"restart": script_changed}, DEFAULT_SCRIPT_TIMEOUT_SECONDS)
			self._wait_for_mount(name, folder)
		except BaseException as error:
			problems = self._roll_back(name, folder, config_written)
			if problems and isinstance(error, WnError):
				raise ShareError("\n".join([str(error)] + problems)) from error
			raise
		self._print("{}: {} is now at {} in Windows, {}".format(name, mac_path, vm_path, READ_ONLY_TEXT if read_only else READ_WRITE_TEXT))
		self._print("Windows writes reach the Mac about {} s after a file is closed. See wn {} --help for the limits.".format(WRITE_BACK_SECONDS, SHARE_COMMAND))
		if installed_winfsp:
			self._print(WINFSP_NOTICE)
