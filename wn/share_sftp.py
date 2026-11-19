"""Mac folder into Windows over rclone SFTP: Mac files, the server command, guest file contents, and the server supervisor."""

import dataclasses
import datetime
import glob
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from typing import Optional

from wn.config import config_path, load_config
from wn.downloads import RCLONE_MAC_BINARY, RCLONE_WINDOWS_BINARY, extract_verified_member
from wn.errors import WnError
from wn.shares import GUEST_ROOT, HOST_ADDRESS, REMOVE_COMMAND, SHARE_COMMAND, SUPPORT_DIRECTORY, Share, ShareError, shares_for_vm

SHARES_DIRECTORY_NAME = "shares"
LOGS_DIRECTORY = os.path.join("~", "Library", "Logs")
LOG_NAME = "wn-share-{}.log"
PID_SUFFIX = ".pid"
HOST_KEY_NAME = "host_ed25519-{}"
CLIENT_KEY_NAME = "client_ed25519"
PUBLIC_KEY_SUFFIX = ".pub"
EMPTY_CONFIG_NAME = "empty-rclone.conf"
BIN_DIRECTORY_NAME = "bin"
MAC_RCLONE_NAME = "rclone"
WINDOWS_RCLONE_NAME = "rclone.exe"
STATE_FILE_NAME = "state.json"
SSH_KEYGEN_PATH = "/usr/bin/ssh-keygen"
KEY_COMMENT = "wn-share"
DIRECTORY_MODE = 0o700
PRIVATE_MODE = 0o600
PUBLIC_MODE = 0o644
EXECUTABLE_MODE = 0o755
DIR_CACHE_TIME = "1s"
SFTP_USER = "wn"
PUBLIC_KEY_FIELD_COUNT = 2
GUEST_SCRIPTS_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guest", "shares")
GUEST_SHARES_DIRECTORY = GUEST_ROOT + "\\shares"
GUEST_CLIENT_KEY = GUEST_ROOT + "\\keys\\client_ed25519"
GUEST_SUPERVISOR = GUEST_ROOT + "\\wn-shares.ps1"
GUEST_STATUS_FILE = GUEST_ROOT + "\\status.json"
PREPARE_SCRIPT = "prepare.ps1"
FETCH_SCRIPT = "fetch.ps1"
TASK_SCRIPT = "task.ps1"
CLEANUP_SCRIPT = "cleanup.ps1"
SUPERVISOR_SCRIPT = "wn-shares.ps1"
RCLONE_CONF_TEMPLATE = (
	"[share]\n"
	"type = sftp\n"
	"host = {host}\n"
	"port = {port}\n"
	"user = {user}\n"
	"key_file = {key_file}\n"
	"known_hosts_file = {known_hosts}\n"
	"shell_type = unix\n"
	"md5sum_command = md5sum\n"
	"sha1sum_command = sha1sum\n"
)
SUPERVISOR_TICK_SECONDS = 2
SHARE_PROTOCOL_VERSION = 1
STATE_REFRESH_SECONDS = 10
SERVER_RESTART_SECONDS = 5
SERVER_STOP_SECONDS = 3
ORPHAN_POLL_SECONDS = 0.1
WAKE_GAP_SECONDS = 5
MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_TAIL_BYTES = 4096
LOG_TIME_PREFIX = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} ")
CONNECT_TIMEOUT_SECONDS = 0.5
PS_PATH = "/bin/ps"
SERVE_SFTP_TEXT = "serve sftp"
SHARE_LOG_PREFIX = "share {}: "
SUPERVISOR_LOG_PREFIX = "shares: "
SERVING_STATE = "serving"
STARTING_STATE = "starting"
WAITING_STATE = "waiting"
RESTARTING_STATE = "restarting"
ERROR_STATE = "error"
NETWORK_DOWN_DETAIL = "the VM network is not up"
MISSING_FOLDER_DETAIL = "{} is missing"
NOT_ALLOWED_DETAIL = "macOS does not allow wn-agent to read {}. Allow it in System Settings > Privacy & Security > Files and Folders > wn-agent"
UNREADABLE_DETAIL = "cannot read {}: {}"
NOT_RESPONDING_DETAIL = "{} does not respond"
CHECKING_DETAIL = "checking {}"
FOLDER_CHECK_SECONDS = 30
FOLDER_CHECK_TIMEOUT_SECONDS = 10
THREAD_STOP_SECONDS = 10
CHECK_PENDING = object()
HOST_KEY_MISSING_DETAIL = "its host key {key} is missing. Run wn {remove} {name}, then wn {share} again"
PART_SUFFIX = ".part"
UTC_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
CONFIG_NOT_READ = -1


