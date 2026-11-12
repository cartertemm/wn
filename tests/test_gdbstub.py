import socket
import unittest

from fake_gdb_server import FakeGdbServer
from wn.gdbstub import GdbStubClient, GdbStubError, checksum, frame

BLOCK_ADDRESS = 0x50000000
REGION_SIZE = 0x1000


def unused_port():
	with socket.socket() as probe:
		probe.bind(("127.0.0.1", 0))
		return probe.getsockname()[1]


class FramingTests(unittest.TestCase):
	def test_checksum_is_sum_modulo_256(self):
		self.assertEqual(checksum("OK"), "9a")
		self.assertEqual(checksum(""), "00")

	def test_frame(self):
		self.assertEqual(frame("qC"), b"$qC#b4")


class ClientTests(unittest.TestCase):
	def test_session_turns_on_physical_memory_and_detaches(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}) as server:
			with GdbStubClient(server.port) as client:
				self.assertTrue(server.paused)
				self.assertTrue(server.physical)
			self.assertFalse(server.paused)
		self.assertEqual(server.packets[0], "qSupported:multiprocess+")
		self.assertIn("Qqemu.PhyMemMode:1", server.packets)
		self.assertEqual(server.packets[-1], "D;1")

	def test_write_then_read(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}) as server:
			with GdbStubClient(server.port) as client:
				client.write_memory(BLOCK_ADDRESS + 8, b"\x01\x02\xff")
				self.assertEqual(client.read_memory(BLOCK_ADDRESS + 8, 3), b"\x01\x02\xff")
		self.assertEqual(server.read(BLOCK_ADDRESS + 8, 3), b"\x01\x02\xff")
		self.assertIn("M50000008,3:0102ff", server.packets)
		self.assertIn("m50000008,3", server.packets)

	def test_read_unmapped_address_raises(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}) as server:
			with GdbStubClient(server.port) as client:
				with self.assertRaises(GdbStubError) as caught:
					client.read_memory(0x90000000, 4)
		self.assertIn("E14", str(caught.exception))

	def test_detaches_after_an_error(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}) as server:
			with self.assertRaises(GdbStubError):
				with GdbStubClient(server.port) as client:
					client.write_memory(0x90000000, b"\x00")
		self.assertFalse(server.paused)

	def test_resends_after_nack(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}, nack_first=2) as server:
			with GdbStubClient(server.port) as client:
				self.assertEqual(client.read_memory(BLOCK_ADDRESS, 2), b"\x00\x00")

	def test_asks_again_after_bad_checksum(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}, corrupt_first=2) as server:
			with GdbStubClient(server.port) as client:
				self.assertEqual(client.read_memory(BLOCK_ADDRESS, 2), b"\x00\x00")

	def test_gives_up_after_retries(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}, nack_first=100) as server:
			with self.assertRaises(GdbStubError) as caught:
				with GdbStubClient(server.port):
					pass
		self.assertIn("rejected", str(caught.exception))

	def test_connection_closed_by_stub(self):
		with FakeGdbServer({BLOCK_ADDRESS: REGION_SIZE}, drop_after=1) as server:
			with self.assertRaises(GdbStubError) as caught:
				with GdbStubClient(server.port):
					pass
		self.assertIn("closed", str(caught.exception))

	def test_connection_refused(self):
		port = unused_port()
		with self.assertRaises(GdbStubError) as caught:
			with GdbStubClient(port):
				pass
		self.assertIn("127.0.0.1:{}".format(port), str(caught.exception))


if __name__ == "__main__":
	unittest.main()
