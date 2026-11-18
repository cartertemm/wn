import json
import os
import socket
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from wn import share_sftp
from wn.downloads import RCLONE_MAC_BINARY, RCLONE_WINDOWS_BINARY, ShareTools
from wn.share_sftp import (
	PREPARE_SCRIPT,
	SharePaths,
	can_bind,
	ensure_keys,
	guest_script_bytes,
	host_address_present,
	install_binaries,
	load_guest_script,
	public_host_key,
	read_mac_state,
	render_known_hosts,
	render_rclone_conf,
	render_share_json,
	server_command,
	share_paths,
)
from wn.shares import Share, ShareError

FORBIDDEN_FLAGS = ("--pass", "--links", "-L", "--rc", "--sftp-chunk-size")
PORT = 47321
LOOPBACK = "127.0.0.1"
DOCUMENTATION_ADDRESS = "192.0.2.1"
HOST_KEY_TEXT = "ssh-ed25519 AAAAkey"
OPEN_UMASK = 0o000


def assert_safe_rclone_arguments(test, arguments):
	for flag in FORBIDDEN_FLAGS:
		test.assertFalse(any(argument == flag or argument.startswith(flag + "=") for argument in arguments), flag)
	for index, argument in enumerate(arguments):
		test.assertFalse(argument.startswith("--vfs-write-back=0"))
		if argument == "--vfs-write-back":
			test.assertNotEqual(arguments[index + 1], "0s")


def make_share(read_only=False, vm_path="C:\\Users\\c\\Projects"):
	return Share("Projects", "Windows", "mac_to_vm", "/Users/c/Projects", vm_path, read_only, PORT)


def file_mode(path):
	return stat.S_IMODE(os.stat(path).st_mode)


class FakeKeygen:
	def __init__(self, returncode=0, stderr=""):
		self.calls = []
		self.returncode = returncode
		self.stderr = stderr

	def __call__(self, argv, capture_output, text):
		self.calls.append(argv)
		if self.returncode == 0:
			key_path = argv[argv.index("-f") + 1]
			with open(key_path, "w") as handle:
				handle.write("private\n")
			with open(key_path + ".pub", "w") as handle:
				handle.write("ssh-ed25519 AAAA wn-share\n")
		return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