@dataclasses.dataclass(frozen=True)
class SharePaths:
	root: str
	logs: str

	@property
	def client_key(self):
		return os.path.join(self.root, CLIENT_KEY_NAME)

	@property
	def client_public_key(self):
		return self.client_key + PUBLIC_KEY_SUFFIX

	@property
	def empty_config(self):
		return os.path.join(self.root, EMPTY_CONFIG_NAME)

	@property
	def mac_rclone(self):
		return os.path.join(self.root, BIN_DIRECTORY_NAME, MAC_RCLONE_NAME)

	@property
	def windows_rclone(self):
		return os.path.join(self.root, BIN_DIRECTORY_NAME, WINDOWS_RCLONE_NAME)

	@property
	def state_file(self):
		return os.path.join(self.root, STATE_FILE_NAME)

	def host_key(self, name):
		return os.path.join(self.root, HOST_KEY_NAME.format(name))

	def host_public_key(self, name):
		return self.host_key(name) + PUBLIC_KEY_SUFFIX

	def pid_file(self, name):
		return os.path.join(self.root, name + PID_SUFFIX)

	def log_file(self, name):
		return os.path.join(self.logs, LOG_NAME.format(name))


def share_paths():
	return SharePaths(os.path.expanduser(os.path.join(SUPPORT_DIRECTORY, SHARES_DIRECTORY_NAME)), os.path.expanduser(LOGS_DIRECTORY))


def server_command(share, paths):
	"""Return the argv of the rclone SFTP server for ``share``. Never add --pass, --links, -L, --rc or --sftp-chunk-size."""
	command = [
		paths.mac_rclone, "serve", "sftp", share.mac_path,
		"--config", paths.empty_config,
		"--addr", "{}:{}".format(HOST_ADDRESS, share.port),
		"--key", paths.host_key(share.name),
		"--authorized-keys", paths.client_public_key,
		"--dir-cache-time", DIR_CACHE_TIME,
		"-v",
		"--log-file", paths.log_file(share.name),
	]
	if share.read_only:
		command.append("--read-only")
	return command


def guest_share_folder(name):
	return "{}\\{}".format(GUEST_SHARES_DIRECTORY, name)


def render_rclone_conf(share):
	folder = guest_share_folder(share.name)
	return RCLONE_CONF_TEMPLATE.format(host=HOST_ADDRESS, port=share.port, user=SFTP_USER, key_file=GUEST_CLIENT_KEY, known_hosts=folder + "\\known_hosts")


def render_known_hosts(share, host_public_key):
	return "{host} {key}\n[{host}]:{port} {key}\n".format(host=HOST_ADDRESS, port=share.port, key=host_public_key)


def render_share_json(share):
	return json.dumps({"name": share.name, "mount_path": share.vm_path, "read_only": share.read_only, "port": share.port}, ensure_ascii=True)


def ensure_share_directory(paths):
	os.makedirs(paths.root, DIRECTORY_MODE, exist_ok=True)
	os.chmod(paths.root, DIRECTORY_MODE)
	if not os.path.exists(paths.empty_config):
		os.close(os.open(paths.empty_config, os.O_WRONLY | os.O_CREAT, PRIVATE_MODE))


def _ssh_keygen(run, *arguments):
	result = run([SSH_KEYGEN_PATH, *arguments], capture_output=True, text=True)
	if result.returncode != 0:
		raise ShareError("ssh-keygen failed with exit code {}: {}".format(result.returncode, result.stderr.strip()))
	return result.stdout


def _generate_key(run, key_path):
	_ssh_keygen(run, "-q", "-t", "ed25519", "-N", "", "-C", KEY_COMMENT, "-f", key_path)
	os.chmod(key_path, PRIVATE_MODE)
	os.chmod(key_path + PUBLIC_KEY_SUFFIX, PUBLIC_MODE)


