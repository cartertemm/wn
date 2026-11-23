"""The share, list, and remove commands: checks, order of steps, rollback, and output."""

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import subprocess
import time

from wn.agent import LOG_PATH, foreground_pid, launch_agent_path, launchd_state
from wn.backend import BackendError
from wn.backends.utm import UnknownVMError, UTMCtlError, UTMCtlMissingError
from wn.config import REMOVE_SETTING, load_config, update_config_file
from wn.downloads import RCLONE_WINDOWS_BINARY, WINFSP_SOURCE, fetch_share_tools
from wn.errors import WnError
from wn.guest_runner import BOM_TEXT, DEFAULT_SCRIPT_TIMEOUT_SECONDS, FileOffer, GuestError, GuestRunner
from wn.setup import install_agent
from wn.share_sftp import (
	CLEANUP_SCRIPT,
	ERROR_STATE,
	FETCH_SCRIPT,
	GUEST_CLIENT_KEY,
	GUEST_STATUS_FILE,
	GUEST_SUPERVISOR,
	PREPARE_SCRIPT,
	SERVING_STATE,
	SHARE_PROTOCOL_VERSION,
	STATE_REFRESH_SECONDS,
	SUPERVISOR_SCRIPT,
	TASK_SCRIPT,
	UNPREPARE_SCRIPT,
	WINDOWS_RCLONE_NAME,
	can_bind,
	ensure_keys,
	guest_script_bytes,
	guest_share_folder,
	host_address_present,
	install_binaries,
	load_guest_script,
	new_host_key,
	public_host_key,
	read_mac_state,
	remove_file,
	remove_host_key,
	render_known_hosts,
	render_rclone_conf,
	render_share_json,
	seconds_since,
	share_paths,
)
from wn.shares import (
	HOST_ADDRESS,
	MAC_TO_VM,
	REMOVE_COMMAND,
	SHARE_COMMAND,
	Share,
	ShareError,
	check_mac_path,
	check_new_share,
	check_share_name,
	default_share_name,
	normalize_vm_path,
	pick_port,
	shares_for_vm,
)

AGENT_PICKUP_TIMEOUT_SECONDS = 30
AGENT_STATE_TIMEOUT_SECONDS = 5
AGENT_STATE_MAX_AGE_SECONDS = 3 * STATE_REFRESH_SECONDS
MOUNT_TIMEOUT_SECONDS = 90
MISSED_READS_AFTER_DEADLINE = 3
REMOVE_TIMEOUT_SECONDS = 180
REMOVE_PROGRESS_SECONDS = 30
ROLLBACK_REMOVE_TIMEOUT_SECONDS = 30
STATUS_POLL_SECONDS = 1
FETCH_TIMEOUT_SECONDS = 300
WINFSP_INSTALL_TIMEOUT_SECONDS = 120
WRITE_BACK_SECONDS = 5
RESTART_NEEDED_EXIT_CODE = 3010
RUNNING_STATE = "running"
MOUNTED_STATE = "mounted"
REMOVING_STATE = "removing"
BLOCKED_STATE = "blocked"
GUEST_ERROR_STATE = "error"
INCOMPLETE_STATE = "incomplete"
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
WINDOWS_ONLY_MARKER_NAME = "orphan"
DISCARD_MARKER_NAME = "discard"
GUEST_CACHE_FILES = "cache\\vfs\\share"
LOCK_FILE_NAME = "commands.lock"
READ_WRITE_TEXT = "read and write"
READ_ONLY_TEXT = "read only"
GUEST_AGENT_DOWN_MESSAGE = "Windows is still starting (no answer from the guest agent). Try again in a minute. If this keeps happening, install the UTM guest tools in Windows"
NO_HOST_ADDRESS_MESSAGE = "{} is not on this Mac. wn shares need the VM's network set to Shared Network in UTM. Change it in the VM's Network settings, restart the VM, then run wn {} again"
INSTALL_AGENT_COMMAND = "python3 -m wn agent --install"
SHARE_STOPPED_MESSAGE = "VM '{}' is {}. Run wn start, wait until Windows has started, then run wn {} again"
LOCKED_MESSAGE = "another wn share or wn unshare command is running. Wait for it to finish, then try again"
REMOVE_STOPPED_MESSAGE = "VM '{}' is {}. Start it with wn start and run wn {} again, so wn can remove the share from Windows too"
NOT_READ = object()
MACOS_PROTECTED_FOLDERS = (
	os.path.join("~", "Desktop"),
	os.path.join("~", "Documents"),
	os.path.join("~", "Downloads"),
	os.path.join("~", "Library", "Mobile Documents"),
	"/Volumes",
)
PRIVACY_NOTICE = "If macOS asks whether wn-agent may access {}, click Allow."
WINFSP_NOTICE = "Uses WinFsp - Windows File System Proxy, Copyright (C) Bill Zissimopoulos, https://github.com/winfsp/winfsp"