class SharePathsTestCase(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		self.addCleanup(self.home.cleanup)
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.root = os.path.join(self.home.name, "shares")
		self.logs = os.path.join(self.home.name, "Logs")
		self.paths = SharePaths(self.root, self.logs)


class ServerCommandTest(SharePathsTestCase):
	def test_server_command(self):
		self.assertEqual(server_command(make_share(), self.paths), [
			os.path.join(self.root, "bin", "rclone"), "serve", "sftp", "/Users/c/Projects",
			"--config", os.path.join(self.root, "empty-rclone.conf"),
			"--addr", "192.168.64.1:47321",
			"--key", os.path.join(self.root, "host_ed25519"),
			"--authorized-keys", os.path.join(self.root, "client_ed25519.pub"),
			"--dir-cache-time", "1s",
			"-v",
			"--log-file", os.path.join(self.logs, "wn-share-Projects.log"),
		])

	def test_read_only_server_command_ends_with_read_only(self):
		command = server_command(make_share(read_only=True), self.paths)
		self.assertEqual(command[-1], "--read-only")
		self.assertEqual(command[:-1], server_command(make_share(), self.paths))

	def test_server_command_has_no_forbidden_flag(self):
		for read_only in (False, True):
			assert_safe_rclone_arguments(self, server_command(make_share(read_only=read_only), self.paths))

	def test_server_command_reads_only_wns_empty_config(self):
		command = server_command(make_share(), self.paths)
		self.assertEqual(command.count("--config"), 1)
		self.assertEqual(command[command.index("--config") + 1], self.paths.empty_config)

	def test_share_paths_live_in_the_support_folder(self):
		paths = share_paths()
		self.assertEqual(paths.root, os.path.join(self.home.name, "Library", "Application Support", "wn", "shares"))
		self.assertEqual(paths.logs, os.path.join(self.home.name, "Library", "Logs"))
		self.assertEqual(paths.pid_file("Projects"), os.path.join(paths.root, "Projects.pid"))
		self.assertEqual(paths.state_file, os.path.join(paths.root, "state.json"))
		self.assertEqual(paths.windows_rclone, os.path.join(paths.root, "bin", "rclone.exe"))


class GuestFilesTest(unittest.TestCase):
	def test_rclone_conf(self):
		self.assertEqual(render_rclone_conf(make_share()), (
			"[share]\n"
			"type = sftp\n"
			"host = 192.168.64.1\n"
			"port = 47321\n"
			"user = wn\n"
			"key_file = C:\\ProgramData\\wn\\keys\\client_ed25519\n"
			"known_hosts_file = C:\\ProgramData\\wn\\shares\\Projects\\known_hosts\n"
			"shell_type = unix\n"
			"md5sum_command = md5sum\n"
			"sha1sum_command = sha1sum\n"
		))

	def test_rclone_conf_has_no_forbidden_key(self):
		keys = [line.split(" = ")[0] for line in render_rclone_conf(make_share()).splitlines()[1:]]
		self.assertNotIn("pass", keys)
		self.assertNotIn("chunk_size", keys)

	def test_known_hosts_has_both_lines(self):
		self.assertEqual(render_known_hosts(make_share(), HOST_KEY_TEXT), "192.168.64.1 ssh-ed25519 AAAAkey\n[192.168.64.1]:47321 ssh-ed25519 AAAAkey\n")

	def test_share_json(self):
		self.assertEqual(json.loads(render_share_json(make_share(read_only=True))), {
			"name": "Projects",
			"mount_path": "C:\\Users\\c\\Projects",
			"read_only": True,
			"port": PORT,
		})

	def test_share_json_keeps_control_characters_inside_the_value(self):
		vm_path = "C:\\Users\\c\\a\n\"port\": 1\r\x00"
		text = render_share_json(make_share(vm_path=vm_path))
		self.assertNotIn("\n", text)
		self.assertNotIn("\r", text)
		self.assertNotIn("\x00", text)
		values = json.loads(text)
		self.assertEqual(values["mount_path"], vm_path)
		self.assertEqual(values["port"], PORT)


class KeysTest(SharePathsTestCase):
	def setUp(self):
		super().setUp()
		previous = os.umask(OPEN_UMASK)
		self.addCleanup(os.umask, previous)

	def keygen_argv(self, key_path):
		return ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "wn-share", "-f", key_path]

	def test_ensure_keys_runs_ssh_keygen_once_per_missing_key(self):
		run = FakeKeygen()
		ensure_keys(self.paths, run=run)
		self.assertEqual(run.calls, [self.keygen_argv(self.paths.host_key), self.keygen_argv(self.paths.client_key)])
		self.assertEqual(file_mode(self.root), 0o700)
		for key_path in (self.paths.host_key, self.paths.client_key):
			self.assertEqual(file_mode(key_path), 0o600)
			self.assertEqual(file_mode(key_path + ".pub"), 0o644)
		self.assertTrue(os.path.isfile(self.paths.empty_config))
		self.assertEqual(os.path.getsize(self.paths.empty_config), 0)
		self.assertEqual(file_mode(self.paths.empty_config), 0o600)
		second = FakeKeygen()
		ensure_keys(self.paths, run=second)
		self.assertEqual(second.calls, [])

	def test_ensure_keys_tightens_an_existing_folder(self):
		os.makedirs(self.root, 0o755)
		ensure_keys(self.paths, run=FakeKeygen())
		self.assertEqual(file_mode(self.root), 0o700)

	def test_ensure_keys_keeps_existing_keys(self):
		ensure_keys(self.paths, run=FakeKeygen())
		with open(self.paths.host_key, "w") as handle:
			handle.write("kept\n")
		ensure_keys(self.paths, run=FakeKeygen())
		with open(self.paths.host_key) as handle:
			self.assertEqual(handle.read(), "kept\n")

	def test_ensure_keys_reports_a_failure(self):
		with self.assertRaises(ShareError) as caught:
			ensure_keys(self.paths, run=FakeKeygen(returncode=1, stderr="no entropy"))
		self.assertEqual(str(caught.exception), "ssh-keygen failed with exit code 1: no entropy")

	@unittest.skipUnless(os.path.exists("/usr/bin/ssh-keygen"), "needs /usr/bin/ssh-keygen")
	def test_ensure_keys_with_the_real_ssh_keygen(self):
		ensure_keys(self.paths)
		self.assertEqual(file_mode(self.paths.host_key), 0o600)
		self.assertEqual(file_mode(self.paths.client_key), 0o600)
		self.assertTrue(public_host_key(self.paths).startswith("ssh-ed25519 AAAA"))


