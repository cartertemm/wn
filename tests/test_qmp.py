import os
import tempfile
import unittest

from fake_qmp_server import FakeQMPServer, TCPFakeQMPServer, error_reply, event, reply
from fakes import unused_port
from wn.qmp import QMPClient, QMPConnectionError, QMPError, TCPQMPClient, qmp_client_for


class QMPClientTests(unittest.TestCase):
	def test_handshake_sends_capabilities_first(self):
		with FakeQMPServer() as server:
			with QMPClient(server.socket_path) as client:
				self.assertEqual(client.greeting["QMP"]["version"]["qemu"]["major"], 11)
		self.assertEqual(server.received[0]["execute"], "qmp_capabilities")

	def test_execute_returns_return_value(self):
		handlers = {"query-status": lambda request: [reply(request, {"status": "running", "running": True})]}
		with FakeQMPServer(handlers) as server:
			with QMPClient(server.socket_path) as client:
				self.assertEqual(client.execute("query-status"), {"status": "running", "running": True})

	def test_execute_sends_arguments(self):
		handlers = {"send-key": lambda request: [reply(request, {})]}
		arguments = {"keys": [{"type": "qcode", "data": "a"}]}
		with FakeQMPServer(handlers) as server:
			with QMPClient(server.socket_path) as client:
				client.execute("send-key", arguments)
		self.assertEqual(server.received[1]["arguments"], arguments)

	def test_execute_raises_qmp_error(self):
		handlers = {"send-key": lambda request: [error_reply(request, "GenericError", "Invalid parameter 'keys'")]}
		with FakeQMPServer(handlers) as server:
			with QMPClient(server.socket_path) as client:
				with self.assertRaises(QMPError) as context:
					client.execute("send-key", {"keys": []})
		self.assertEqual(context.exception.error_class, "GenericError")
		self.assertEqual(context.exception.description, "Invalid parameter 'keys'")
		self.assertIn("GenericError", str(context.exception))

	def test_unknown_command_raises(self):
		with FakeQMPServer() as server:
			with QMPClient(server.socket_path) as client:
				with self.assertRaises(QMPError) as context:
					client.execute("no-such-command")
		self.assertEqual(context.exception.error_class, "CommandNotFound")

	def test_events_are_skipped_and_collected(self):
		handlers = {"query-status": lambda request: [
			event("RTC_CHANGE", {"offset": 1}),
			reply(request, {"status": "running"}),
		]}
		with FakeQMPServer(handlers, events_after_greeting=[event("RESUME")]) as server:
			with QMPClient(server.socket_path) as client:
				self.assertEqual(client.execute("query-status"), {"status": "running"})
				self.assertEqual([message["event"] for message in client.events], ["RESUME", "RTC_CHANGE"])

	def test_consecutive_commands(self):
		counter = {"calls": 0}

		def count(request):
			counter["calls"] += 1
			return [reply(request, counter["calls"])]

		with FakeQMPServer({"count": count}) as server:
			with QMPClient(server.socket_path) as client:
				self.assertEqual([client.execute("count") for _ in range(3)], [1, 2, 3])

	def test_missing_socket_raises_connection_error(self):
		missing_path = os.path.join(tempfile.gettempdir(), "wn-missing-qmp.sock")
		with self.assertRaises(QMPConnectionError) as context:
			QMPClient(missing_path).connect()
		self.assertIn(missing_path, str(context.exception))


class TCPQMPClientTests(unittest.TestCase):
	def test_handshake_and_execute(self):
		handlers = {"query-status": lambda request: [reply(request, {"status": "running"})]}
		with TCPFakeQMPServer(handlers) as server:
			with TCPQMPClient(server.address) as client:
				self.assertIn("QMP", client.greeting)
				self.assertEqual(client.execute("query-status"), {"status": "running"})
		self.assertEqual(server.received[0]["execute"], "qmp_capabilities")

	def test_host_and_port_are_parsed(self):
		client = TCPQMPClient("tcp:127.0.0.1:4444")
		self.assertEqual((client.host, client.port), ("127.0.0.1", 4444))

	def test_connection_refused(self):
		port = unused_port()
		with self.assertRaises(QMPConnectionError) as context:
			TCPQMPClient("tcp:127.0.0.1:{}".format(port)).connect()
		self.assertIn(str(port), str(context.exception))

	def test_bad_address(self):
		for address in ("tcp:127.0.0.1", "tcp::4444", "tcp:127.0.0.1:port"):
			with self.subTest(address=address):
				with self.assertRaises(QMPConnectionError):
					TCPQMPClient(address)


class QMPClientForTests(unittest.TestCase):
	def test_tcp_address_gives_tcp_client(self):
		client = qmp_client_for("tcp:127.0.0.1:4444")
		self.assertIsInstance(client, TCPQMPClient)
		self.assertEqual(client.port, 4444)

	def test_path_gives_unix_client(self):
		client = qmp_client_for("/tmp/qmp.sock")
		self.assertIs(type(client), QMPClient)
		self.assertEqual(client.socket_path, "/tmp/qmp.sock")

	def test_client_is_not_connected_until_entered(self):
		with TCPFakeQMPServer() as server:
			client = qmp_client_for(server.address)
			self.assertIsNone(client.greeting)
			with client:
				self.assertIn("QMP", client.greeting)

	def test_unix_client_connects(self):
		with FakeQMPServer() as server:
			with qmp_client_for(server.socket_path) as client:
				self.assertIsNotNone(client.greeting)


if __name__ == "__main__":
	unittest.main()
