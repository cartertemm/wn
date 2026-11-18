import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.fakes import FakeBackend, write_config
from wn.backends.utm import UnknownVMError, UTMCtlError
from wn.config import ConfigError, load_config, update_config_file
from wn.downloads import RCLONE_WINDOWS_BINARY, WINFSP_SOURCE, ShareTools
from wn.guest_runner import GuestError, GuestScriptError
from wn.agent import launch_agent_path, pid_file_path
from wn.share_commands import REMOVE_PROGRESS_SECONDS, WINFSP_NOTICE, ShareCommands, agent_is_installed, agent_is_running
from wn.share_sftp import (
	CLEANUP_SCRIPT,
	FETCH_SCRIPT,
	GUEST_CLIENT_KEY,
	GUEST_STATUS_FILE,
	GUEST_SUPERVISOR,
	PREPARE_SCRIPT,
	SUPERVISOR_SCRIPT,
	TASK_SCRIPT,
	SharePaths,
	guest_script_bytes,
)
from wn.shares import FIRST_PORT, Share, ShareError

CLIENT_KEY_TEXT = "-----BEGIN OPENSSH PRIVATE KEY-----\nsecretkeymaterial\n-----END OPENSSH PRIVATE KEY-----\n"
HOST_PUBLIC_KEY_TEXT = "ssh-ed25519 AAAAhost wn-share\n"
SHARE_NAME = "Projects"
VM_PATH = "C:\\Users\\c\\Projects"
FOLDER = "C:\\ProgramData\\wn\\shares\\Projects"
TOOLS = ShareTools("/downloads/rclone-mac.zip", "/downloads/rclone-windows.zip", "/downloads/winfsp.msi")


def prepare_result(**changes):
	result = {
		"winfsp": "ok",
		"rclone_sha256": RCLONE_WINDOWS_BINARY.sha256,
		"supervisor_sha256": None,
		"client_key_sha256": None,
		"parent_exists": True,
		"vm_path": "missing",
		"removing": False,
		"linked_parent": None,
		"system_folder": None,
	}
	result.update(changes)
	return result


def sha256_text(text):
	return hashlib.sha256(text.encode("utf-8")).hexdigest()


def status_text(shares):
	return json.dumps({"updated": "2026-11-17T10:00:00Z", "shares": shares})


class FakeRunner:
	"""Stands in for GuestRunner. Scripts are told apart by their label, the script file name."""

	def __init__(self, results=None):
		self.calls = []
		self.files = {}
		self.on_call = None
		self.push_errors = {}
		self.results = {
			PREPARE_SCRIPT: prepare_result(),
			FETCH_SCRIPT: {"mismatch": None, "winfsp_exit": 0, "winfsp_log": []},
			TASK_SCRIPT: {"task": "registered"},
			CLEANUP_SCRIPT: {"cleaned": True},
		}
		self.results.update(results or {})

	def _record(self, call):
		self.calls.append(call)
		if self.on_call is not None:
			self.on_call(call)

	def run_json(self, script, params=None, timeout=None, label=None):
		self._record(("run", label, params))
		result = self.results[label]
		if isinstance(result, Exception):
			raise result
		return result

	def push(self, guest_path, data):
		self._record(("push", guest_path))
		if guest_path in self.push_errors:
			raise self.push_errors[guest_path]
		self.files[guest_path] = data.decode("utf-8")

	def pull(self, guest_path):
		self._record(("pull", guest_path))
		value = self.files.get(guest_path)
		if callable(value):
			value = value()
		if value is None:
			raise UTMCtlError("utmctl file failed with exit code 1: Error: cannot open file")
		return value


class FakeClock:
	def __init__(self):
		self.now = 0.0

	def __call__(self):
		return self.now

	def sleep(self, seconds):
		self.now += seconds


class FakeOffer:
	def __init__(self, files, log):
		self.files = dict(files)
		self.open = False
		log.append(self)

	def __enter__(self):
		self.open = True
		return self

	def __exit__(self, *exc_info):
		self.open = False

	def url(self, name):
		return "http://offer/" + name


