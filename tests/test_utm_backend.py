import json
import os
import socket
import sys
import tempfile
import unittest

from fake_qmp_server import ACCEPT_TIMEOUT_SECONDS, FakeQMPServer, reply
from wn.backend import Backend, VMStatus
from wn.backends.utm import (
	DEFAULT_QMP_PORT,
	DEFAULT_UTMCTL_PATH,
	TCPQMPClient,
	UnknownVMError,
	UTMBackend,
	UTMCtlError,
	UTMCtlMissingError,
	open_qmp,
)
from wn.qmp import QMPClient, QMPConnectionError

FAKE_UTMCTL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_utmctl.py")
VM_UUID = "9E1A6F38-2D0B-4C41-8F7E-3B5A1C2D0001"
OTHER_UUID = "9E1A6F38-2D0B-4C41-8F7E-3B5A1C2D0002"


class TCPFakeQMPServer(FakeQMPServer):
	"""The shared fake QMP server, listening on 127.0.0.1 instead of a Unix socket."""

	def __init__(self, handlers=None):
		super().__init__(handlers)
		self._listener.close()
		self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self._listener.bind(("127.0.0.1", 0))
		self._listener.listen(1)
		self._listener.settimeout(ACCEPT_TIMEOUT_SECONDS)
		self.port = self._listener.getsockname()[1]


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
		self.assertEqual(backend.qmp_port, DEFAULT_QMP_PORT)
		self.assertEqual(DEFAULT_UTMCTL_PATH, "/Applications/UTM.app/Contents/MacOS/utmctl")
		self.assertEqual(DEFAULT_QMP_PORT, 4444)

	def test_reads_attributes_from_config_object(self):
		class ConfigObject:
			vm_name = "Win11"
			qmp_port = 5555

		backend = UTMBackend(ConfigObject())
		self.assertEqual(backend.vm_name, "Win11")
		self.assertEqual(backend.qmp_port, 5555)
		self.assertEqual(backend.utmctl_command, [DEFAULT_UTMCTL_PATH])

	def test_qmp_socket_path_is_tcp_address(self):
		self.assertEqual(UTMBackend({}).qmp_socket_path(), "tcp:127.0.0.1:4444")
		self.assertEqual(UTMBackend({"qmp_port": 4500}).qmp_socket_path(), "tcp:127.0.0.1:4500")


class OpenQMPTests(unittest.TestCase):
	def test_tcp_address(self):
		handlers = {"query-status": lambda request: [reply(request, {"status": "running"})]}
		with TCPFakeQMPServer(handlers) as server:
			client = open_qmp("tcp:127.0.0.1:{}".format(server.port))
			try:
				self.assertIsInstance(client, TCPQMPClient)
				self.assertEqual(client.execute("query-status"), {"status": "running"})
			finally:
				client.close()
		self.assertEqual(server.received[0]["execute"], "qmp_capabilities")

	def test_unix_path(self):
		with FakeQMPServer() as server:
			client = open_qmp(server.socket_path)
			try:
				self.assertIs(type(client), QMPClient)
				self.assertIsNotNone(client.greeting)
			finally:
				client.close()

	def test_tcp_client_as_context_manager(self):
		with TCPFakeQMPServer() as server:
			with TCPQMPClient("tcp:127.0.0.1:{}".format(server.port)) as client:
				self.assertIn("QMP", client.greeting)

	def test_tcp_connection_refused(self):
		unused = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		unused.bind(("127.0.0.1", 0))
		port = unused.getsockname()[1]
		unused.close()
		with self.assertRaises(QMPConnectionError) as context:
			open_qmp("tcp:127.0.0.1:{}".format(port))
		self.assertIn(str(port), str(context.exception))

	def test_bad_tcp_address(self):
		with self.assertRaises(QMPConnectionError):
			open_qmp("tcp:127.0.0.1")


if __name__ == "__main__":
	unittest.main()