def ensure_keys(paths, run=subprocess.run):
	"""Make the client key that every share uses, when it is missing."""
	ensure_share_directory(paths)
	key_path = paths.client_key
	public_path = paths.client_public_key
	if not os.path.exists(key_path):
		_generate_key(run, key_path)
	elif not os.path.exists(public_path):
		public_key = _ssh_keygen(run, "-y", "-f", key_path)
		descriptor = os.open(public_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PUBLIC_MODE)
		os.fchmod(descriptor, PUBLIC_MODE)
		with os.fdopen(descriptor, "w") as handle:
			handle.write(public_key)


def new_host_key(paths, name, run=subprocess.run):
	"""Make a new host key for share ``name``. A Windows mount left from an old share then refuses this share's server."""
	ensure_share_directory(paths)
	remove_host_key(paths, name)
	_generate_key(run, paths.host_key(name))


def remove_host_key(paths, name):
	remove_file(paths.host_key(name))
	remove_file(paths.host_public_key(name))


def install_binaries(tools, paths):
	extract_verified_member(tools.rclone_mac_zip, RCLONE_MAC_BINARY, paths.mac_rclone, EXECUTABLE_MODE)
	extract_verified_member(tools.rclone_windows_zip, RCLONE_WINDOWS_BINARY, paths.windows_rclone, PUBLIC_MODE)


def public_host_key(paths, name):
	with open(paths.host_public_key(name)) as handle:
		return " ".join(handle.read().split()[:PUBLIC_KEY_FIELD_COUNT])


def can_bind(port, host=HOST_ADDRESS):
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
		try:
			probe.bind((host, port))
		except OSError:
			return False
	return True


def host_address_present(host=HOST_ADDRESS):
	return can_bind(0, host)


def can_connect(port, host=HOST_ADDRESS, timeout=CONNECT_TIMEOUT_SECONDS):
	try:
		with socket.create_connection((host, port), timeout):
			return True
	except OSError:
		return False


def last_log_line(path, offset=0):
	"""Return the last line of ``path`` after byte ``offset`` that is not blank, without rclone's time, reading at most LOG_TAIL_BYTES. Return None when there is none."""
	try:
		with open(path, "rb") as handle:
			size = handle.seek(0, os.SEEK_END)
			handle.seek(max(offset, size - LOG_TAIL_BYTES))
			data = handle.read(LOG_TAIL_BYTES)
	except OSError:
		return None
	lines = [line.strip() for line in data.decode("utf-8", "replace").splitlines() if line.strip()]
	return LOG_TIME_PREFIX.sub("", lines[-1]) if lines else None


def load_guest_script(name):
	with open(os.path.join(GUEST_SCRIPTS_DIRECTORY, name), encoding="utf-8") as handle:
		return handle.read()


def guest_script_bytes(name):
	with open(os.path.join(GUEST_SCRIPTS_DIRECTORY, name), "rb") as handle:
		return handle.read()


def read_mac_state(paths):
	try:
		with open(paths.state_file, encoding="utf-8") as handle:
			state = json.load(handle)
	except (OSError, ValueError):
		state = None
	if not isinstance(state, dict) or not isinstance(state.get("shares"), dict):
		return {"shares": {}}
	return state


def utc_now():
	return datetime.datetime.now(datetime.timezone.utc).strftime(UTC_TIME_FORMAT)


def seconds_since(text, now):
	"""Return the seconds from the UTC time ``text``, as utc_now writes it, to the Unix time ``now``, or None when ``text`` is not such a time."""
	try:
		then = datetime.datetime.strptime(text, UTC_TIME_FORMAT).replace(tzinfo=datetime.timezone.utc)
	except (TypeError, ValueError):
		return None
	return now - then.timestamp()


def write_private_file(path, text):
	part = path + PART_SUFFIX
	descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_MODE)
	os.fchmod(descriptor, PRIVATE_MODE)
	with os.fdopen(descriptor, "w") as handle:
		handle.write(text)
	os.replace(part, path)


def remove_file(path):
	try:
		os.remove(path)
	except FileNotFoundError:
		pass


