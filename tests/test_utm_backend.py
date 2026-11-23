import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from wn.backend import Backend, VMStatus
from wn.backends.utm import (
	DEFAULT_UTMCTL_PATH,
	UnknownVMError,
	UTMBackend,
	UTMCtlError,
	UTMCtlMissingError,
	UTMCtlTimeoutError,
)
from wn.config import Config

FAKE_UTMCTL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_utmctl.py")
VM_UUID = "9E1A6F38-2D0B-4C41-8F7E-3B5A1C2D0001"
OTHER_UUID = "9E1A6F38-2D0B-4C41-8F7E-3B5A1C2D0002"
HANGING_UTMCTL_SECONDS = 10
TEST_TIMEOUT_SECONDS = 0.5


class FakeUTMCtlTestCase(unittest.TestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.state_path = os.path.join(directory.name, "state.json")
		self.write_state({"vms": [
			{"uuid": VM_UUID, "name": "Windows 11", "status": "stopped", "ip_addresses": []},
			{"uuid": OTHER_UUID, "name": "Linux", "status": "started", "ip_addresses": []},
		]})
		previous = os.environ.get("FAKE_UTMCTL_STATE")
		os.environ["FAKE_UTMCTL_STATE"] = self.state_path
		self.addCleanup(self.restore_environment, previous)

	@staticmethod
	def restore_environment(previous):
		if previous is None:
			os.environ.pop("FAKE_UTMCTL_STATE", None)
		else:
			os.environ["FAKE_UTMCTL_STATE"] = previous

	def write_state(self, state):
		with open(self.state_path, "w") as state_file:
			json.dump(state, state_file)

	def read_state(self):
		with open(self.state_path) as state_file:
			return json.load(state_file)

	def update_vm(self, **fields):
		state = self.read_state()
		state["vms"][0].update(fields)
		self.write_state(state)

	def fail_command(self, command, message):
		state = self.read_state()
		state.setdefault("fail", {})[command] = message
		self.write_state(state)

	def backend(self, **settings):
		values = {"vm_name": "Windows 11", "utmctl_path": [sys.executable, FAKE_UTMCTL]}
		values.update(settings)
		return UTMBackend(values)


class UTMBackendLifecycleTests(FakeUTMCtlTestCase):
	def test_is_a_backend(self):
		self.assertIsInstance(self.backend(), Backend)

	def test_status_of_stopped_vm(self):
		self.assertEqual(self.backend().status(), VMStatus(name="Windows 11", state="stopped"))

	def test_started_maps_to_running(self):
		self.update_vm(status="started")
		self.assertEqual(self.backend().status().state, "running")

	def test_every_utm_state_maps(self):
		expected = {
			"stopped": "stopped",
			"starting": "starting",
			"started": "running",
			"pausing": "pausing",
			"paused": "paused",
			"resuming": "resuming",
			"stopping": "stopping",
		}
		for utm_state, wn_state in expected.items():
			with self.subTest(utm_state=utm_state):
				self.update_vm(status=utm_state)
				self.assertEqual(self.backend().status().state, wn_state)

	def test_unexpected_state_raises(self):
		self.update_vm(status="exploded")
		with self.assertRaises(UTMCtlError) as context:
			self.backend().status()
		self.assertIn("exploded", str(context.exception))

	def test_start_then_stop_request(self):
		backend = self.backend()
		backend.start()
		self.assertEqual(backend.status().state, "running")
		backend.stop()
		self.assertEqual(backend.status().state, "stopped")
		self.assertEqual(self.read_state()["vms"][0]["last_stop_flags"], ["--request"])

	def test_stop_force(self):
		self.update_vm(status="started")
		self.backend().stop(force=True)
		self.assertEqual(self.read_state()["vms"][0]["last_stop_flags"], ["--force"])

	def test_name_with_spaces_is_one_argument(self):
		self.backend().start()
		self.assertIn(["start", "Windows 11"], self.read_state()["calls"])

	def test_vm_found_by_uuid(self):
		self.assertEqual(self.backend(vm_name=VM_UUID).status().state, "stopped")

	def test_utmctl_path_as_string(self):
		script_directory = tempfile.TemporaryDirectory()
		self.addCleanup(script_directory.cleanup)
		wrapper = os.path.join(script_directory.name, "utmctl")
		with open(wrapper, "w") as wrapper_file:
			wrapper_file.write("#!/bin/sh\nexec '{}' '{}' \"$@\"\n".format(sys.executable, FAKE_UTMCTL))
		os.chmod(wrapper, 0o755)
		self.assertEqual(self.backend(utmctl_path=wrapper).status().state, "stopped")


class UTMBackendErrorTests(FakeUTMCtlTestCase):
	def test_missing_utmctl(self):
		backend = self.backend(utmctl_path="/no/such/utmctl")
		with self.assertRaises(UTMCtlMissingError) as context:
			backend.status()
		self.assertIn("/no/such/utmctl", str(context.exception))

	def test_utmctl_timeout(self):
		backend = self.backend(utmctl_path=[sys.executable, "-c", "import time; time.sleep({})".format(HANGING_UTMCTL_SECONDS)])
		with mock.patch("wn.backends.utm.UTMCTL_TIMEOUT_SECONDS", TEST_TIMEOUT_SECONDS):
			with mock.patch("wn.backends.utm.subprocess.run", wraps=subprocess.run) as run:
				with self.assertRaises(UTMCtlTimeoutError) as context:
					backend.status()
		self.assertEqual(run.call_count, 1)
		self.assertEqual(run.call_args.kwargs["timeout"], TEST_TIMEOUT_SECONDS)
		self.assertIn("dialog", str(context.exception))
		self.assertIn("Automation", str(context.exception))

	def test_unknown_vm_on_status(self):
		with self.assertRaises(UnknownVMError) as context:
			self.backend(vm_name="Nope").status()
		message = str(context.exception)
		self.assertIn("Nope", message)
		self.assertIn("Windows 11", message)
		self.assertIn("Linux", message)

	def test_unknown_vm_on_start(self):
		with self.assertRaises(UnknownVMError):
			self.backend(vm_name="Nope").start()

	def test_unknown_vm_on_stop(self):
		with self.assertRaises(UnknownVMError):
			self.backend(vm_name="Nope").stop()

	def test_failure_includes_stderr(self):
		self.fail_command("start", "Error: errAEEventNotPermitted")
		with self.assertRaises(UTMCtlError) as context:
			self.backend().start()
		self.assertNotIsInstance(context.exception, UnknownVMError)
		self.assertIn("errAEEventNotPermitted", str(context.exception))
		self.assertIn("start", str(context.exception))

	def test_not_found_stderr_is_unknown_vm(self):
		self.fail_command("start", "Error: Virtual machine not found.")
		with self.assertRaises(UnknownVMError) as context:
			self.backend().start()
		self.assertIn("Windows 11", str(context.exception))

	def test_not_found_stderr_is_unknown_vm_when_list_fails(self):
		self.fail_command("ip-address", "Error: Virtual machine not found.")
		self.fail_command("list", "Error: UTM is not running")
		with self.assertRaises(UnknownVMError):
			self.backend().ip()

	def test_list_failure_includes_stderr(self):
		self.fail_command("list", "Error: UTM is not running")
		with self.assertRaises(UTMCtlError) as context:
			self.backend().status()
		self.assertIn("UTM is not running", str(context.exception))


class UTMBackendIPTests(FakeUTMCtlTestCase):
	def test_first_ipv4_address(self):
		self.update_vm(status="started", ip_addresses=["fe80::1", "192.168.64.7", "10.0.0.2"])
		self.assertEqual(self.backend().ip(), "192.168.64.7")

	def test_no_ipv4_address_raises(self):
		self.update_vm(status="started", ip_addresses=["fe80::1"])
		with self.assertRaises(UTMCtlError) as context:
			self.backend().ip()
		self.assertIn("IPv4", str(context.exception))

	def test_no_address_raises(self):
		with self.assertRaises(UTMCtlError):
			self.backend().ip()

	def test_unknown_vm(self):
		with self.assertRaises(UnknownVMError):
			self.backend(vm_name="Nope").ip()


class UTMBackendConfigTests(unittest.TestCase):
	def test_defaults(self):
		backend = UTMBackend({})
		self.assertEqual(backend.vm_name, "Windows")
		self.assertEqual(backend.utmctl_command, [DEFAULT_UTMCTL_PATH])
		self.assertEqual(DEFAULT_UTMCTL_PATH, "/Applications/UTM.app/Contents/MacOS/utmctl")

	def test_reads_attributes_from_config_object(self):
		class ConfigObject:
			vm_name = "Win11"

		backend = UTMBackend(ConfigObject())
		self.assertEqual(backend.vm_name, "Win11")
		self.assertEqual(backend.utmctl_command, [DEFAULT_UTMCTL_PATH])

	def test_reads_wn_config(self):
		backend = UTMBackend(Config(vm_name="Win11", utmctl_path="/opt/utmctl"))
		self.assertEqual(backend.vm_name, "Win11")
		self.assertEqual(backend.utmctl_command, ["/opt/utmctl"])

	def test_wn_config_defaults_match_backend_defaults(self):
		backend = UTMBackend(Config())
		self.assertEqual(backend.vm_name, "Windows")
		self.assertEqual(backend.utmctl_command, [DEFAULT_UTMCTL_PATH])

	def test_qmp_sockets_are_named_after_the_vm(self):
		container = "/home/me/Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM"
		with mock.patch.dict(os.environ, {"HOME": "/home/me"}):
			self.assertEqual(UTMBackend({}).qmp_socket_path(), container + "/wn-Windows.qmp")
			self.assertEqual(UTMBackend({}).qmp_socket_path(session=True), container + "/wn-Windows-session.qmp")
			self.assertEqual(UTMBackend({"vm_name": "Win11"}).qmp_socket_path(), container + "/wn-Win11.qmp")


if __name__ == "__main__":
	unittest.main()


class UTMBackendGuestTests(FakeUTMCtlTestCase):
	def test_pull_file(self):
		self.update_vm(files={"C:\\wn-setup-done": "done\r\n"})
		self.assertEqual(self.backend().pull_file("C:\\wn-setup-done").strip(), "done")

	def test_pull_file_replaces_bytes_that_are_not_utf_8(self):
		backend = self.backend(utmctl_path=[sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'bad \\xff byte')"])
		self.assertEqual(backend.pull_file("C:\\out.txt"), "bad \ufffd byte")

	def test_pull_missing_file_raises(self):
		with self.assertRaises(UTMCtlError):
			self.backend().pull_file("C:\\missing")

	def test_delete(self):
		self.backend().delete()
		self.assertEqual([vm["name"] for vm in self.read_state()["vms"]], ["Linux"])

	def test_exists(self):
		self.assertTrue(self.backend().exists())
		self.assertFalse(self.backend(vm_name="Other").exists())


class UTMBackendPushAndExecTests(FakeUTMCtlTestCase):
	def test_push_file_sends_the_bytes_on_stdin(self):
		backend = self.backend()
		backend.push_file("C:\\x.ps1", b"\xef\xbb\xbfhello\r\n")
		state = self.read_state()
		self.assertEqual(state["vms"][0]["files"]["C:\\x.ps1"], "\ufeffhello\r\n")
		self.assertEqual(state["calls"][-1], ["file", "push", "Windows 11", "C:\\x.ps1"])
		self.assertEqual(backend.pull_file("C:\\x.ps1"), "\ufeffhello\n")

	def test_exec_command_passes_the_argv_after_cmd(self):
		self.backend().exec_command("cmd.exe", "/c", "C:\\a.cmd")
		self.assertEqual(self.read_state()["calls"][-1], ["exec", "Windows 11", "--cmd", "cmd.exe", "/c", "C:\\a.cmd"])

	def test_push_file_to_an_unknown_vm_raises_unknown_vm_error(self):
		with self.assertRaises(UnknownVMError):
			self.backend(vm_name="Other").push_file("C:\\x.ps1", b"hello")

	def test_exec_command_failure_raises_utmctl_error(self):
		self.fail_command("exec", "Error: guest agent not running")
		with self.assertRaises(UTMCtlError) as context:
			self.backend().exec_command("cmd.exe")
		self.assertNotIsInstance(context.exception, UnknownVMError)
		self.assertIn("Error: guest agent not running", str(context.exception))