class ShareCommandTestCase(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		self.addCleanup(self.home.cleanup)
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.mac_folder = self.make_mac_folder(SHARE_NAME)
		self.paths = SharePaths(os.path.join(self.home.name, "support", "shares"), os.path.join(self.home.name, "logs"))
		os.makedirs(self.paths.root)
		with open(self.paths.client_key, "w") as handle:
			handle.write(CLIENT_KEY_TEXT)
		with open(self.paths.host_public_key, "w") as handle:
			handle.write(HOST_PUBLIC_KEY_TEXT)
		write_config(self.home.name, {})
		self.runner = FakeRunner()
		self.backend = FakeBackend()
		self.clock = FakeClock()
		self.output = io.StringIO()
		self.offers = []
		self.installs = []
		self.serve(SHARE_NAME)
		self.mount(SHARE_NAME)

	def make_mac_folder(self, name):
		path = os.path.join(self.home.name, name)
		os.makedirs(path)
		return os.path.realpath(path)

	def serve(self, name, state="serving", detail="192.168.64.1:47321"):
		with open(self.paths.state_file, "w") as handle:
			json.dump({"updated": "now", "shares": {name: {"state": state, "detail": detail, "pid": 1234}}}, handle)

	def mount(self, name):
		self.runner.files[GUEST_STATUS_FILE] = status_text({name: {"state": "mounted", "detail": "", "pid": 5678}})

	def install_agent(self):
		self.installs.append(True)
		return "/agents/com.wn.agent.plist"

	def commands(self, **changes):
		options = dict(
			runner=self.runner,
			paths=self.paths,
			fetch_tools=lambda report: TOOLS,
			install_binaries=lambda tools, paths: None,
			ensure_keys=lambda paths: None,
			agent_installed=lambda: True,
			agent_running=lambda: True,
			install_agent=self.install_agent,
			offer_factory=lambda files: FakeOffer(files, self.offers),
			can_bind=lambda port: True,
			clock=self.clock,
			sleep=self.clock.sleep,
		)
		options.update(changes)
		return ShareCommands(load_config(), self.backend, self.output, **options)

	def add(self, mac_dir=None, vm_dir=VM_PATH, **options):
		return self.commands().add_share(mac_dir or self.mac_folder, vm_dir, **options)

	def config_text(self):
		with open(os.path.join(self.home.name, ".config", "wn", "config.json")) as handle:
			return handle.read()

	def runs(self):
		return [call[1] for call in self.runner.calls if call[0] == "run"]

	def pushes(self):
		return [call[1] for call in self.runner.calls if call[0] == "push"]

	def params_of(self, label):
		return [call[2] for call in self.runner.calls if call[0] == "run" and call[1] == label][0]

	def assert_refused_with_config_unchanged(self, message=None):
		before = self.config_text()
		with self.assertRaises(ShareError) as caught:
			self.add()
		self.assertEqual(self.config_text(), before)
		self.assertEqual(load_config().shares, ())
		if message is not None:
			self.assertEqual(str(caught.exception), message)
		return caught.exception

	def assert_rolled_back(self):
		self.assertIn(FOLDER + "\\remove", self.runner.files)
		self.assertEqual(self.runner.files[FOLDER + "\\remove"], "")
		self.assertEqual(load_config().shares, ())


class VMCheckTests(ShareCommandTestCase):
	def test_stopped_vm_is_refused_before_any_guest_call(self):
		self.backend.state = "stopped"
		self.assert_refused_with_config_unchanged("VM 'Windows' is stopped. Run wn start, wait until Windows has started, then run wn share again")
		self.assertEqual(self.runner.calls, [])

	def test_guest_agent_down_is_refused(self):
		self.backend.ip_address = None
		self.assert_refused_with_config_unchanged("Windows is still starting (no answer from the guest agent). Try again in a minute")
		self.assertEqual(self.runner.calls, [])

	def test_guest_agent_error_is_refused(self):
		self.backend.ip = mock.Mock(side_effect=UTMCtlError("utmctl ip-address failed with exit code 1"))
		self.assert_refused_with_config_unchanged("Windows is still starting (no answer from the guest agent). Try again in a minute")
		self.assertEqual(self.runner.calls, [])

	def test_duplicate_name_and_overlap_are_refused_before_the_vm_check(self):
		update_config_file({"shares": [Share(SHARE_NAME, "Windows", "mac_to_vm", "/elsewhere", "C:\\Other", False, FIRST_PORT).to_dict()]})
		with self.assertRaises(ShareError) as caught:
			self.add()
		self.assertIn("already exists", str(caught.exception))
		with self.assertRaises(ShareError) as caught:
			self.add(vm_dir="C:\\Other\\Inner", name="Other")
		self.assertIn("overlaps C:\\Other", str(caught.exception))
		update_config_file({"shares": [Share("Old", "Windows", "mac_to_vm", self.mac_folder, "C:\\Other", False, FIRST_PORT).to_dict()]})
		with self.assertRaises(ShareError) as caught:
			self.add(vm_dir="C:\\New")
		self.assertIn("the Mac folder of share 'Old'", str(caught.exception))
		self.assertNotIn(("status",), self.backend.calls)
		self.assertEqual(self.runner.calls, [])


class AgentStateTests(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		self.addCleanup(self.home.cleanup)
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)

	def launchctl(self, output):
		return lambda argv, **options: subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

	def install(self):
		os.makedirs(os.path.dirname(launch_agent_path()), exist_ok=True)
		open(launch_agent_path(), "w").close()

	def test_installed_means_the_launch_agent_file_exists(self):
		self.assertFalse(agent_is_installed())
		self.install()
		self.assertTrue(agent_is_installed())

	def test_running_under_launchd(self):
		self.install()
		self.assertTrue(agent_is_running(self.launchctl("\tpid = 42\n"), uid=501))
		self.assertFalse(agent_is_running(self.launchctl("\tstate = waiting\n"), uid=501))

	def test_launchd_is_not_asked_when_not_installed(self):
		self.assertFalse(agent_is_running(mock.Mock(side_effect=AssertionError("launchctl was called")), uid=501))

	def test_running_in_the_foreground(self):
		os.makedirs(os.path.dirname(pid_file_path()), exist_ok=True)
		with open(pid_file_path(), "w") as handle:
			handle.write(str(os.getpid()))
		self.assertTrue(agent_is_running(mock.Mock(side_effect=AssertionError("launchctl was called")), uid=501))


class AgentTests(ShareCommandTestCase):
	def test_agent_is_installed_when_missing(self):
		self.commands(agent_installed=lambda: False, agent_running=lambda: False).add_share(self.mac_folder, VM_PATH)
		self.assertEqual(len(self.installs), 1)
		lines = self.output.getvalue().splitlines()
		self.assertEqual(lines[:4], [
			"Installed the wn agent and started it: /agents/com.wn.agent.plist",
			"It runs the Mac side of your shared folders.",
			"Log: {}".format(os.path.join(self.home.name, "Library", "Logs", "wn-agent.log")),
			"To remove it: python3 -m wn agent --uninstall",
		])

	def test_installed_agent_that_is_not_running_is_refused_before_guest_changes(self):
		with self.assertRaises(ShareError) as caught:
			self.commands(agent_running=lambda: False).add_share(self.mac_folder, VM_PATH)
		self.assertEqual(str(caught.exception), "the wn agent is installed but not running. Run python3 -m wn agent start, then run wn share again")
		self.assertEqual(self.runner.calls, [])
		self.assertEqual(self.installs, [])
		self.assertEqual(load_config().shares, ())


class AddShareTests(ShareCommandTestCase):
	def test_steps_run_in_order(self):
		snapshots = {}

		def snapshot(call):
			snapshots[call[:2]] = tuple(share.name for share in load_config().shares)

		self.runner.on_call = snapshot
		self.add()
		self.assertEqual(self.runner.calls[0][:2], ("run", PREPARE_SCRIPT))
		self.assertEqual([call[:2] for call in self.runner.calls[1:]], [
			("push", GUEST_CLIENT_KEY),
			("push", GUEST_SUPERVISOR),
			("push", FOLDER + "\\rclone.conf"),
			("push", FOLDER + "\\known_hosts"),
			("push", FOLDER + "\\share.json"),
			("run", TASK_SCRIPT),
			("pull", GUEST_STATUS_FILE),
		])
		self.assertEqual(snapshots[("push", FOLDER + "\\share.json")], ())
		self.assertEqual(snapshots[("run", TASK_SCRIPT)], (SHARE_NAME,))
		self.assertEqual(self.params_of(PREPARE_SCRIPT), {"name": SHARE_NAME, "vm_path": VM_PATH})
		self.assertEqual(self.params_of(TASK_SCRIPT), {"restart": True})
		self.assertEqual(load_config().shares, (Share(SHARE_NAME, "Windows", "mac_to_vm", self.mac_folder, VM_PATH, False, FIRST_PORT),))

	def test_guest_files_have_the_share_contents(self):
		self.add()
		self.assertEqual(self.runner.files[GUEST_CLIENT_KEY], CLIENT_KEY_TEXT)
		self.assertEqual(self.runner.files[GUEST_SUPERVISOR], guest_script_bytes(SUPERVISOR_SCRIPT).decode("utf-8"))
		self.assertIn("port = {}\n".format(FIRST_PORT), self.runner.files[FOLDER + "\\rclone.conf"])
		self.assertEqual(self.runner.files[FOLDER + "\\known_hosts"], "192.168.64.1 ssh-ed25519 AAAAhost\n[192.168.64.1]:47321 ssh-ed25519 AAAAhost\n")
		self.assertEqual(json.loads(self.runner.files[FOLDER + "\\share.json"]), {"name": SHARE_NAME, "mount_path": VM_PATH, "read_only": False, "port": FIRST_PORT})

	def test_no_fetch_when_winfsp_and_rclone_are_present(self):
		self.add()
		self.assertNotIn(FETCH_SCRIPT, self.runs())
		self.assertEqual(self.offers, [])
		self.assertNotIn(WINFSP_NOTICE, self.output.getvalue())

	def test_fetch_offers_only_what_is_missing(self):
		self.runner.results[PREPARE_SCRIPT] = prepare_result(rclone_sha256="0" * 64)
		open_during_fetch = []
		self.runner.on_call = lambda call: open_during_fetch.append(self.offers[0].open) if call[:2] == ("run", FETCH_SCRIPT) else None
		self.add()
		self.assertEqual(len(self.offers), 1)
		self.assertEqual(self.offers[0].files, {"rclone.exe": self.paths.windows_rclone})
		self.assertEqual(open_during_fetch, [True])
		self.assertEqual(self.params_of(FETCH_SCRIPT), {
			"rclone_url": "http://offer/rclone.exe",
			"rclone_sha256": RCLONE_WINDOWS_BINARY.sha256,
			"winfsp_url": "",
			"winfsp_sha256": "",
			"winfsp_name": "",
		})
		self.assertNotIn(WINFSP_NOTICE, self.output.getvalue())

	def test_winfsp_install_prints_the_notice_once(self):
		self.runner.results[PREPARE_SCRIPT] = prepare_result(winfsp="missing")
		self.add()
		self.assertEqual(self.offers[0].files, {WINFSP_SOURCE.file_name: TOOLS.winfsp_msi})
		self.assertEqual(self.params_of(FETCH_SCRIPT), {
			"rclone_url": "",
			"rclone_sha256": "",
			"winfsp_url": "http://offer/" + WINFSP_SOURCE.file_name,
			"winfsp_sha256": WINFSP_SOURCE.sha256,
			"winfsp_name": WINFSP_SOURCE.file_name,
		})
		lines = self.output.getvalue().splitlines()
		self.assertEqual(lines[-1], WINFSP_NOTICE)
		self.assertEqual(lines.count(WINFSP_NOTICE), 1)

	def test_hash_mismatch_in_windows_stops(self):
		self.runner.results[PREPARE_SCRIPT] = prepare_result(rclone_sha256=None)
		self.runner.results[FETCH_SCRIPT] = {"mismatch": "rclone.exe", "winfsp_exit": None, "winfsp_log": []}
		self.assert_refused_with_config_unchanged("the file Windows received does not match its SHA-256 (rclone.exe). Windows deleted it. Run wn share again")
		self.assertEqual(self.pushes(), [])

	def test_winfsp_restart_needed_stops(self):
		self.runner.results[PREPARE_SCRIPT] = prepare_result(winfsp="old")
		self.runner.results[FETCH_SCRIPT] = {"mismatch": None, "winfsp_exit": 3010, "winfsp_log": ["restart required"]}
		self.assert_refused_with_config_unchanged("WinFsp is installed but Windows needs a restart. Restart Windows, then run wn share again")
		self.assertEqual(self.pushes(), [])

	def test_winfsp_failure_shows_the_code_and_log(self):
		self.runner.results[PREPARE_SCRIPT] = prepare_result(winfsp="missing")
		self.runner.results[FETCH_SCRIPT] = {"mismatch": None, "winfsp_exit": 1603, "winfsp_log": ["line one", "line two"]}
		self.assert_refused_with_config_unchanged("WinFsp setup failed in Windows with exit code 1603:\nline one\nline two")
		self.assertEqual(self.pushes(), [])

	def test_refused_vm_paths(self):
		cases = (
			(prepare_result(vm_path="files"), VM_PATH + " has files in Windows. Pick an empty or new folder"),
			(prepare_result(vm_path="junction"), VM_PATH + " is already a mount point in Windows"),
			(prepare_result(vm_path="file"), VM_PATH + " is a file in Windows"),
			(prepare_result(parent_exists=False), "the folder that holds " + VM_PATH + " does not exist in Windows. Create it first"),
			(prepare_result(linked_parent="C:\\Users\\c"), "C:\\Users\\c in Windows is a link or mount point. Pick a path with no link in it"),
			(prepare_result(system_folder="C:\\Windows"), VM_PATH + " is inside C:\\Windows in Windows. Pick a folder outside it"),
		)
		for result, message in cases:
			with self.subTest(message=message):
				self.runner.calls = []
				self.runner.results[PREPARE_SCRIPT] = result
				self.assert_refused_with_config_unchanged(message)
				self.assertEqual(self.runs(), [PREPARE_SCRIPT])
				self.assertEqual(self.pushes(), [])

	def test_share_that_windows_is_removing_starts_the_supervisor_and_is_refused(self):
		self.runner.results[PREPARE_SCRIPT] = prepare_result(removing=True)
		self.assert_refused_with_config_unchanged("Windows is still removing a share named Projects and has files in its cache to upload first. Run wn share again later")
		self.assertEqual(self.runs(), [PREPARE_SCRIPT, TASK_SCRIPT])
		self.assertEqual(self.params_of(TASK_SCRIPT), {"restart": False})
		self.assertEqual(self.pushes(), [])

	def test_empty_vm_folder_is_accepted(self):
		self.runner.results[PREPARE_SCRIPT] = prepare_result(vm_path="empty")
		self.add()
		self.assertEqual(len(load_config().shares), 1)

	def test_unchanged_key_and_supervisor_are_not_pushed(self):
		self.runner.results[PREPARE_SCRIPT] = prepare_result(
			client_key_sha256=sha256_text(CLIENT_KEY_TEXT),
			supervisor_sha256=sha256_text(guest_script_bytes(SUPERVISOR_SCRIPT).decode("utf-8")),
		)
		self.add()
		self.assertNotIn(GUEST_CLIENT_KEY, self.pushes())
		self.assertNotIn(GUEST_SUPERVISOR, self.pushes())
		self.assertEqual(self.params_of(TASK_SCRIPT), {"restart": False})

	def test_changed_key_is_pushed_again(self):
		self.runner.results[PREPARE_SCRIPT] = prepare_result(client_key_sha256=sha256_text("old key"))
		self.add()
		self.assertIn(GUEST_CLIENT_KEY, self.pushes())

	def test_port_skips_used_and_unbindable_ports(self):
		other = self.make_mac_folder("Other")
		update_config_file({"shares": [Share("Other", "Windows", "mac_to_vm", other, "C:\\Other", False, FIRST_PORT).to_dict()]})
		self.commands(can_bind=lambda port: port != FIRST_PORT + 1).add_share(self.mac_folder, VM_PATH)
		self.assertEqual(load_config().shares[1].port, FIRST_PORT + 2)

	def test_default_name_and_backslash_path(self):
		self.add(vm_dir="C:/Users/c/Projects/")
		share = load_config().shares[0]
		self.assertEqual(share.name, SHARE_NAME)
		self.assertEqual(share.vm_path, VM_PATH)

	def test_name_option_overrides_the_default(self):
		folder = "C:\\ProgramData\\wn\\shares\\Work"
		self.serve("Work")
		self.mount("Work")
		self.add(name="Work")
		self.assertEqual(load_config().shares[0].name, "Work")
		self.assertIn(folder + "\\share.json", self.pushes())

	def test_output_lines(self):
		self.add()
		self.assertEqual(self.output.getvalue().splitlines(), [
			"Projects: {} is now at {} in Windows, read and write".format(self.mac_folder, VM_PATH),
			"Windows writes reach the Mac about 5 s after a file is closed. See wn share --help for the limits.",
		])

	def test_read_only_output_and_share(self):
		self.add(read_only=True)
		self.assertEqual(self.output.getvalue().splitlines()[0], "Projects: {} is now at {} in Windows, read only".format(self.mac_folder, VM_PATH))
		self.assertTrue(load_config().shares[0].read_only)
		self.assertTrue(json.loads(self.runner.files[FOLDER + "\\share.json"])["read_only"])

	def test_paths_with_special_characters_pass_as_data(self):
		mac_folder = self.make_mac_folder("My $files & 'docs' 100%")
		vm_path = "C:\\Users\\c\\My $files & 'docs' 100%"
		name = "My-files-docs-100-"
		self.serve(name)
		self.mount(name)
		self.add(mac_dir=mac_folder, vm_dir=vm_path)
		self.assertEqual(self.params_of(PREPARE_SCRIPT), {"name": name, "vm_path": vm_path})
		share_json = json.loads(self.runner.files["C:\\ProgramData\\wn\\shares\\{}\\share.json".format(name)])
		self.assertEqual(share_json["mount_path"], vm_path)
		share = load_config().shares[0]
		self.assertEqual((share.mac_path, share.vm_path), (mac_folder, vm_path))
		self.assertIn("{} is now at {} in Windows".format(mac_folder, vm_path), self.output.getvalue())

	def test_private_key_goes_only_through_push(self):
		self.runner.results[PREPARE_SCRIPT] = prepare_result(winfsp="missing", rclone_sha256=None)
		self.runner.results[TASK_SCRIPT] = GuestScriptError(TASK_SCRIPT, 1, "task failed")
		with self.assertRaises(GuestScriptError) as caught:
			self.add()
		secret = "secretkeymaterial"
		self.assertEqual(self.runner.files[GUEST_CLIENT_KEY], CLIENT_KEY_TEXT)
		for call in self.runner.calls:
			if call[0] == "run":
				self.assertNotIn(secret, json.dumps(call[2]))
		for offer in self.offers:
			self.assertNotIn(self.paths.client_key, offer.files.values())
		self.assertNotIn(secret, self.output.getvalue())
		self.assertNotIn(secret, str(caught.exception))



class StatusPollTests(ShareCommandTestCase):
	def test_missing_or_unparsable_status_means_not_ready_yet(self):
		answers = [None, "{", "\ufeff" + status_text({}), "\ufeff" + status_text({SHARE_NAME: {"state": "mounted", "detail": "", "pid": 1}})]
		self.runner.files[GUEST_STATUS_FILE] = lambda: answers.pop(0)
		self.add()
		self.assertEqual(answers, [])
		self.assertEqual(len(load_config().shares), 1)
		self.assertIn("is now at", self.output.getvalue())

	def test_status_that_is_not_an_object_is_not_ready(self):
		answers = ["[]", json.dumps({"shares": []}), json.dumps({"shares": {SHARE_NAME: "mounted"}}), status_text({SHARE_NAME: {"state": "mounted", "detail": ""}})]
		self.runner.files[GUEST_STATUS_FILE] = lambda: answers.pop(0)
		self.add()
		self.assertEqual(answers, [])

	def test_unparsable_status_never_counts_as_mounted(self):
		self.runner.files[GUEST_STATUS_FILE] = "{"
		with self.assertRaises(ShareError) as caught:
			self.add()
		self.assertIn("Windows did not mount the share within 30 s. Windows reports: unknown.", str(caught.exception))
		self.assert_rolled_back()


class RollbackTests(ShareCommandTestCase):
	def test_mount_timeout_rolls_back(self):
		self.runner.files[GUEST_STATUS_FILE] = status_text({SHARE_NAME: {"state": "waiting", "detail": "rclone exited with code 1; last log line: no route", "pid": None}})
		self.runner.files[FOLDER + "\\mount.log"] = "\ufeff" + "\r\n".join("log line {}".format(number) for number in range(30)) + "\r\n"
		with self.assertRaises(ShareError) as caught:
			self.add()
		message = str(caught.exception)
		self.assertTrue(message.startswith("Windows did not mount the share within 30 s. Windows reports: waiting, rclone exited with code 1; last log line: no route. Last lines of {}\\mount.log:\n".format(FOLDER)))
		self.assertIn("log line 10\nlog line 11", message)
		self.assertNotIn("log line 9\n", message)
		self.assertTrue(message.endswith("log line 29"))
		self.assertNotIn("\r", message)
		self.assert_rolled_back()
		self.assertGreaterEqual(self.clock.now, 30)

	def test_mount_timeout_without_a_log(self):
		self.runner.files[GUEST_STATUS_FILE] = status_text({})
		with self.assertRaises(ShareError) as caught:
			self.add()
		self.assertIn("Windows reports: unknown. Last lines of", str(caught.exception))
		self.assert_rolled_back()

	def test_agent_pickup_timeout_rolls_back(self):
		os.remove(self.paths.state_file)
		with self.assertRaises(ShareError) as caught:
			self.add()
		self.assertEqual(str(caught.exception), "the wn agent did not pick up the share within 15 s. Check python3 -m wn agent status. If you updated wn, run python3 -m wn agent --install so the agent runs the new code")
		self.assertNotIn(TASK_SCRIPT, self.runs())
		self.assert_rolled_back()

	def test_agent_reports_a_problem(self):
		self.serve(SHARE_NAME, "waiting", "the VM network is not up")
		with self.assertRaises(ShareError) as caught:
			self.add()
		self.assertEqual(str(caught.exception), "the Mac side of the share is waiting: the VM network is not up")
		self.assert_rolled_back()

	def test_task_script_failure_rolls_back(self):
		self.runner.results[TASK_SCRIPT] = GuestScriptError(TASK_SCRIPT, 1, "Register-ScheduledTask failed")
		with self.assertRaises(GuestScriptError):
			self.add()
		self.assert_rolled_back()
		self.assertEqual(self.pushes()[-1], FOLDER + "\\remove")

	def test_failed_remove_push_does_not_hide_the_error(self):
		self.runner.results[TASK_SCRIPT] = GuestScriptError(TASK_SCRIPT, 1, "Register-ScheduledTask failed")
		self.runner.push_errors[FOLDER + "\\remove"] = GuestError("utmctl file push failed")
		with self.assertRaises(ShareError) as caught:
			self.add()
		message = str(caught.exception)
		self.assertTrue(message.startswith("task.ps1 failed in Windows with exit code 1:\nRegister-ScheduledTask failed"))
		self.assertIn("Also could not ask Windows to remove the share: utmctl file push failed", message)
		self.assertEqual(load_config().shares, ())

	def test_failed_config_rollback_does_not_hide_the_error(self):
		self.runner.results[TASK_SCRIPT] = GuestScriptError(TASK_SCRIPT, 1, "Register-ScheduledTask failed")
		with mock.patch("wn.share_commands.update_config_file", side_effect=self.first_then_fail(update_config_file)):
			with self.assertRaises(ShareError) as caught:
				self.add()
		message = str(caught.exception)
		self.assertTrue(message.startswith("task.ps1 failed in Windows with exit code 1"))
		self.assertIn("Also could not remove share 'Projects' from the config: disk full", message)
		self.assertIn(FOLDER + "\\remove", self.runner.files)

	def first_then_fail(self, real_update):
		calls = []

		def update(values, path=None):
			calls.append(values)
			if len(calls) > 1:
				raise OSError("disk full")
			return real_update(values, path)

		return update

	def test_config_write_failure_asks_windows_to_remove_the_share(self):
		before = self.config_text()
		with mock.patch("wn.share_commands.update_config_file", side_effect=ConfigError("config is locked")):
			with self.assertRaises(ConfigError):
				self.add()
		self.assertEqual(self.config_text(), before)
		self.assertIn(FOLDER + "\\remove", self.runner.files)
		self.assertNotIn(TASK_SCRIPT, self.runs())

	def test_failure_before_the_share_file_leaves_no_remove_marker(self):
		self.runner.push_errors[FOLDER + "\\known_hosts"] = GuestError("utmctl file push failed")
		before = self.config_text()
		with self.assertRaises(GuestError):
			self.add()
		self.assertEqual(self.config_text(), before)
		self.assertNotIn(FOLDER + "\\remove", self.pushes())


class SavedShareTestCase(ShareCommandTestCase):
	def share(self, name=SHARE_NAME, vm="Windows", mac_path=None, vm_path=VM_PATH, read_only=False, port=FIRST_PORT):
		return Share(name, vm, "mac_to_vm", mac_path or self.mac_folder, vm_path, read_only, port)

	def save(self, *shares):
		update_config_file({"shares": [share.to_dict() for share in shares]})

	def serve_all(self, entries):
		with open(self.paths.state_file, "w") as handle:
			json.dump({"updated": "now", "shares": entries}, handle)

	def guest_reports(self, shares):
		self.runner.files[GUEST_STATUS_FILE] = status_text(shares)

	def pulls(self):
		return [call[1] for call in self.runner.calls if call[0] == "pull"]


class ListTests(SavedShareTestCase):
	def list_lines(self, **changes):
		self.assertEqual(self.commands(**changes).list_shares(), 0)
		return self.output.getvalue().splitlines()

	def test_no_shares(self):
		self.assertEqual(self.list_lines(), ["No shares. Add one with wn share <dir-on-mac> <dir-on-vm>"])
		self.assertEqual(self.runner.calls, [])

	def test_share_of_another_vm(self):
		self.save(self.share("Archive", vm="Test"))
		self.assertEqual(self.list_lines(), ["Archive: belongs to VM 'Test'"])
		self.assertEqual(self.runner.calls, [])
		self.assertNotIn(("status",), self.backend.calls)

	def test_agent_not_running(self):
		self.save(self.share())
		self.assertEqual(self.list_lines(agent_running=lambda: False), [
			"Projects: Mac folder {} in Windows at {}, read and write. Mac: the agent is not running. Run wn agent start. Windows: mounted.".format(self.mac_folder, VM_PATH),
		])

	def test_serving_and_mounted(self):
		self.save(self.share(mac_path="/Users/carter/Projects", vm_path="C:\\Users\\carter\\Projects"))
		self.assertEqual(self.list_lines(), [
			"Projects: Mac folder /Users/carter/Projects in Windows at C:\\Users\\carter\\Projects, read and write. Mac: serving on port 47321. Windows: mounted.",
		])

	def test_waiting_and_stopped_vm(self):
		self.save(self.share("Old", mac_path="/Users/carter/Old", vm_path="C:\\Old", read_only=True))
		self.serve("Old", "waiting", "the VM network is not up")
		self.backend.state = "stopped"
		self.assertEqual(self.list_lines(), [
			"Old: Mac folder /Users/carter/Old in Windows at C:\\Old, read only. Mac: waiting, the VM network is not up. Windows: unknown, the VM is stopped.",
		])
		self.assertEqual(self.runner.calls, [])

	def test_guest_agent_down(self):
		self.save(self.share())
		self.backend.ip_address = None
		self.assertTrue(self.list_lines()[0].endswith("Windows: unknown, no answer from the guest agent."))
		self.output.truncate(0)
		self.output.seek(0)
		self.backend.ip = mock.Mock(side_effect=UTMCtlError("utmctl ip-address failed with exit code 1"))
		self.assertTrue(self.list_lines()[0].endswith("Windows: unknown, no answer from the guest agent."))
		self.assertEqual(self.runner.calls, [])

	def test_unknown_vm(self):
		self.save(self.share())
		self.backend.status = mock.Mock(side_effect=UnknownVMError("UTM has no VM named 'Windows'. Known VMs: none"))
		self.assertTrue(self.list_lines()[0].endswith("Windows: unknown, VM 'Windows' is not in UTM."))

	def test_other_backend_error(self):
		self.save(self.share())
		self.backend.status = mock.Mock(side_effect=UTMCtlError("utmctl status timed out"))
		self.assertTrue(self.list_lines()[0].endswith("Windows: unknown, utmctl status timed out."))

	def test_guest_status_missing_or_unparsable(self):
		self.save(self.share())
		for answer in (None, "{"):
			with self.subTest(answer=answer):
				self.output.truncate(0)
				self.output.seek(0)
				self.runner.files[GUEST_STATUS_FILE] = answer
				self.assertTrue(self.list_lines()[0].endswith("Windows: unknown, Windows has no share status yet."))

	def test_missing_entries(self):
		self.save(self.share())
		os.remove(self.paths.state_file)
		self.guest_reports({})
		self.assertTrue(self.list_lines()[0].endswith("Mac: no state yet. Windows: not set up."))

	def test_windows_state_with_detail(self):
		self.save(self.share())
		self.guest_reports({SHARE_NAME: {"state": "blocked", "detail": "C:\\Users\\c\\Projects exists as a local folder with files", "pid": None}})
		self.assertTrue(self.list_lines()[0].endswith("Windows: blocked, C:\\Users\\c\\Projects exists as a local folder with files."))

	def test_read_only_wording(self):
		self.save(self.share(read_only=True))
		self.assertIn(", read only. Mac:", self.list_lines()[0])

	def test_list_pulls_the_guest_status_once(self):
		other = self.make_mac_folder("Other")
		self.save(self.share(), self.share("Other", mac_path=other, vm_path="C:\\Other", port=FIRST_PORT + 1), self.share("Archive", vm="Test", mac_path="/Archive", vm_path="C:\\Archive", port=FIRST_PORT + 2))
		self.serve_all({SHARE_NAME: {"state": "serving", "detail": "", "pid": 1}, "Other": {"state": "error", "detail": "cannot start rclone", "pid": None}})
		self.guest_reports({SHARE_NAME: {"state": "mounted", "detail": "", "pid": 2}, "Other": {"state": "starting", "detail": "", "pid": 3}})
		self.assertEqual(self.list_lines(), [
			"Projects: Mac folder {} in Windows at {}, read and write. Mac: serving on port {}. Windows: mounted.".format(self.mac_folder, VM_PATH, FIRST_PORT),
			"Other: Mac folder {} in Windows at C:\\Other, read and write. Mac: error, cannot start rclone. Windows: starting.".format(other),
			"Archive: belongs to VM 'Test'",
		])
		self.assertEqual(self.pulls(), [GUEST_STATUS_FILE])


class RemoveTests(SavedShareTestCase):
	def setUp(self):
		super().setUp()
		os.makedirs(self.paths.logs)
		with open(self.paths.log_file(SHARE_NAME), "w") as handle:
			handle.write("rclone log\n")

	def remove(self, name=SHARE_NAME, **changes):
		return self.commands(**changes).remove_share(name)

	def answer_in_turn(self, *answers):
		answers = list(answers)
		self.runner.files[GUEST_STATUS_FILE] = lambda: answers.pop(0) if len(answers) > 1 else answers[0]
		return answers

	def assert_refused_with_everything_kept(self, message, name=SHARE_NAME):
		before = self.config_text()
		with self.assertRaises(ShareError) as caught:
			self.remove(name)
		self.assertEqual(str(caught.exception), message)
		self.assertEqual(self.config_text(), before)
		self.assertTrue(os.path.exists(self.paths.log_file(SHARE_NAME)))
		self.assertNotIn(CLEANUP_SCRIPT, self.runs())

	def test_unknown_name_lists_the_names(self):
		other = self.make_mac_folder("Other")
		self.save(self.share(), self.share("Other", mac_path=other, vm_path="C:\\Other", port=FIRST_PORT + 1))
		self.assert_refused_with_everything_kept("no share named 'Missing'. Shares: Projects, Other", "Missing")
		self.save()
		self.assert_refused_with_everything_kept("no share named 'Missing'. There are no shares", "Missing")
		self.assertEqual(self.runner.calls, [])

	def test_name_ignores_case(self):
		self.save(self.share())
		self.guest_reports({})
		self.remove("projects")
		self.assertEqual(self.pushes(), [FOLDER + "\\remove"])
		self.assertEqual(load_config().shares, ())

	def test_share_of_another_vm_is_refused(self):
		self.save(self.share(vm="Test"))
		self.assert_refused_with_everything_kept("share 'Projects' belongs to VM 'Test'. Set vm_name to 'Test' in the config to remove it")
		self.assertEqual(self.runner.calls, [])

	def test_unknown_vm_removes_only_the_mac_side(self):
		self.save(self.share())
		self.backend.status = mock.Mock(side_effect=UnknownVMError("UTM has no VM named 'Windows'. Known VMs: none"))
		self.assertIsNone(self.remove())
		self.assertEqual(load_config().shares, ())
		self.assertFalse(os.path.exists(self.paths.log_file(SHARE_NAME)))
		self.assertEqual(self.output.getvalue(), "VM 'Windows' no longer exists in UTM, so only the Mac side was removed\n")
		self.assertEqual(self.runner.calls, [])

	def test_unknown_vm_without_a_log_file(self):
		self.save(self.share())
		os.remove(self.paths.log_file(SHARE_NAME))
		self.backend.status = mock.Mock(side_effect=UnknownVMError("UTM has no VM named 'Windows'. Known VMs: none"))
		self.remove()
		self.assertEqual(load_config().shares, ())

	def test_stopped_vm_keeps_everything(self):
		self.save(self.share())
		self.backend.state = "stopped"
		self.assert_refused_with_everything_kept("VM 'Windows' is stopped. Start it with wn start and run wn unshare again, so wn can remove the share from Windows too")
		self.assertEqual(self.runner.calls, [])

	def test_guest_agent_down_keeps_everything(self):
		self.save(self.share())
		self.backend.ip_address = None
		self.assert_refused_with_everything_kept("Windows is still starting (no answer from the guest agent). Try again in a minute")
		self.assertEqual(self.runner.calls, [])

	def test_removes_after_windows_drops_the_share(self):
		self.save(self.share())
		removing = {SHARE_NAME: {"state": "removing", "detail": "waiting for rclone to report its uploads", "pid": 5678}}
		self.answer_in_turn(status_text({SHARE_NAME: {"state": "mounted", "detail": "", "pid": 5678}}), status_text(removing), status_text(removing), status_text({}))
		configs = []
		self.runner.on_call = lambda call: configs.append(tuple(share.name for share in load_config().shares)) if call[0] == "pull" else None
		self.assertIsNone(self.remove())
		self.assertEqual(self.pushes(), [FOLDER + "\\remove"])
		self.assertEqual(self.runner.files[FOLDER + "\\remove"], "")
		self.assertEqual(len(self.pulls()), 4)
		self.assertEqual(configs, [(SHARE_NAME,)] * 4)
		self.assertEqual(load_config().shares, ())
		self.assertFalse(os.path.exists(self.paths.log_file(SHARE_NAME)))
		self.assertEqual(self.runs(), [CLEANUP_SCRIPT])
		self.assertEqual(self.output.getvalue().splitlines(), [
			"Waiting for Windows to finish its uploads to the Mac (waiting for rclone to report its uploads). Press Ctrl+C to stop waiting. The share then stays in place until you run wn unshare Projects again.",
			"Removed Projects. {} is gone from Windows. The files stay in {} on the Mac.".format(VM_PATH, self.mac_folder),
			"Removed the wn share files and the wn-shares task from Windows. WinFsp stays installed. To remove it, open Settings > Apps in Windows.",
		])

	def test_missing_or_half_written_status_is_not_gone(self):
		self.save(self.share())
		answers = self.answer_in_turn(None, "", "{\"updated\": \"2026", "\ufeff" + status_text({SHARE_NAME: {"state": "removing", "detail": "", "pid": 1}}), status_text({}))
		self.remove()
		self.assertEqual(len(self.pulls()), 5)
		self.assertEqual(answers, [status_text({})])
		self.assertEqual(load_config().shares, ())

	def test_unparsable_status_never_counts_as_gone(self):
		self.save(self.share())
		self.runner.files[GUEST_STATUS_FILE] = "{"
		self.assert_refused_with_everything_kept("Windows did not remove the share within 180 s. Windows reports: unknown. Run wn unshare Projects again later")
		self.assertGreaterEqual(self.clock.now, 180)

	def test_other_state_at_the_timeout_raises(self):
		self.save(self.share())
		self.assert_refused_with_everything_kept("Windows did not remove the share within 180 s. Windows reports: mounted. Run wn unshare Projects again later")

	def test_pending_uploads_wait_without_a_time_limit(self):
		self.save(self.share())
		pending = {SHARE_NAME: {"state": "removing", "detail": "3 files to upload to the Mac", "pid": 1, "pending": 3}}
		self.runner.files[GUEST_STATUS_FILE] = lambda: status_text({}) if self.clock.now >= 600 else status_text(pending)
		configs = []
		self.runner.on_call = lambda call: configs.append(len(load_config().shares)) if call[0] == "pull" else None
		self.assertIsNone(self.remove())
		self.assertGreaterEqual(self.clock.now, 600)
		self.assertEqual(set(configs), {1})
		self.assertEqual(load_config().shares, ())
		lines = self.output.getvalue().splitlines()
		progress = "Waiting for Windows to upload 3 files to the Mac. Press Ctrl+C to stop waiting. The share then stays in place until you run wn unshare Projects again."
		self.assertEqual(lines.count(progress), 600 // REMOVE_PROGRESS_SECONDS)
		self.assertEqual(lines[0], progress)

	def test_removing_without_a_count_waits_without_a_time_limit(self):
		self.save(self.share())
		removing = {SHARE_NAME: {"state": "removing", "detail": "", "pid": 1}}
		self.runner.files[GUEST_STATUS_FILE] = lambda: status_text({}) if self.clock.now >= 600 else status_text(removing)
		self.assertIsNone(self.remove())
		self.assertGreaterEqual(self.clock.now, 600)
		self.assertEqual(load_config().shares, ())
		self.assertIn("Waiting for Windows to finish its uploads to the Mac. Press Ctrl+C to stop waiting.", self.output.getvalue())

	def test_ctrl_c_while_waiting_keeps_the_mac_side(self):
		self.save(self.share())
		self.guest_reports({SHARE_NAME: {"state": "removing", "detail": "3 files to upload to the Mac", "pid": 1, "pending": 3}})
		before = self.config_text()

		def interrupt(seconds):
			raise KeyboardInterrupt

		with self.assertRaises(KeyboardInterrupt):
			self.remove(sleep=interrupt)
		self.assertEqual(self.config_text(), before)
		self.assertTrue(os.path.exists(self.paths.log_file(SHARE_NAME)))

	def test_share_already_gone_from_windows(self):
		self.save(self.share())
		self.runner.push_errors[FOLDER + "\\remove"] = UTMCtlError("utmctl file failed with exit code 1: Error: cannot open file")
		self.guest_reports({})
		self.remove()
		self.assertEqual(load_config().shares, ())

	def test_failed_push_with_no_good_status_keeps_everything(self):
		self.save(self.share())
		self.runner.push_errors[FOLDER + "\\remove"] = UTMCtlError("utmctl file failed with exit code 1: Error: cannot open file")
		for answer in (None, "{", status_text({SHARE_NAME: {"state": "mounted", "detail": "", "pid": 1}})):
			with self.subTest(answer=answer):
				self.runner.files[GUEST_STATUS_FILE] = answer
				before = self.config_text()
				with self.assertRaises(UTMCtlError):
					self.remove()
				self.assertEqual(self.config_text(), before)

	def test_cleanup_runs_only_after_the_last_share(self):
		other = self.make_mac_folder("Other")
		self.save(self.share(), self.share("Other", mac_path=other, vm_path="C:\\Other", port=FIRST_PORT + 1), self.share("Archive", vm="Test", mac_path="/Archive", vm_path="C:\\Archive", port=FIRST_PORT + 2))
		self.guest_reports({})
		self.remove()
		self.assertEqual(self.runs(), [])
		self.assertEqual([share.name for share in load_config().shares], ["Other", "Archive"])
		self.remove("Other")
		self.assertEqual(self.runs(), [CLEANUP_SCRIPT])
		self.assertEqual([share.name for share in load_config().shares], ["Archive"])


	def test_cleanup_waits_while_windows_lists_other_shares(self):
		self.save(self.share())
		self.guest_reports({"Leftover": {"state": "removing", "detail": "", "pid": 1}})
		self.remove()
		self.assertEqual(load_config().shares, ())
		self.assertEqual(self.runs(), [])
		self.assertEqual(self.output.getvalue().splitlines()[-1], "Windows still lists other wn shares (Leftover), so wn kept its share files and the wn-shares task in Windows.")


if __name__ == "__main__":
	unittest.main()
