"""Mac folder into Windows over rclone SFTP: Mac files, the server command, guest file contents, and the server supervisor."""

import dataclasses
import json
import os
import socket
import subprocess

from wn.downloads import RCLONE_MAC_BINARY, RCLONE_WINDOWS_BINARY, extract_verified_member
from wn.shares import GUEST_ROOT, HOST_ADDRESS, SUPPORT_DIRECTORY, ShareError

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
