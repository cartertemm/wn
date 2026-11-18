"""Mac folder into Windows over rclone SFTP: Mac files, the server command, guest file contents, and the server supervisor."""

import dataclasses
import datetime
import glob
import json
import os
import signal
import socket
import subprocess
import threading
import time
from typing import Optional

from wn.config import config_path, load_config
from wn.downloads import RCLONE_MAC_BINARY, RCLONE_WINDOWS_BINARY, extract_verified_member
from wn.errors import WnError
from wn.shares import GUEST_ROOT, HOST_ADDRESS, SUPPORT_DIRECTORY, Share, ShareError, shares_for_vm

SHARES_DIRECTORY_NAME = "shares"
LOGS_DIRECTORY = os.path.join("~", "Library", "Logs")
LOG_NAME = "wn-share-{}.log"
PID_SUFFIX = ".pid"
HOST_KEY_NAME = "host_ed25519"
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
SERVER_RESTART_SECONDS = 5
SERVER_STOP_SECONDS = 3
ORPHAN_POLL_SECONDS = 0.1
WAKE_GAP_SECONDS = 5
MAX_LOG_BYTES = 10 * 1024 * 1024
PS_PATH = "/bin/ps"
SERVE_SFTP_TEXT = "serve sftp"
SHARE_LOG_PREFIX = "share {}: "
SUPERVISOR_LOG_PREFIX = "shares: "
SERVING_STATE = "serving"
WAITING_STATE = "waiting"
RESTARTING_STATE = "restarting"
ERROR_STATE = "error"
NETWORK_DOWN_DETAIL = "the VM network is not up"
PART_SUFFIX = ".part"
CONFIG_NOT_READ = -1


@dataclasses.dataclass(frozen=True)
class SharePaths:
	root: str
	logs: str

	@property
	def host_key(self):
		return os.path.join(self.root, HOST_KEY_NAME)

	@property
	def client_key(self):
		return os.path.join(self.root, CLIENT_KEY_NAME)

	@property
	def client_public_key(self):
		return self.client_key + PUBLIC_KEY_SUFFIX

	@property
	def host_public_key(self):
		return self.host_key + PUBLIC_KEY_SUFFIX

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
		"--key", paths.host_key,
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
	return json.dumps({"name": share.name, "mount_path": share.vm_path, "read_only": share.read_only, "port": share.port})


def ensure_share_directory(paths):
	os.makedirs(paths.root, DIRECTORY_MODE, exist_ok=True)
	os.chmod(paths.root, DIRECTORY_MODE)
	if not os.path.exists(paths.empty_config):
		os.close(os.open(paths.empty_config, os.O_WRONLY | os.O_CREAT, PRIVATE_MODE))


def ensure_keys(paths, run=subprocess.run):
	ensure_share_directory(paths)
	for key_path in (paths.host_key, paths.client_key):
		if os.path.exists(key_path):
			continue
		result = run([SSH_KEYGEN_PATH, "-q", "-t", "ed25519", "-N", "", "-C", KEY_COMMENT, "-f", key_path], capture_output=True, text=True)
		if result.returncode != 0:
			raise ShareError("ssh-keygen failed with exit code {}: {}".format(result.returncode, result.stderr.strip()))
		os.chmod(key_path, PRIVATE_MODE)
		os.chmod(key_path + PUBLIC_KEY_SUFFIX, PUBLIC_MODE)


def install_binaries(tools, paths):
	extract_verified_member(tools.rclone_mac_zip, RCLONE_MAC_BINARY, paths.mac_rclone, EXECUTABLE_MODE)
	extract_verified_member(tools.rclone_windows_zip, RCLONE_WINDOWS_BINARY, paths.windows_rclone, PUBLIC_MODE)


def public_host_key(paths):
	with open(paths.host_public_key) as handle:
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
	return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def kill_orphan_servers(paths, run=subprocess.run, kill=os.kill, sleep=time.sleep, clock=time.monotonic):
	"""Stop the rclone servers that a previous agent left running, by the PIDs in their PID files."""
	for pid_path in glob.glob(os.path.join(glob.escape(paths.root), "*" + PID_SUFFIX)):
		try:
			with open(pid_path) as handle:
				pid = int(handle.read().strip())
		except (OSError, ValueError):
			pid = None
		if pid is not None and _is_alive(pid, kill) and _is_wn_server(pid, paths, run):
			kill(pid, signal.SIGTERM)
			deadline = clock() + SERVER_STOP_SECONDS
			while _is_alive(pid, kill) and clock() < deadline:
				sleep(ORPHAN_POLL_SECONDS)
			if _is_alive(pid, kill) and _is_wn_server(pid, paths, run):
				kill(pid, signal.SIGKILL)
		remove_file(pid_path)