def agent_is_installed():
	return os.path.exists(launch_agent_path())


def agent_is_running(run=None, uid=None):
	if agent_is_installed() and launchd_state(run or subprocess.run, os.getuid() if uid is None else uid)[1] is not None:
		return True
	return foreground_pid() is not None


def macos_may_ask(mac_path):
	"""Return True when ``mac_path`` is in a folder where macOS asks the user before wn-agent can read it."""
	path = mac_path.casefold()
	for folder in MACOS_PROTECTED_FOLDERS:
		root = os.path.realpath(os.path.expanduser(folder)).casefold()
		if path == root or path.startswith(root + os.sep):
			return True
	return False


def _sha256(data):
	return hashlib.sha256(data).hexdigest()


def _state_of(entry):
	return entry.get("state") if isinstance(entry, dict) else None


def _status_key(shares, name):
	"""Return the key of share ``name`` in the guest's shares object, or None. The guest uses the folder name on disk, whose letter case can differ."""
	return next((key for key in shares if key.casefold() == name.casefold()), None)


def _status_entry(shares, name):
	return shares.get(_status_key(shares, name))


def _is_stuck(entry):
	"""Return True when the guest reports a state that does not fix itself."""
	return _state_of(entry) == BLOCKED_STATE or (_state_of(entry) == GUEST_ERROR_STATE and bool(entry.get("detail")))


def _only_refused_uploads(entry):
	"""Return True when every file Windows still has to upload is one the Mac refuses."""
	failed = entry.get("failed")
	to_upload = entry.get("to_upload")
	return isinstance(failed, int) and isinstance(to_upload, int) and failed >= 1 and failed >= to_upload


def _file_count(count):
	return "1 file" if count == 1 else "{} files".format(count)


def _listed_shares(guest):
	"""Return the names of the shares that Windows lists, leaving out the folders that never got a share.json."""
	return sorted(key for key, entry in guest.items() if _state_of(entry) != INCOMPLETE_STATE)


def _windows_only_shares(guest, own):
	names = {share.name.casefold() for share in own}
	return [key for key in _listed_shares(guest) if key.casefold() not in names]


def _reported(entry):
	"""Return the guest's view of one share as "state, detail", or just the state when there is no detail."""
	if not isinstance(entry, dict):
		return UNKNOWN_STATE
	state = entry.get("state") or UNKNOWN_STATE
	detail = entry.get("detail")
	return "{}, {}".format(state, detail) if detail else state


