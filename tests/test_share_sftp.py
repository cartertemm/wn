import io
import json
import os
import signal
import socket
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from fakes import write_config
from wn import share_sftp
from wn.downloads import RCLONE_MAC_BINARY, RCLONE_WINDOWS_BINARY, ShareTools
from wn.share_sftp import (
	PREPARE_SCRIPT,
	SftpServerSupervisor,
	SharePaths,
	can_bind,
	ensure_keys,
	guest_script_bytes,
	host_address_present,
	install_binaries,
	kill_orphan_servers,
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
		if self.returncode == 0 and "-y" in argv:
			return subprocess.CompletedProcess(argv, 0, "ssh-ed25519 AAAA wn-share\n", "")
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

	def test_share_json_is_ascii_and_round_trips_a_non_ascii_path(self):
		vm_path = "C:\\Users\\Zoë\\Документы\\写真"
		text = render_share_json(make_share(vm_path=vm_path))
		self.assertTrue(text.isascii())
		self.assertEqual(json.loads(text)["mount_path"], vm_path)


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

	def test_ensure_keys_recreates_a_missing_public_key(self):
		ensure_keys(self.paths, run=FakeKeygen())
		os.remove(self.paths.client_public_key)
		run = FakeKeygen()
		ensure_keys(self.paths, run=run)
		self.assertEqual(run.calls, [["/usr/bin/ssh-keygen", "-y", "-f", self.paths.client_key]])
		with open(self.paths.client_public_key) as handle:
			self.assertEqual(handle.read(), "ssh-ed25519 AAAA wn-share\n")
		self.assertEqual(file_mode(self.paths.client_public_key), 0o644)
		with open(self.paths.client_key) as handle:
			self.assertEqual(handle.read(), "private\n")

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

	@unittest.skipUnless(os.path.exists("/usr/bin/ssh-keygen"), "needs /usr/bin/ssh-keygen")
	def test_ensure_keys_recreates_both_public_keys_with_the_real_ssh_keygen(self):
		ensure_keys(self.paths)
		keys = {}
		for key_path in (self.paths.host_key, self.paths.client_key):
			with open(key_path) as private, open(key_path + ".pub") as public:
				keys[key_path] = (private.read(), public.read().split()[:2])
			os.remove(key_path + ".pub")
		ensure_keys(self.paths)
		for key_path, (private_text, public_fields) in keys.items():
			with open(key_path) as private, open(key_path + ".pub") as public:
				self.assertEqual(private.read(), private_text)
				self.assertEqual(public.read().split()[:2], public_fields)
			self.assertEqual(file_mode(key_path + ".pub"), 0o644)


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


VM_NAME = "Windows"
SECOND_PORT = 47322
FIRST_PID = 5000
START_TIME = 1000.0
WALL_START = 1700000000.0
FIRST_MTIME_NS = 10 ** 18
SLEEP_SECONDS = 600
SUPERVISOR_TICK = 2
SHORT_LOG_LIMIT = 10
ORPHAN_PID = 4242


class FakeClock:
	def __init__(self, value):
		self.value = value

	def __call__(self):
		return self.value

	def advance(self, seconds):
		self.value += seconds


class FakeServerProcess:
	def __init__(self, pid, ignores_sigterm=False, ignores_sigkill=False):
		self.pid = pid
		self.returncode = None
		self.ignores_sigterm = ignores_sigterm
		self.ignores_sigkill = ignores_sigkill
		self.terminated = False
		self.killed = False
		self.wait_timeouts = []

	def poll(self):
		return self.returncode

	def wait(self, timeout=None):
		self.wait_timeouts.append(timeout)
		if self.returncode is None:
			raise subprocess.TimeoutExpired("rclone", timeout)
		return self.returncode

	def terminate(self):
		self.terminated = True
		if not self.ignores_sigterm:
			self.returncode = -signal.SIGTERM

	def kill(self):
		self.killed = True
		if not self.ignores_sigkill:
			self.returncode = -signal.SIGKILL


class FakePopen:
	def __init__(self):
		self.commands = []
		self.processes = []
		self.error = None
		self.options = {}

	def __call__(self, command):
		if self.error is not None:
			raise self.error
		self.commands.append(command)
		process = FakeServerProcess(FIRST_PID + len(self.processes), **self.options)
		self.processes.append(process)
		return process


class FakeKill:
	def __init__(self, alive=True):
		self.calls = []
		self.alive = alive

	def __call__(self, pid, sig):
		self.calls.append((pid, sig))
		if not self.alive:
			raise ProcessLookupError(pid)


def fake_ps(*outputs):
	remaining = list(outputs)

	def run(argv, capture_output, text):
		output = remaining.pop(0) if len(remaining) > 1 else remaining[0]
		return subprocess.CompletedProcess(argv, 0, output, "")
	return run


class SftpServerSupervisorTests(SharePathsTestCase):
	def setUp(self):
		super().setUp()
		os.makedirs(self.root)
		os.makedirs(self.logs)
		self.config_file = os.path.join(self.home.name, ".config", "wn", "config.json")
		self.mtime = FIRST_MTIME_NS
		self.log = io.StringIO()
		self.popen = FakePopen()
		self.present = True
		self.clock = FakeClock(START_TIME)
		self.wall = FakeClock(WALL_START)
		self.projects = self.make_folder("Projects")
		self.photos = self.make_folder("Photos")

	def make_folder(self, name):
		path = os.path.join(self.home.name, name)
		os.makedirs(path)
		return path

	def share_item(self, name="Projects", port=PORT, mac_path=None, read_only=False, vm=VM_NAME):
		mac_path = mac_path or (self.projects if name == "Projects" else self.photos)
		return {"name": name, "vm": vm, "direction": "mac_to_vm", "mac_path": mac_path, "vm_path": "C:\\Users\\c\\" + name, "read_only": read_only, "port": port}

	def write_shares(self, *items):
		write_config(self.home.name, {"vm_name": VM_NAME, "shares": list(items)})
		self.bump_mtime()

	def write_text(self, text):
		with open(self.config_file, "w") as handle:
			handle.write(text)
		self.bump_mtime()

	def bump_mtime(self):
		self.mtime += 1
		os.utime(self.config_file, ns=(self.mtime, self.mtime))

	def supervisor(self, **options):
		values = {"paths": self.paths, "config_file": self.config_file, "popen": self.popen, "address_present": lambda: self.present, "monotonic": self.clock, "wall_clock": self.wall}
		values.update(options)
		return SftpServerSupervisor(self.log, **values)

	def advance(self, seconds):
		self.clock.advance(seconds)
		self.wall.advance(seconds)

	def state(self):
		with open(self.paths.state_file) as handle:
			return json.load(handle)["shares"]

	def test_starts_a_server_when_the_address_is_present(self):
		item = self.share_item()
		self.write_shares(item)
		self.supervisor().tick()
		self.assertEqual(self.popen.commands, [server_command(Share(**item), self.paths)])
		self.assertEqual(self.state(), {"Projects": {"state": "serving", "detail": "192.168.64.1:47321", "pid": FIRST_PID}})
		with open(self.paths.pid_file("Projects")) as handle:
			self.assertEqual(handle.read().strip(), str(FIRST_PID))
		self.assertIn("share Projects: serving, 192.168.64.1:47321\n", self.log.getvalue())

	def test_started_commands_have_no_forbidden_flag(self):
		self.write_shares(self.share_item(), self.share_item("Photos", SECOND_PORT, read_only=True))
		self.supervisor().tick()
		self.assertEqual(len(self.popen.commands), 2)
		for command in self.popen.commands:
			assert_safe_rclone_arguments(self, command)

	def test_waits_while_the_address_is_absent(self):
		self.write_shares(self.share_item())
		self.present = False
		self.supervisor().tick()
		self.assertEqual(self.popen.commands, [])
		self.assertEqual(self.state(), {"Projects": {"state": "waiting", "detail": "the VM network is not up", "pid": None}})

	def test_stops_servers_when_the_address_goes(self):
		self.write_shares(self.share_item())
		supervisor = self.supervisor()
		supervisor.tick()
		self.present = False
		self.advance(SUPERVISOR_TICK)
		supervisor.tick()
		self.assertTrue(self.popen.processes[0].terminated)
		self.assertFalse(os.path.exists(self.paths.pid_file("Projects")))
		self.assertEqual(self.state()["Projects"]["state"], "waiting")
		self.present = True
		self.advance(SUPERVISOR_TICK)
		supervisor.tick()
		self.assertEqual(len(self.popen.commands), 2)

	def test_restarts_5_s_after_an_exit(self):
		self.write_shares(self.share_item())
		supervisor = self.supervisor()
		supervisor.tick()
		self.popen.processes[0].returncode = 1
		supervisor.tick()
		self.assertIn("share Projects: rclone exited with code 1. Starting it again in 5 s\n", self.log.getvalue())
		self.assertEqual(self.state()["Projects"], {"state": "restarting", "detail": "rclone exited with code 1", "pid": None})
		self.assertFalse(os.path.exists(self.paths.pid_file("Projects")))
		self.advance(4)
		supervisor.tick()
		self.assertEqual(len(self.popen.commands), 1)
		self.advance(1)
		supervisor.tick()
		self.assertEqual(len(self.popen.commands), 2)

	def test_a_server_that_exits_at_once_is_retried_every_5_s_and_logged_once(self):
		self.write_shares(self.share_item())
		supervisor = self.supervisor()
		for _ in range(4):
			supervisor.tick()
			self.popen.processes[-1].returncode = 1
			self.advance(SUPERVISOR_TICK)
			supervisor.tick()
			self.advance(5)
		self.assertEqual(len(self.popen.commands), 4)
		self.assertEqual(self.log.getvalue().count("rclone exited with code 1. Starting"), 1)
		self.assertEqual(self.log.getvalue().count("serving"), 1)

	def test_an_exit_after_a_server_ran_is_logged_again(self):
		self.write_shares(self.share_item())
		supervisor = self.supervisor()
		for _ in range(2):
			supervisor.tick()
			self.advance(SUPERVISOR_TICK)
			supervisor.tick()
			self.popen.processes[-1].returncode = 1
			supervisor.tick()
			self.advance(5)
		self.assertEqual(self.log.getvalue().count("rclone exited with code 1. Starting"), 2)

	def test_restarts_every_server_after_a_wake_gap(self):
		self.write_shares(self.share_item(), self.share_item("Photos", SECOND_PORT))
		supervisor = self.supervisor()
		supervisor.tick()
		self.clock.advance(SUPERVISOR_TICK)
		self.wall.advance(SLEEP_SECONDS)
		supervisor.tick()
		self.assertTrue(all(process.terminated for process in self.popen.processes[:2]))
		self.assertEqual(len(self.popen.commands), 4)
		self.assertIn("shares: the Mac woke from sleep. Restarting the share servers\n", self.log.getvalue())
		self.assertEqual(self.state()["Projects"]["pid"], self.popen.processes[2].pid)

	def test_a_wall_clock_that_jumps_back_is_not_a_wake(self):
		self.write_shares(self.share_item())
		supervisor = self.supervisor()
		supervisor.tick()
		self.clock.advance(SUPERVISOR_TICK)
		self.wall.advance(-SLEEP_SECONDS)
		supervisor.tick()
		self.assertFalse(self.popen.processes[0].terminated)
		self.assertEqual(len(self.popen.commands), 1)
		self.assertNotIn("woke", self.log.getvalue())

	def test_a_gap_of_5_s_is_not_a_wake(self):
		self.write_shares(self.share_item())
		supervisor = self.supervisor()
		supervisor.tick()
		self.clock.advance(SUPERVISOR_TICK)
		self.wall.advance(SUPERVISOR_TICK + 5)
		supervisor.tick()
		self.assertEqual(len(self.popen.commands), 1)

	def test_config_reload_adds_and_removes_shares(self):
		self.write_shares(self.share_item())
		supervisor = self.supervisor()
		supervisor.tick()
		self.write_shares(self.share_item(), self.share_item("Photos", SECOND_PORT))
		supervisor.tick()
		self.assertEqual(len(self.popen.commands), 2)
		self.assertEqual(set(self.state()), {"Projects", "Photos"})
		self.write_shares(self.share_item("Photos", SECOND_PORT))
		supervisor.tick()
		self.assertTrue(self.popen.processes[0].terminated)
		self.assertFalse(self.popen.processes[1].terminated)
		self.assertFalse(os.path.exists(self.paths.pid_file("Projects")))
		self.assertEqual(set(self.state()), {"Photos"})

	def test_removing_the_last_share_updates_the_state_file(self):
		self.write_shares(self.share_item())
		supervisor = self.supervisor()
		supervisor.tick()
		self.write_shares()
		supervisor.tick()
		self.assertEqual(self.state(), {})

	def test_a_changed_share_restarts_its_server_with_the_new_command(self):
		other_folder = self.make_folder("Other")
		for change in ({"read_only": True}, {"port": SECOND_PORT}, {"mac_path": other_folder}):
			with self.subTest(change=change):
				self.popen = FakePopen()
				self.write_shares(self.share_item())
				supervisor = self.supervisor()
				supervisor.tick()
				changed = self.share_item(**change)
				self.write_shares(changed)
				supervisor.tick()
				self.assertTrue(self.popen.processes[0].terminated)
				self.assertEqual(self.popen.commands[1:], [server_command(Share(**changed), self.paths)])

	def test_shares_of_another_vm_are_ignored(self):
		self.write_shares(self.share_item(), self.share_item("Photos", SECOND_PORT, vm="Other"))
		self.supervisor().tick()
		self.assertEqual(self.popen.commands, [server_command(Share(**self.share_item()), self.paths)])
		self.assertEqual(set(self.state()), {"Projects"})

	def test_a_missing_config_file_means_no_shares(self):
		self.supervisor().tick()
		self.assertEqual(self.popen.commands, [])
		self.assertEqual(self.state(), {})

	def test_missing_mac_folder_waits(self):
		missing = os.path.join(self.home.name, "Gone")
		self.write_shares(self.share_item(mac_path=missing))
		supervisor = self.supervisor()
		supervisor.tick()
		self.assertEqual(self.popen.commands, [])
		self.assertEqual(self.state()["Projects"], {"state": "waiting", "detail": missing + " is missing", "pid": None})
		os.makedirs(missing)
		supervisor.tick()
		self.assertEqual(len(self.popen.commands), 1)

	def test_bad_config_keeps_the_last_good_shares_and_logs_once(self):
		self.write_shares(self.share_item())
		supervisor = self.supervisor()
		supervisor.tick()
		for _ in range(3):
			self.write_text("not json")
			supervisor.tick()
		self.assertFalse(self.popen.processes[0].terminated)
		self.assertEqual(self.state()["Projects"]["state"], "serving")
		self.assertEqual(self.log.getvalue().count("shares: cannot read the config, keeping the last good shares: "), 1)

	def test_state_file_is_written_only_on_change(self):
		self.write_shares(self.share_item())
		supervisor = self.supervisor()
		supervisor.tick()
		os.utime(self.paths.state_file, ns=(FIRST_MTIME_NS, FIRST_MTIME_NS))
		supervisor.tick()
		supervisor.tick()
		self.assertEqual(os.stat(self.paths.state_file).st_mtime_ns, FIRST_MTIME_NS)

	def test_state_and_pid_files_are_private_and_replaced_atomically(self):
		previous = os.umask(OPEN_UMASK)
		self.addCleanup(os.umask, previous)
		with open(self.paths.state_file + ".part", "w") as handle:
			handle.write("old")
		os.chmod(self.paths.state_file + ".part", 0o644)
		self.write_shares(self.share_item())
		with mock.patch.object(share_sftp.os, "replace", wraps=os.replace) as replace:
			self.supervisor().tick()
		self.assertIn(mock.call(self.paths.state_file + ".part", self.paths.state_file), replace.call_args_list)
		self.assertIn(mock.call(self.paths.pid_file("Projects") + ".part", self.paths.pid_file("Projects")), replace.call_args_list)
		self.assertEqual(file_mode(self.paths.state_file), 0o600)
		self.assertEqual(file_mode(self.paths.pid_file("Projects")), 0o600)
		self.assertEqual(file_mode(self.paths.log_file("Projects")), 0o600)
		self.assertFalse(os.path.exists(self.paths.state_file + ".part"))

	def test_no_state_file_without_the_shares_folder(self):
		os.rmdir(self.root)
		self.supervisor().tick()
		self.assertFalse(os.path.exists(self.root))

	def test_one_log_line_per_distinct_message(self):
		self.write_shares(self.share_item())
		self.present = False
		supervisor = self.supervisor()
		for _ in range(3):
			supervisor.tick()
		self.assertEqual(self.log.getvalue(), "share Projects: waiting, the VM network is not up\n")

	def test_truncates_a_large_log_before_start(self):
		log_file = self.paths.log_file("Projects")
		with open(log_file, "w") as handle:
			handle.write("x" * (SHORT_LOG_LIMIT + 1))
		self.write_shares(self.share_item())
		with mock.patch.object(share_sftp, "MAX_LOG_BYTES", SHORT_LOG_LIMIT):
			self.supervisor().tick()
		self.assertEqual(os.path.getsize(log_file), 0)

	def test_keeps_a_log_at_the_limit(self):
		log_file = self.paths.log_file("Projects")
		with open(log_file, "w") as handle:
			handle.write("x" * SHORT_LOG_LIMIT)
		self.write_shares(self.share_item())
		with mock.patch.object(share_sftp, "MAX_LOG_BYTES", SHORT_LOG_LIMIT):
			self.supervisor().tick()
		self.assertEqual(os.path.getsize(log_file), SHORT_LOG_LIMIT)

	def test_a_start_error_sets_error_and_retries_after_5_s(self):
		self.write_shares(self.share_item())
		self.popen.error = OSError("no rclone")
		supervisor = self.supervisor()
		supervisor.tick()
		self.assertEqual(self.state()["Projects"], {"state": "error", "detail": "cannot start rclone: no rclone", "pid": None})
		self.popen.error = None
		self.advance(4)
		supervisor.tick()
		self.assertEqual(self.popen.commands, [])
		self.advance(1)
		supervisor.tick()
		self.assertEqual(len(self.popen.commands), 1)

	def test_a_server_that_ignores_sigterm_is_killed_after_3_s(self):
		self.write_shares(self.share_item())
		self.popen.options = {"ignores_sigterm": True}
		supervisor = self.supervisor()
		supervisor.tick()
		self.present = False
		supervisor.tick()
		process = self.popen.processes[0]
		self.assertTrue(process.killed)
		self.assertEqual(process.wait_timeouts, [3, 3])

	def test_a_server_stuck_after_sigkill_does_not_block_and_is_reaped_later(self):
		self.write_shares(self.share_item())
		self.popen.options = {"ignores_sigterm": True, "ignores_sigkill": True}
		supervisor = self.supervisor()
		supervisor.tick()
		self.present = False
		supervisor.tick()
		process = self.popen.processes[0]
		self.assertNotIn(None, process.wait_timeouts)
		self.assertEqual(supervisor.unreaped, [(process, self.paths.pid_file("Projects"))])
		process.returncode = -signal.SIGKILL
		supervisor.tick()
		self.assertEqual(supervisor.unreaped, [])

	def test_a_stuck_server_keeps_its_pid_file_until_it_is_reaped(self):
		self.write_shares(self.share_item())
		self.popen.options = {"ignores_sigterm": True, "ignores_sigkill": True}
		supervisor = self.supervisor()
		supervisor.tick()
		self.present = False
		supervisor.tick()
		supervisor.tick()
		with open(self.paths.pid_file("Projects")) as handle:
			self.assertEqual(handle.read(), "{}\n".format(FIRST_PID))
		self.popen.processes[0].returncode = -signal.SIGKILL
		supervisor.tick()
		self.assertFalse(os.path.exists(self.paths.pid_file("Projects")))

	def test_reaping_a_stuck_server_keeps_the_pid_file_of_its_successor(self):
		self.write_shares(self.share_item())
		self.popen.options = {"ignores_sigterm": True, "ignores_sigkill": True}
		supervisor = self.supervisor()
		supervisor.tick()
		self.present = False
		supervisor.tick()
		self.present = True
		self.popen.options = {}
		supervisor.tick()
		self.popen.processes[0].returncode = -signal.SIGKILL
		supervisor.tick()
		with open(self.paths.pid_file("Projects")) as handle:
			self.assertEqual(handle.read(), "{}\n".format(FIRST_PID + 1))

	def test_stop_terminates_and_removes_the_state_file(self):
		self.write_shares(self.share_item())
		supervisor = self.supervisor(run=fake_ps(""))
		supervisor.tick()
		supervisor.start()
		supervisor.stop()
		self.assertFalse(supervisor.thread.is_alive())
		self.assertTrue(self.popen.processes[0].terminated)
		self.assertFalse(os.path.exists(self.paths.state_file))
		self.assertFalse(os.path.exists(self.paths.pid_file("Projects")))

	def test_run_kills_orphans_before_the_first_tick(self):
		with open(self.paths.pid_file("Old"), "w") as handle:
			handle.write("{}\n".format(ORPHAN_PID))
		kill = FakeKill(alive=False)
		supervisor = self.supervisor(run=fake_ps(""), kill=kill)
		supervisor.stopping.set()
		supervisor.run()
		self.assertEqual(kill.calls, [(ORPHAN_PID, 0)])
		self.assertFalse(os.path.exists(self.paths.pid_file("Old")))

	def test_run_logs_each_new_tick_error_once_and_keeps_ticking(self):
		supervisor = self.supervisor(run=fake_ps(""))
		errors = [OSError(28, "No space left on device")] * 3 + [ValueError("bad state")]
		waits = []
		def tick():
			if not errors:
				supervisor.stopping.set()
				return
			raise errors.pop(0)
		supervisor.tick = tick
		supervisor.wait = waits.append
		supervisor.run()
		self.assertEqual(errors, [])
		self.assertEqual(waits, [SUPERVISOR_TICK] * 5)
		self.assertEqual(self.log.getvalue(), (
			"shares: unexpected error, trying again in 2 s: [Errno 28] No space left on device\n"
			"shares: unexpected error, trying again in 2 s: bad state\n"
		))

	def test_run_logs_an_orphan_cleanup_error_and_keeps_going(self):
		with open(self.paths.pid_file("Old"), "w") as handle:
			handle.write("{}\n".format(ORPHAN_PID))
		ticks = []
		def run(argv, capture_output, text):
			raise OSError(35, "Resource temporarily unavailable")
		supervisor = self.supervisor(run=run, kill=FakeKill())
		def tick():
			ticks.append(True)
			supervisor.stopping.set()
		supervisor.tick = tick
		supervisor.run()
		self.assertEqual(ticks, [True])
		self.assertEqual(self.log.getvalue(), "shares: cannot stop the share servers of a previous agent: [Errno 35] Resource temporarily unavailable\n")

	def test_run_ends_on_keyboard_interrupt(self):
		supervisor = self.supervisor(run=fake_ps(""))
		supervisor.tick = mock.Mock(side_effect=KeyboardInterrupt)
		with self.assertRaises(KeyboardInterrupt):
			supervisor.run()


class KillOrphanServersTests(SharePathsTestCase):
	def setUp(self):
		super().setUp()
		os.makedirs(self.root)
		self.clock = FakeClock(START_TIME)
		self.wn_command = "{} serve sftp /x --addr 192.168.64.1:47321".format(self.paths.mac_rclone)

	def write_pid(self, name, text):
		with open(self.paths.pid_file(name), "w") as handle:
			handle.write(text)

	def kill_orphans(self, run, kill):
		kill_orphan_servers(self.paths, run=run, kill=kill, sleep=self.clock.advance, clock=self.clock)

	def spawn_sleeper(self):
		process = subprocess.Popen(["/bin/sleep", "30"])
		self.addCleanup(process.wait)
		self.addCleanup(process.kill)
		return process

	def test_kill_orphan_servers_kills_only_wn_rclone(self):
		for output, killed in ((self.wn_command, True), ("/bin/sleep 30", False)):
			with self.subTest(output=output):
				process = self.spawn_sleeper()
				self.write_pid("Projects", "{}\n".format(process.pid))
				calls = []

				def kill(pid, sig):
					calls.append((pid, sig))
					os.kill(pid, sig)
					if sig == signal.SIGTERM:
						process.wait()
				self.kill_orphans(fake_ps(output), kill)
				self.assertEqual((process.pid, signal.SIGTERM) in calls, killed)
				self.assertNotIn((process.pid, signal.SIGKILL), calls)
				self.assertEqual(process.poll() == -signal.SIGTERM, killed)
				self.assertFalse(os.path.exists(self.paths.pid_file("Projects")))

	def test_a_server_that_ignores_sigterm_gets_sigkill_after_3_s(self):
		self.write_pid("Projects", "{}\n".format(ORPHAN_PID))
		kill = FakeKill()
		self.kill_orphans(fake_ps(self.wn_command), kill)
		self.assertEqual([call for call in kill.calls if call[1] != 0], [(ORPHAN_PID, signal.SIGTERM), (ORPHAN_PID, signal.SIGKILL)])
		self.assertGreaterEqual(self.clock.value, START_TIME + 3)

	def test_a_pid_reused_during_the_wait_is_not_killed(self):
		self.write_pid("Projects", "{}\n".format(ORPHAN_PID))
		kill = FakeKill()
		self.kill_orphans(fake_ps(self.wn_command, "/usr/bin/other"), kill)
		self.assertNotIn((ORPHAN_PID, signal.SIGKILL), kill.calls)

	def test_a_command_that_only_mentions_the_rclone_path_is_not_killed(self):
		self.write_pid("Projects", "{}\n".format(ORPHAN_PID))
		kill = FakeKill()
		self.kill_orphans(fake_ps("/usr/bin/grep serve sftp {}".format(self.paths.mac_rclone)), kill)
		self.assertEqual(kill.calls, [(ORPHAN_PID, 0)])

	def test_a_process_of_another_user_is_left_alone(self):
		self.write_pid("Projects", "{}\n".format(ORPHAN_PID))
		calls = []

		def kill(pid, sig):
			calls.append((pid, sig))
			raise PermissionError(pid)
		self.kill_orphans(fake_ps(self.wn_command), kill)
		self.assertEqual(calls, [(ORPHAN_PID, 0)])

	def test_bad_and_dead_pid_files_are_deleted(self):
		self.write_pid("Bad", "not a pid")
		self.write_pid("Dead", "{}\n".format(ORPHAN_PID))
		kill = FakeKill(alive=False)
		self.kill_orphans(fake_ps(self.wn_command), kill)
		self.assertEqual(kill.calls, [(ORPHAN_PID, 0)])
		self.assertEqual(os.listdir(self.root), [])

	def test_a_missing_shares_folder_is_fine(self):
		os.rmdir(self.root)
		self.kill_orphans(fake_ps(self.wn_command), FakeKill())


if __name__ == "__main__":
	unittest.main()