@dataclasses.dataclass
class ShareServer:
	share: Share
	process: Optional[subprocess.Popen] = None
	restart_at: float = 0.0
	state: Optional[str] = None
	detail: Optional[str] = None
	logged: set = dataclasses.field(default_factory=set)


class SftpServerSupervisor:
	"""Runs one rclone SFTP server per Mac to Windows share of the configured VM and keeps that set in line with the config."""

	def __init__(self, log, paths=None, config_file=None, popen=subprocess.Popen, address_present=host_address_present,
			monotonic=time.monotonic, wall_clock=time.time, run=subprocess.run, kill=os.kill, load=load_config):
		self.log = log
		self.paths = paths or share_paths()
		self.config_file = config_file or config_path()
		self.popen = popen
		self.address_present = address_present
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
		self.unreaped = []

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
			try:
				process.wait(timeout=SERVER_STOP_SECONDS)
			except subprocess.TimeoutExpired:
				process.kill()
				try:
					process.wait(timeout=SERVER_STOP_SECONDS)
				except subprocess.TimeoutExpired:
					self.unreaped.append(process)
			remove_file(self.paths.pid_file(server.share.name))
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
		for name, share in self.desired.items():
			if name not in self.servers:
				self.servers[name] = ShareServer(share)

	def _poll(self, now):
		self.unreaped = [process for process in self.unreaped if process.poll() is None]
		for server in self.servers.values():
			if server.process is None:
				continue
			code = server.process.poll()
			if code is None:
				server.logged.clear()
				continue
			server.process = None
			remove_file(self.paths.pid_file(server.share.name))
			self._log_share(server, "rclone exited with code {}. Starting it again in {} s".format(code, SERVER_RESTART_SECONDS))
			self._set(server, RESTARTING_STATE, "rclone exited with code {}".format(code))
			server.restart_at = now + SERVER_RESTART_SECONDS

	def _start(self, server, now):
		share = server.share
		log_file = self.paths.log_file(share.name)
		try:
			if os.path.exists(log_file) and os.path.getsize(log_file) > MAX_LOG_BYTES:
				os.truncate(log_file, 0)
			os.close(os.open(log_file, os.O_WRONLY | os.O_CREAT, PRIVATE_MODE))
			server.process = self.popen(server_command(share, self.paths))
		except OSError as error:
			self._set(server, ERROR_STATE, "cannot start rclone: {}".format(error))
			server.restart_at = now + SERVER_RESTART_SECONDS
			return
		write_private_file(self.paths.pid_file(share.name), "{}\n".format(server.process.pid))
		self._set(server, SERVING_STATE, "{}:{}".format(HOST_ADDRESS, share.port))

	def _write_state(self):
		shares = {name: {"state": server.state, "detail": server.detail, "pid": server.process.pid if server.process else None} for name, server in self.servers.items()}
		if shares == self.last_state or not os.path.isdir(self.paths.root):
			return
		write_private_file(self.paths.state_file, json.dumps({"updated": utc_now(), "shares": shares}))
		self.last_state = shares

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
				if server.process is not None:
					self._set(server, SERVING_STATE, "{}:{}".format(HOST_ADDRESS, share.port))
				elif not os.path.isdir(share.mac_path):
					self._set(server, WAITING_STATE, "{} is missing".format(share.mac_path))
				elif now >= server.restart_at:
					self._start(server, now)
		self._write_state()

	def run(self):
		kill_orphan_servers(self.paths, self.run_command, self.kill)
		while not self.stopping.is_set():
			self.tick()
			self.wait(SUPERVISOR_TICK_SECONDS)

	def start(self):
		self.thread = threading.Thread(target=self.run, daemon=True)
		self.thread.start()

	def stop(self):
		self.stopping.set()
		if self.thread is not None:
			self.thread.join()
		self._stop(list(self.servers.values()), self.monotonic())
		remove_file(self.paths.state_file)
