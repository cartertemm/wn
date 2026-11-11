import os
import tempfile
import unittest

from fake_qmp_server import FakeQMPServer, error_reply, event, reply
from wn.qmp import QMPClient, QMPConnectionError, QMPError


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


if __name__ == "__main__":
	unittest.main()