def _is_wn_server(pid, paths, run):
	command = run([PS_PATH, "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
	return command.startswith("{} {} ".format(paths.mac_rclone, SERVE_SFTP_TEXT))


def _is_alive(pid, kill):
	try:
		kill(pid, 0)
	except (ProcessLookupError, PermissionError):
		return False
	return True


def _read_pid(path):
	try:
		with open(path) as handle:
			return int(handle.read().strip())
	except (OSError, ValueError):
		return None


def kill_orphan_servers(paths, run=subprocess.run, kill=os.kill, sleep=time.sleep, clock=time.monotonic):
	"""Stop the rclone servers that a previous agent left running, by the PIDs in their PID files."""
	for pid_path in glob.glob(os.path.join(glob.escape(paths.root), "*" + PID_SUFFIX)):
		pid = _read_pid(pid_path)
		if pid is not None and _is_alive(pid, kill) and _is_wn_server(pid, paths, run):
			kill(pid, signal.SIGTERM)
			deadline = clock() + SERVER_STOP_SECONDS
			while _is_alive(pid, kill) and clock() < deadline:
				sleep(ORPHAN_POLL_SECONDS)
			if _is_alive(pid, kill) and _is_wn_server(pid, paths, run):
				kill(pid, signal.SIGKILL)
		remove_file(pid_path)


def check_folder(path):
	"""Return why the share folder at ``path`` cannot be served, as a state and a detail, or None when wn-agent can list it."""
	if not os.path.isdir(path):
		return WAITING_STATE, MISSING_FOLDER_DETAIL.format(path)
	try:
		with os.scandir(path) as entries:
			next(entries, None)
	except PermissionError:
		return ERROR_STATE, NOT_ALLOWED_DETAIL.format(path)
	except OSError as error:
		return WAITING_STATE, UNREADABLE_DETAIL.format(path, error.strerror or error)
	return None


def start_daemon_thread(target):
	threading.Thread(target=target, daemon=True).start()


class FolderCheck:
	"""Runs a folder check on its own thread, so a folder that hangs holds up only its share."""

	def __init__(self, path, started, check, start_thread):
		self.path = path
		self.started = started
		self.check = check
		self.result = None
		self.finished = threading.Event()
		start_thread(self.run)

	def run(self):
		try:
			self.result = self.check(self.path)
		finally:
			self.finished.set()


@dataclasses.dataclass
class ShareServer:
	share: Share
	process: Optional[subprocess.Popen] = None
	restart_at: float = 0.0
	state: Optional[str] = None
	detail: Optional[str] = None
	start_error: Optional[str] = None
	log_offset: int = 0
	check: Optional[FolderCheck] = None
	folder_problem: Optional[tuple] = None
	folder_checked_at: Optional[float] = None
	logged: set = dataclasses.field(default_factory=set)


class SftpServerSupervisor:
	"""Runs one rclone SFTP server per Mac to Windows share of the configured VM and keeps that set in line with the config."""

	def __init__(self, log, paths=None, config_file=None, popen=subprocess.Popen, address_present=host_address_present, connect=can_connect,
			check_folder=check_folder, start_thread=start_daemon_thread, monotonic=time.monotonic, wall_clock=time.time, run=subprocess.run, kill=os.kill, load=load_config):
		self.log = log
		self.paths = paths or share_paths()
		self.config_file = config_file or config_path()
		self.popen = popen
		self.address_present = address_present
		self.connect = connect
		self.check_folder = check_folder
		self.start_thread = start_thread
		self.monotonic = monotonic
		self.wall_clock = wall_clock
		self.run_command = run
		self.kill = kill
		self.load = load
		self.stopping = threading.Event()
		self.wait = self.stopping.wait
		self.thread = None
		self.servers = {}
		self.desired = {}
		self.config_mtime = CONFIG_NOT_READ
		self.config_error = None
		self.last_clocks = None
		self.last_state = None
		self.state_written_at = None
		self.unreaped = []
		self.last_error = None

	def _write(self, message):
		self.log.write(message + "\n")
		self.log.flush()

	def _log_share(self, server, message):
		if message in server.logged:
			return
		server.logged.add(message)
		self._write(SHARE_LOG_PREFIX.format(server.share.name) + message)

	def _set(self, server, state, detail):
		if (state, detail) == (server.state, server.detail):
			return
		server.state = state
		server.detail = detail
		self._log_share(server, "{}, {}".format(state, detail))

	def _reload_config(self):
		try:
			mtime = os.stat(self.config_file).st_mtime_ns
		except FileNotFoundError:
			mtime = None
		if mtime == self.config_mtime:
			return
		self.config_mtime = mtime
		if mtime is None:
			self.desired = {}
			return
		try:
			config = self.load(self.config_file)
		except WnError as error:
			if str(error) != self.config_error:
				self.config_error = str(error)
				self._write(SUPERVISOR_LOG_PREFIX + "cannot read the config, keeping the last good shares: {}".format(error))
			return
		self.config_error = None
		self.desired = {share.name: share for share in shares_for_vm(config.shares, config.vm_name)}

	def _woke(self, now, wall):
		last = self.last_clocks
		self.last_clocks = (now, wall)
		return last is not None and (wall - last[1]) - (now - last[0]) > WAKE_GAP_SECONDS

	def _stop(self, servers, now):
		running = [server for server in servers if server.process is not None]
		for server in running:
			server.process.terminate()
		for server in running:
			process = server.process
			pid_file = self.paths.pid_file(server.share.name)
			try:
				process.wait(timeout=SERVER_STOP_SECONDS)
				remove_file(pid_file)
			except subprocess.TimeoutExpired:
				process.kill()
				try:
					process.wait(timeout=SERVER_STOP_SECONDS)
					remove_file(pid_file)
				except subprocess.TimeoutExpired:
					self.unreaped.append((process, pid_file))
			server.process = None
			server.restart_at = now

	def _apply_desired(self, now):
		for name in list(self.servers):
			server = self.servers[name]
			share = self.desired.get(name)
			if share is None:
				self._stop([server], now)
				del self.servers[name]
			elif share != server.share:
				self._stop([server], now)
				server.share = share
				server.check = None
				server.folder_checked_at = None
		for name, share in self.desired.items():
			if name not in self.servers:
				self.servers[name] = ShareServer(share)

	def _poll(self, now):
		unreaped = []
		for process, pid_file in self.unreaped:
			if process.poll() is None:
				unreaped.append((process, pid_file))
			elif _read_pid(pid_file) == process.pid:
				remove_file(pid_file)
		self.unreaped = unreaped
		for server in self.servers.values():
			if server.process is None:
				continue
			code = server.process.poll()
			if code is None:
				continue
			server.process = None
			remove_file(self.paths.pid_file(server.share.name))
			line = last_log_line(self.paths.log_file(server.share.name), server.log_offset)
			detail = "rclone exited with code {}".format(code) + (": " + line if line else "")
			self._log_share(server, "rclone exited with code {}. Starting it again in {} s".format(code, SERVER_RESTART_SECONDS))
			if server.state == SERVING_STATE:
				self._set(server, RESTARTING_STATE, detail)
			else:
				server.start_error = detail
				self._set(server, ERROR_STATE, detail)
			server.restart_at = now + SERVER_RESTART_SECONDS

	def _start(self, server, now):
		share = server.share
		log_file = self.paths.log_file(share.name)
		try:
			if os.path.exists(log_file) and os.path.getsize(log_file) > MAX_LOG_BYTES:
				os.truncate(log_file, 0)
			os.close(os.open(log_file, os.O_WRONLY | os.O_CREAT, PRIVATE_MODE))
			server.log_offset = os.path.getsize(log_file)
			server.process = self.popen(server_command(share, self.paths))
		except OSError as error:
			server.start_error = "cannot start rclone: {}".format(error)
			self._set(server, ERROR_STATE, server.start_error)
			server.restart_at = now + SERVER_RESTART_SECONDS
			return
		write_private_file(self.paths.pid_file(share.name), "{}\n".format(server.process.pid))
		if server.start_error is None:
			self._set(server, STARTING_STATE, "{}:{}".format(HOST_ADDRESS, share.port))
		else:
			self._set(server, ERROR_STATE, server.start_error)

	def _check_serving(self, server):
		"""Report the server as serving once its port accepts a connection."""
		if server.state == SERVING_STATE or not self.connect(server.share.port):
			return
		server.start_error = None
		server.logged.clear()
		self._set(server, SERVING_STATE, "{}:{}".format(HOST_ADDRESS, server.share.port))

	def _collect_check(self, server):
		check = server.check
		if check is not None and check.finished.is_set():
			server.check = None
			server.folder_problem = check.result
			server.folder_checked_at = check.started

	def _folder_problem(self, server, now):
		"""Return why the share's folder cannot be served, None when it can, or CHECK_PENDING before the first check ends. A check runs before each start and every FOLDER_CHECK_SECONDS while the server runs."""
		self._collect_check(server)
		interval = FOLDER_CHECK_SECONDS if server.process is not None else 0
		if server.check is None and (server.folder_checked_at is None or now - server.folder_checked_at >= interval):
			server.check = FolderCheck(server.share.mac_path, now, self.check_folder, self.start_thread)
			self._collect_check(server)
		if server.check is not None and now - server.check.started >= FOLDER_CHECK_TIMEOUT_SECONDS:
			return WAITING_STATE, NOT_RESPONDING_DETAIL.format(server.share.mac_path)
		if server.folder_checked_at is None:
			return CHECK_PENDING
		return server.folder_problem

	def _write_state(self, now):
		"""Write the state file when it changes, and every STATE_REFRESH_SECONDS so wn share can tell that this agent runs."""
		shares = {name: {"state": server.state, "detail": server.detail, "pid": server.process.pid if server.process else None} for name, server in self.servers.items()}
		fresh = self.state_written_at is not None and now - self.state_written_at < STATE_REFRESH_SECONDS
		if (shares == self.last_state and fresh) or not os.path.isdir(self.paths.root):
			return
		write_private_file(self.paths.state_file, json.dumps({"protocol": SHARE_PROTOCOL_VERSION, "updated": utc_now(), "shares": shares}))
		self.last_state = shares
		self.state_written_at = now

	def tick(self):
		now = self.monotonic()
		wall = self.wall_clock()
		self._reload_config()
		if self._woke(now, wall):
			self._write(SUPERVISOR_LOG_PREFIX + "the Mac woke from sleep. Restarting the share servers")
			self._stop(list(self.servers.values()), now)
		self._apply_desired(now)
		self._poll(now)
		if not self.address_present():
			self._stop(list(self.servers.values()), now)
			for server in self.servers.values():
				self._set(server, WAITING_STATE, NETWORK_DOWN_DETAIL)
		else:
			for server in self.servers.values():
				share = server.share
				problem = self._folder_problem(server, now)
				if problem is CHECK_PENDING:
					self._set(server, STARTING_STATE, CHECKING_DETAIL.format(share.mac_path))
				elif problem is not None:
					self._set(server, *problem)
				elif server.process is not None:
					self._check_serving(server)
				elif not os.path.exists(self.paths.host_key(share.name)):
					self._set(server, ERROR_STATE, HOST_KEY_MISSING_DETAIL.format(key=self.paths.host_key(share.name), remove=REMOVE_COMMAND, name=share.name, share=SHARE_COMMAND))
				elif now >= server.restart_at:
					self._start(server, now)
		self._write_state(now)

	def _log_errors(self, action, context):
		try:
			action()
		except Exception as error:
			message = "{}{}: {}".format(SUPERVISOR_LOG_PREFIX, context, error)
			if message != self.last_error:
				self.last_error = message
				self._write(message)

	def run(self):
		self._log_errors(lambda: kill_orphan_servers(self.paths, self.run_command, self.kill), "cannot stop the share servers of a previous agent")
		while not self.stopping.is_set():
			self._log_errors(self.tick, "unexpected error, trying again in {} s".format(SUPERVISOR_TICK_SECONDS))
			self.wait(SUPERVISOR_TICK_SECONDS)

	def start(self):
		self.thread = threading.Thread(target=self.run, daemon=True)
		self.thread.start()

	def stop(self):
		self.stopping.set()
		if self.thread is not None:
			self.thread.join(THREAD_STOP_SECONDS)
		self._stop(list(self.servers.values()), self.monotonic())
		remove_file(self.paths.state_file)