class ShareCommands:
	"""Runs the share commands. The collaborators are parameters so tests can replace them."""

	def __init__(self, config, backend, output, runner=None, paths=None, fetch_tools=fetch_share_tools,
			install_binaries=install_binaries, ensure_keys=ensure_keys, new_host_key=new_host_key, agent_installed=agent_is_installed,
			agent_running=agent_is_running, install_agent=install_agent, offer_factory=FileOffer,
			can_bind=can_bind, address_present=host_address_present, clock=time.monotonic, sleep=time.sleep):
		self.config = config
		self.backend = backend
		self.output = output
		self.runner = runner or GuestRunner(backend)
		self.paths = paths or share_paths()
		self.fetch_tools = fetch_tools
		self.install_binaries = install_binaries
		self.ensure_keys = ensure_keys
		self.new_host_key = new_host_key
		self.agent_installed = agent_installed
		self.agent_running = agent_running
		self.install_agent = install_agent
		self.offer_factory = offer_factory
		self.can_bind = can_bind
		self.address_present = address_present
		self.clock = clock
		self.sleep = sleep

	def _print(self, line):
		self.output.write(line + "\n")

	def _run_script(self, name, params, timeout):
		return self.runner.run_json(load_guest_script(name), params, timeout=timeout, label=name)

	def _guest_agent_answers(self):
		try:
			return self.backend.ip() is not None
		except UnknownVMError:
			raise
		except UTMCtlError:
			return False

	def _check_vm(self, command, stopped_message=SHARE_STOPPED_MESSAGE):
		status = self.backend.status()
		if status.state != RUNNING_STATE:
			raise ShareError(stopped_message.format(status.name, status.state, command))
		if not self._guest_agent_answers():
			raise ShareError(GUEST_AGENT_DOWN_MESSAGE)

	def _read_guest_status(self):
		"""Return the shares object of the guest's status file and its updated time, or None and None while it is missing or cannot be parsed."""
		try:
			status = json.loads(self.runner.pull(GUEST_STATUS_FILE).lstrip(BOM_TEXT))
		except (UnknownVMError, UTMCtlMissingError):
			raise
		except (BackendError, ValueError):
			return None, None
		shares = status.get("shares") if isinstance(status, dict) else None
		if not isinstance(shares, dict):
			return None, None
		return shares, status.get("updated")

	def _guest_status(self):
		return self._read_guest_status()[0]

	@contextlib.contextmanager
	def _command_lock(self):
		os.makedirs(self.paths.root, exist_ok=True)
		with open(os.path.join(self.paths.root, LOCK_FILE_NAME), "a") as handle:
			try:
				fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
			except BlockingIOError:
				raise ShareError(LOCKED_MESSAGE) from None
			yield

	def _write_shares(self, shares):
		update_config_file({"shares": [share.to_dict() for share in shares] if shares else REMOVE_SETTING})

	def _wait(self, read, done, timeout, progress=None):
		"""Call ``read`` every STATUS_POLL_SECONDS until ``done`` accepts its value. Give up only at a good read, one that is not None, that began after the deadline, or after MISSED_READS_AFTER_DEADLINE bad reads past it. A change in ``progress`` between good reads moves the deadline to ``timeout`` from then. Return the last good value."""
		deadline = self.clock() + timeout
		value = None
		last_progress = NOT_READ
		missed = 0
		while True:
			began = self.clock()
			answer = read()
			if answer is not None:
				value = answer
				if done(value):
					return value
				if progress is not None:
					key = progress(value)
					if last_progress is not NOT_READ and key != last_progress:
						deadline = began + timeout
					last_progress = key
				if began >= deadline:
					return value
			elif began >= deadline:
				missed += 1
				if missed >= MISSED_READS_AFTER_DEADLINE:
					return value
			self.sleep(STATUS_POLL_SECONDS)

	def _agent_serves_shares(self, state):
		"""Return True when the agent's state file comes from a share supervisor of this wn that wrote it lately."""
		age = seconds_since(state.get("updated"), time.time())
		return state.get("protocol") == SHARE_PROTOCOL_VERSION and age is not None and age < AGENT_STATE_MAX_AGE_SECONDS

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
			config = load_config()
			if config.battery_sync or config.camera or shares_for_vm(config.shares, config.vm_name):
				raise ShareError("the wn agent is installed but not running. Run wn agent start, then run wn {} again".format(SHARE_COMMAND))
		elif not self._agent_serves_shares(self._wait(lambda: read_mac_state(self.paths), self._agent_serves_shares, AGENT_STATE_TIMEOUT_SECONDS)):
			path = self.install_agent()
			self._print("The running wn agent is older than this wn and cannot serve shares. Installed the new agent and restarted it: {}".format(path))

	def _prepare_guest(self, name, vm_path):
		"""Run prepare.ps1. Windows removes a share folder in the supervisor's next pass, after its uploads, so make sure the supervisor runs."""
		state = self._run_script(PREPARE_SCRIPT, {"name": name, "vm_path": vm_path}, DEFAULT_SCRIPT_TIMEOUT_SECONDS)
		if state["removing"]:
			self._run_script(TASK_SCRIPT, {"restart": False}, DEFAULT_SCRIPT_TIMEOUT_SECONDS)
			raise ShareError("Windows is still removing a share named {}. Wait a minute and run wn {} again".format(name, SHARE_COMMAND))
		return state

	def _refuse_guest_state(self, state, vm_path):
		if not state["parent_exists"]:
			raise ShareError("the folder that holds {} does not exist in Windows. Create it first".format(vm_path))
		if state["linked_parent"]:
			raise ShareError("{} in Windows is a link or mount point. Pick a path with no link in it".format(state["linked_parent"]))
		if state["system_folder"]:
			raise ShareError("{} is inside {} in Windows. Pick a folder outside it".format(vm_path, state["system_folder"]))
		problems = {
			FILES_PATH_STATE: "{} has files in Windows. Pick an empty or new folder",
			JUNCTION_PATH_STATE: "{} is already a mount point in Windows",
			FILE_PATH_STATE: "{} is a file in Windows",
		}
		if state["vm_path"] in problems:
			raise ShareError(problems[state["vm_path"]].format(vm_path))

	def _install_guest_tools(self, state, tools):
		"""Have Windows fetch rclone.exe and the WinFsp installer from the Mac when it lacks them."""
		need_rclone = state["rclone_sha256"] != RCLONE_WINDOWS_BINARY.sha256
		need_winfsp = state["winfsp"] != WINFSP_OK
		if not (need_rclone or need_winfsp):
			return
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
		if winfsp_exit and winfsp_exit != RESTART_NEEDED_EXIT_CODE:
			raise ShareError("WinFsp setup failed in Windows with exit code {}:\n".format(winfsp_exit) + "\n".join(result["winfsp_log"]))
		if need_winfsp:
			self._print(WINFSP_NOTICE)
		if winfsp_exit == RESTART_NEEDED_EXIT_CODE:
			raise ShareError("WinFsp is installed but Windows needs a restart. Restart Windows, then run wn {} again".format(SHARE_COMMAND))

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
		shares = self._wait(lambda: read_mac_state(self.paths)["shares"], lambda shares: _state_of(shares.get(name)) in (SERVING_STATE, ERROR_STATE), AGENT_PICKUP_TIMEOUT_SECONDS)
		entry = shares.get(name)
		if _state_of(entry) == SERVING_STATE:
			return
		if entry is None:
			raise ShareError("the wn agent did not pick up the share within {} s. Check wn agent status. If you updated wn, run python3 -m wn agent --install so the agent runs the new code".format(AGENT_PICKUP_TIMEOUT_SECONDS))
		raise ShareError("the wn agent did not start serving the share. It reports: {}".format(_reported(entry)))

	def _wait_for_mount(self, name, folder):
		guest = self._wait(self._guest_status, lambda guest: _state_of(_status_entry(guest, name)) == MOUNTED_STATE or _is_stuck(_status_entry(guest, name)),
			MOUNT_TIMEOUT_SECONDS, lambda guest: _state_of(_status_entry(guest, name)))
		entry = _status_entry(guest or {}, name)
		if _state_of(entry) == MOUNTED_STATE:
			return
		if _is_stuck(entry):
			raise ShareError("Windows cannot mount the share. Windows reports: {}".format(_reported(entry)))
		log_path = folder + "\\" + MOUNT_LOG_NAME
		try:
			log = self.runner.pull(log_path).lstrip(BOM_TEXT).replace("\r", "")
		except BackendError:
			log = ""
		lines = "\n".join(log.splitlines()[-MOUNT_LOG_TAIL_LINES:])
		raise ShareError("Windows did not mount the share within {} s. Windows reports: {}. Last lines of {}:\n{}".format(MOUNT_TIMEOUT_SECONDS, _reported(entry), log_path, lines))

	def _roll_back(self, name, folder, prepared, share_file_sent, config_written, task_started):
		"""Undo what wn share did so far: remove the empty share folder or ask Windows to remove the share, take it out of the config, delete its host key, and after the last share remove the wn files from Windows. Return what could not be done, as message lines."""
		problems = []
		if prepared and not share_file_sent:
			try:
				self._run_script(UNPREPARE_SCRIPT, {"name": name}, DEFAULT_SCRIPT_TIMEOUT_SECONDS)
			except WnError as error:
				problems.append("Also could not remove the empty share folder {} from Windows: {}".format(folder, error))
		if share_file_sent:
			try:
				self.runner.push(folder + "\\" + REMOVE_MARKER_NAME, b"")
			except WnError as error:
				if config_written:
					return ["Also could not ask Windows to remove the share: {}. wn kept the share in the config. Run wn {} {} to finish".format(error, REMOVE_COMMAND, name)]
				problems.append("Also could not ask Windows to remove the share: {}".format(error))
		if config_written:
			try:
				self._write_shares(tuple(share for share in load_config().shares if share.name != name))
			except (WnError, OSError) as error:
				problems.append("Also could not remove share '{}' from the config: {}".format(name, error))
				return problems
			self._remove_share_files(name)
		else:
			remove_host_key(self.paths, name)
		if task_started and not shares_for_vm(load_config().shares, self.config.vm_name):
			problems.extend(self._clean_up_after_a_rollback(name))
		return problems

	def _clean_up_after_a_rollback(self, name):
		"""Run the last share cleanup once Windows has dropped the rolled back share and lists no other. Return what could not be done, as message lines."""
		try:
			guest = self._wait(self._guest_status, lambda guest: _status_key(guest, name) is None, ROLLBACK_REMOVE_TIMEOUT_SECONDS)
			if guest is not None and not _listed_shares(guest):
				self._run_script(CLEANUP_SCRIPT, {}, DEFAULT_SCRIPT_TIMEOUT_SECONDS)
		except WnError as error:
			return ["Also could not remove the wn share files and the wn-shares task from Windows: {}".format(error)]
		return []

	def add_share(self, mac_dir, vm_dir, read_only=False, name=None):
		with self._command_lock():
			self._add_share(mac_dir, vm_dir, read_only, name)

	def _add_share(self, mac_dir, vm_dir, read_only, name):
		mac_path = check_mac_path(mac_dir, read_only)
		vm_path = normalize_vm_path(vm_dir)
		name = name or default_share_name(mac_path)
		check_share_name(name)
		current = load_config().shares
		share = Share(name, self.config.vm_name, MAC_TO_VM, mac_path, vm_path, read_only)
		check_new_share(share, current)
		self._check_vm(SHARE_COMMAND)
		if not self.address_present():
			raise ShareError(NO_HOST_ADDRESS_MESSAGE.format(HOST_ADDRESS, SHARE_COMMAND))
		self._ensure_agent()
		tools = self.fetch_tools(report=self._print)
		self.install_binaries(tools, self.paths)
		self.ensure_keys(self.paths)
		share = dataclasses.replace(share, port=pick_port(current, self.can_bind))
		folder = guest_share_folder(name)
		state = None
		share_file_sent = False
		config_written = False
		task_started = False
		try:
			state = self._prepare_guest(name, vm_path)
			self._refuse_guest_state(state, vm_path)
			self._install_guest_tools(state, tools)
			script_changed = self._push_common_files(state)
			self.new_host_key(self.paths, name)
			self.runner.push(folder + "\\" + RCLONE_CONF_NAME, render_rclone_conf(share).encode("utf-8"))
			self.runner.push(folder + "\\" + KNOWN_HOSTS_NAME, render_known_hosts(share, public_host_key(self.paths, name)).encode("utf-8"))
			share_file_sent = True
			self.runner.push(folder + "\\" + SHARE_JSON_NAME, render_share_json(share).encode("utf-8"))
			self._write_shares(load_config().shares + (share,))
			config_written = True
			if macos_may_ask(mac_path):
				self._print(PRIVACY_NOTICE.format(mac_path))
			self._wait_for_agent(name)
			task_started = True
			self._run_script(TASK_SCRIPT, {"restart": script_changed}, DEFAULT_SCRIPT_TIMEOUT_SECONDS)
			self._wait_for_mount(name, folder)
		except BaseException as error:
			prepared = state is not None or isinstance(error, KeyboardInterrupt)
			problems = self._roll_back(name, folder, prepared, share_file_sent, config_written, task_started)
			if problems and isinstance(error, WnError):
				raise ShareError("\n".join([str(error)] + problems)) from error
			raise
		self._print("{}: {} is now at {} in Windows, {}".format(name, mac_path, vm_path, READ_ONLY_TEXT if read_only else READ_WRITE_TEXT))
		self._print("Windows writes reach the Mac about {} s after a file is closed. See wn {} --help for the limits.".format(WRITE_BACK_SECONDS, SHARE_COMMAND))

	def _guest_view(self):
		"""Return the guest's shares object, or None and the text that says why Windows' view is unknown."""
		try:
			status = self.backend.status()
			if status.state != RUNNING_STATE:
				return None, "{}, the VM is {}".format(UNKNOWN_STATE, status.state)
			if not self._guest_agent_answers():
				return None, "{}, no answer from the guest agent".format(UNKNOWN_STATE)
			guest = self._guest_status()
		except UnknownVMError:
			return None, "{}, VM '{}' is not in UTM".format(UNKNOWN_STATE, self.config.vm_name)
		except BackendError as error:
			return None, "{}, {}".format(UNKNOWN_STATE, error)
		if guest is None:
			return None, "{}, Windows has no share status yet".format(UNKNOWN_STATE)
		return guest, None

	def list_shares(self):
		shares = load_config().shares
		own = shares_for_vm(shares, self.config.vm_name)
		guest, unknown_text = self._guest_view()
		windows_only = _windows_only_shares(guest or {}, own)
		if not shares and not windows_only:
			self._print("No shares. Add one with wn {} <dir-on-mac> <dir-on-vm>".format(SHARE_COMMAND))
			return
		if own:
			mac_state = read_mac_state(self.paths)["shares"]
			agent_installed = self.agent_installed()
			agent_running = self.agent_running()
		for share in shares:
			if share not in own:
				self._print("{}: belongs to VM '{}'".format(share.name, share.vm))
				continue
			mac_entry = mac_state.get(share.name)
			if not agent_running and not agent_installed:
				mac_text = "the agent is not installed. Run {}".format(INSTALL_AGENT_COMMAND)
			elif not agent_running:
				mac_text = "the agent is not running. Run wn agent start"
			elif mac_entry is None:
				mac_text = "no state yet"
			elif _state_of(mac_entry) == SERVING_STATE:
				mac_text = "serving on port {}".format(share.port)
			else:
				mac_text = _reported(mac_entry)
			if guest is None:
				windows_text = unknown_text
			elif _status_key(guest, share.name) is not None:
				windows_text = _reported(_status_entry(guest, share.name))
			else:
				windows_text = "not set up"
			self._print("{}: Mac folder {} in Windows at {}, {}. Mac: {}. Windows: {}.".format(
				share.name, share.mac_path, share.vm_path, READ_ONLY_TEXT if share.read_only else READ_WRITE_TEXT, mac_text, windows_text))
		for key in windows_only:
			path = guest[key].get("path")
			self._print("{}: exists only in Windows{}. The Mac has no share by this name, so this folder does not work in Windows. Run wn {} {} to remove it from Windows.".format(
				key, ", at {}".format(path) if path else "", REMOVE_COMMAND, key))

	def _find_share(self, name):
		"""Return the config entry of share ``name``, or None and the guest's status when only Windows has it."""
		shares = load_config().shares
		for share in shares:
			if share.name.casefold() == name.casefold():
				if share.vm != self.config.vm_name:
					raise ShareError("share '{}' belongs to VM '{}'. Set vm_name to '{}' in the config to remove it".format(share.name, share.vm, share.vm))
				return share, None
		guest = self._guest_view()[0]
		if guest is not None and _status_key(guest, name) in _listed_shares(guest):
			return None, guest
		if not shares:
			raise ShareError("no share named '{}'. There are no shares".format(name))
		raise ShareError("no share named '{}'. Shares: {}".format(name, ", ".join(share.name for share in shares)))

	def _remove_share_files(self, name):
		remove_file(self.paths.log_file(name))
		remove_host_key(self.paths, name)

	def _remove_mac_side(self, name):
		self._write_shares(tuple(share for share in load_config().shares if share.name != name))
		self._remove_share_files(name)

	def _ask_windows_to_remove(self, name):
		"""Push the remove marker. A failed push is fine when Windows has already dropped the share."""
		try:
			self.runner.push(guest_share_folder(name) + "\\" + REMOVE_MARKER_NAME, b"")
		except (UnknownVMError, UTMCtlMissingError):
			raise
		except (BackendError, GuestError):
			guest = self._guest_status()
			if guest is None or _status_key(guest, name) is not None:
				raise

	def _wait_for_removal(self, name, windows_only=False, discard=False):
		"""Wait until a good status read no longer lists the share, and return that status. While Windows reports the share as removing and its status file changes, wait without limit."""
		deadline = self.clock() + REMOVE_TIMEOUT_SECONDS
		next_progress = self.clock()
		last_updated = None
		while True:
			guest, updated = self._read_guest_status()
			if guest is not None and _status_key(guest, name) is None:
				return guest
			entry = _status_entry(guest, name) if guest is not None else None
			if _state_of(entry) == BLOCKED_STATE and entry.get("remove_requested"):
				raise ShareError("Windows cannot remove the share: {}. Windows must mount the share once more to upload the files in its cache to the Mac. Fix that, then run wn {} {} again".format(entry.get("detail"), REMOVE_COMMAND, name))
			unsaved = entry.get("unsaved") if _state_of(entry) == REMOVING_STATE else None
			if isinstance(unsaved, int) and unsaved >= 1 and not discard:
				raise ShareError("Windows has {} in the cache of share {} that never reached the Mac. The Mac has no share named {}, so these files cannot be uploaded, and wn did not remove the share. To keep them, copy them in Windows as an administrator from {}. Then run wn {} {} --discard, which removes the share and deletes these files from Windows.".format(
					_file_count(unsaved), name, name, guest_share_folder(name) + "\\" + GUEST_CACHE_FILES, REMOVE_COMMAND, name))
			if _state_of(entry) == REMOVING_STATE and _only_refused_uploads(entry):
				raise ShareError("Windows cannot upload some files to the Mac, so wn did not remove the share. Windows reports: {}. Fix the cause on the Mac (a locked file, permissions, or a full disk), then run wn {} {} again. Windows keeps the files and uploads them when it can, which can take a few minutes after the fix".format(entry.get("detail"), REMOVE_COMMAND, name))
			if _state_of(entry) == REMOVING_STATE:
				if updated != last_updated:
					deadline = self.clock() + REMOVE_TIMEOUT_SECONDS
					last_updated = updated
				if self.clock() >= next_progress and not windows_only:
					pending = entry.get("pending")
					detail = entry.get("detail")
					waiting = "Waiting for Windows to upload {} {} to the Mac".format(pending, "file" if pending == 1 else "files") if isinstance(pending, int) else "Waiting for Windows to finish its uploads to the Mac" + (" ({})".format(detail) if detail else "")
					self._print("{}. Press Ctrl+C to stop waiting. If you stop, Windows still removes the share after its uploads. Run wn {} {} again to remove it from the Mac.".format(waiting, REMOVE_COMMAND, name))
					next_progress = self.clock() + REMOVE_PROGRESS_SECONDS
			if self.clock() >= deadline:
				raise ShareError("Windows did not remove the share within {} s. Windows reports: {}. Run wn {} {} again later".format(REMOVE_TIMEOUT_SECONDS, _reported(entry), REMOVE_COMMAND, name))
			self.sleep(STATUS_POLL_SECONDS)

	def remove_share(self, name, discard=False):
		with self._command_lock():
			self._remove_share(name, discard)

	def _remove_share(self, name, discard):
		share, guest = self._find_share(name)
		if share is None:
			self._remove_windows_only(_status_key(guest, name), guest, discard)
			return
		if discard:
			raise ShareError("--discard only applies to a share that exists only in Windows. {} is shared from the Mac, so run wn {} {} without --discard".format(share.name, REMOVE_COMMAND, share.name))
		try:
			self._check_vm(REMOVE_COMMAND, REMOVE_STOPPED_MESSAGE)
		except UnknownVMError:
			self._remove_mac_side(share.name)
			self._print("VM '{}' no longer exists in UTM, so only the Mac side was removed".format(self.config.vm_name))
			return
		if not self.agent_running() and not self.agent_installed():
			raise ShareError("the wn agent is not installed, so Windows cannot upload its last changes to the Mac. Run {}, then run wn {} {} again".format(INSTALL_AGENT_COMMAND, REMOVE_COMMAND, share.name))
		if not self.agent_running():
			raise ShareError("the wn agent is not running, so Windows cannot upload its last changes to the Mac. Run wn agent start, then run wn {} {} again".format(REMOVE_COMMAND, share.name))
		self._ask_windows_to_remove(share.name)
		self._run_script(TASK_SCRIPT, {"restart": False}, DEFAULT_SCRIPT_TIMEOUT_SECONDS)
		guest = self._wait_for_removal(share.name)
		self._remove_mac_side(share.name)
		self._print("Removed {}. {} is gone from Windows. The files stay in {} on the Mac.".format(share.name, share.vm_path, share.mac_path))
		self._clean_up_after_the_last_share(guest)

	def _remove_windows_only(self, key, guest, discard):
		"""Remove a share that Windows has and the config does not. It has no Mac server, so Windows keeps files that never reached the Mac unless ``discard`` is set."""
		folder = guest_share_folder(key)
		if discard:
			self.runner.push(folder + "\\" + DISCARD_MARKER_NAME, b"")
		self.runner.push(folder + "\\" + WINDOWS_ONLY_MARKER_NAME, b"")
		self._ask_windows_to_remove(key)
		self._run_script(TASK_SCRIPT, {"restart": False}, DEFAULT_SCRIPT_TIMEOUT_SECONDS)
		path = guest[key].get("path")
		guest = self._wait_for_removal(key, windows_only=True, discard=discard)
		self._remove_share_files(key)
		self._print("Removed {} from Windows.{}".format(key, " {} is gone from Windows.".format(path) if path else ""))
		self._clean_up_after_the_last_share(guest)

	def _clean_up_after_the_last_share(self, guest):
		if shares_for_vm(load_config().shares, self.config.vm_name):
			return
		others = _listed_shares(guest)
		if others:
			self._print("Windows still lists other wn shares ({}), so wn kept its share files and the wn-shares task in Windows.".format(", ".join(others)))
			return
		result = self._run_script(CLEANUP_SCRIPT, {}, DEFAULT_SCRIPT_TIMEOUT_SECONDS)
		if result["cleaned"]:
			self._print("Removed the wn share files and the wn-shares task from Windows. WinFsp stays installed. To remove it, open Settings > Apps in Windows.")
		else:
			self._print("Windows still has files to upload to the Mac in {}, so wn kept its share files and the wn-shares task in Windows.".format(", ".join(result["shares_with_cache"])))