class BinariesTest(SharePathsTestCase):
	def test_install_binaries_extracts_both_with_their_modes(self):
		tools = ShareTools("/d/mac.zip", "/d/windows.zip", "/d/winfsp.msi")
		with mock.patch.object(share_sftp, "extract_verified_member") as extract:
			install_binaries(tools, self.paths)
		self.assertEqual(extract.call_args_list, [
			mock.call("/d/mac.zip", RCLONE_MAC_BINARY, self.paths.mac_rclone, 0o755),
			mock.call("/d/windows.zip", RCLONE_WINDOWS_BINARY, self.paths.windows_rclone, 0o644),
		])


class HostKeyTest(SharePathsTestCase):
	def test_public_host_key_drops_the_comment(self):
		os.makedirs(self.root)
		with open(self.paths.host_public_key, "w") as handle:
			handle.write("ssh-ed25519 AAAAkey wn-share\n")
		self.assertEqual(public_host_key(self.paths), HOST_KEY_TEXT)


class AddressTest(unittest.TestCase):
	def test_can_bind_is_false_for_a_port_in_use(self):
		with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
			taken.bind((LOOPBACK, 0))
			port = taken.getsockname()[1]
			self.assertFalse(can_bind(port, LOOPBACK))

	def test_can_bind_is_true_for_a_free_port(self):
		self.assertTrue(can_bind(0, LOOPBACK))

	def test_host_address_present(self):
		self.assertTrue(host_address_present(LOOPBACK))
		self.assertFalse(host_address_present(DOCUMENTATION_ADDRESS))


class GuestScriptTest(unittest.TestCase):
	def test_load_guest_script_reads_the_file(self):
		self.assertIn("wn-result: ", load_guest_script(PREPARE_SCRIPT))

	def test_guest_script_bytes_matches_the_text(self):
		self.assertEqual(guest_script_bytes(PREPARE_SCRIPT).decode("utf-8"), load_guest_script(PREPARE_SCRIPT))


class MacStateTest(SharePathsTestCase):
	def write_state(self, text):
		os.makedirs(self.root, exist_ok=True)
		with open(self.paths.state_file, "w") as handle:
			handle.write(text)

	def test_read_mac_state_tolerates_missing_and_bad_files(self):
		self.assertEqual(read_mac_state(self.paths), {"shares": {}})
		for text in ("not json", "[]", "{}", '{"shares": []}', '{"shares": 3}'):
			self.write_state(text)
			self.assertEqual(read_mac_state(self.paths), {"shares": {}}, text)

	def test_read_mac_state_returns_a_good_file(self):
		state = {"shares": {"Projects": {"state": "running"}}}
		self.write_state(json.dumps(state))
		self.assertEqual(read_mac_state(self.paths), state)


if __name__ == "__main__":
	unittest.main()
