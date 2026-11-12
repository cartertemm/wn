"""A minimal client for QEMU's GDB stub, used to read and write guest physical memory.

QEMU pauses the VM as soon as a client connects and keeps it paused until the client detaches.
Use the client as a context manager: it detaches on exit, even after an error, so the VM runs again.
"""

import socket

from wn.errors import WnError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_TIMEOUT_SECONDS = 5
MAX_ATTEMPTS = 3
CHECKSUM_MODULO = 256
CHECKSUM_LENGTH = 2
RECEIVE_SIZE = 4096
ACK = b"+"
NACK = b"-"
PACKET_START = b"$"
PACKET_END = b"#"
OK_REPLY = "OK"
ERROR_PREFIX = "E"
MULTIPROCESS_FEATURE = "multiprocess+"
SUPPORTED_QUERY = "qSupported:" + MULTIPROCESS_FEATURE
CURRENT_THREAD_QUERY = "qC"
MULTIPROCESS_THREAD_PREFIX = "QCp"
PHYSICAL_MEMORY_ON = "Qqemu.PhyMemMode:1"


class GdbStubError(WnError):
	"""Raised when the GDB stub cannot be reached or rejects a request."""


def checksum(data):
	return "{:02x}".format(sum(data.encode("ascii")) % CHECKSUM_MODULO)


def frame(data):
	return "${}#{}".format(data, checksum(data)).encode("ascii")


class GdbStubClient:
	"""One session with the stub: connect, switch to physical memory, then read and write."""

	def __init__(self, port, host=DEFAULT_HOST, timeout=DEFAULT_TIMEOUT_SECONDS):
		self.port = port
		self.host = host
		self.timeout = timeout
		self._socket = None
		self._buffer = b""
		self._detach_packet = "D"

	def __enter__(self):
		self.connect()
		try:
			self._start_session()
		except GdbStubError:
			self.close()
			raise
		return self

	def __exit__(self, *exc_info):
		self.close()

	def connect(self):
		try:
			self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
		except OSError as error:
			raise GdbStubError("cannot connect to the GDB stub on {}:{}: {}".format(self.host, self.port, error)) from None

	def _start_session(self):
		features = self.command(SUPPORTED_QUERY).split(";")
		if MULTIPROCESS_FEATURE in features:
			thread = self.command(CURRENT_THREAD_QUERY)
			if thread.startswith(MULTIPROCESS_THREAD_PREFIX):
				process_id = thread[len(MULTIPROCESS_THREAD_PREFIX):].split(".")[0]
				self._detach_packet = "D;{:x}".format(int(process_id, 16))
		self._expect_ok(PHYSICAL_MEMORY_ON)

	def close(self):
		"""Detach so the VM runs again, then close the connection."""
		if self._socket is None:
			return
		try:
			self._expect_ok(self._detach_packet)
		except GdbStubError:
			pass
		finally:
			self._socket.close()
			self._socket = None

	def read_memory(self, address, length):
		return bytes.fromhex(self._checked("m{:x},{:x}".format(address, length)))

	def write_memory(self, address, data):
		self._expect_ok("M{:x},{:x}:{}".format(address, len(data), data.hex()))

	def command(self, data):
		"""Send one packet and return the reply text."""
		if self._socket is None:
			raise GdbStubError("not connected to the GDB stub")
		try:
			self._send(data)
			return self._receive()
		except socket.timeout:
			raise GdbStubError("the GDB stub did not answer '{}' in {} s".format(data, self.timeout)) from None
		except OSError as error:
			raise GdbStubError("GDB stub connection failed: {}".format(error)) from None

	def _checked(self, data):
		reply = self.command(data)
		if reply.startswith(ERROR_PREFIX) or not reply:
			raise GdbStubError("the GDB stub refused '{}': {}".format(data, reply or "not supported"))
		return reply

	def _expect_ok(self, data):
		reply = self._checked(data)
		if reply != OK_REPLY:
			raise GdbStubError("the GDB stub answered '{}' with '{}'".format(data, reply))

	def _send(self, data):
		packet = frame(data)
		for _ in range(MAX_ATTEMPTS):
			self._socket.sendall(packet)
			if self._read_byte() == ACK:
				return
		raise GdbStubError("the GDB stub rejected '{}' {} times".format(data, MAX_ATTEMPTS))

	def _receive(self):
		for _ in range(MAX_ATTEMPTS):
			while self._read_byte() != PACKET_START:
				pass
			body = self._read_until(PACKET_END)
			received_checksum = b"".join(self._read_byte() for _ in range(CHECKSUM_LENGTH)).decode("ascii")
			data = body.decode("ascii")
			if received_checksum == checksum(data):
				self._socket.sendall(ACK)
				return data
			self._socket.sendall(NACK)
		raise GdbStubError("the GDB stub sent {} replies with bad checksums".format(MAX_ATTEMPTS))

	def _read_until(self, terminator):
		body = b""
		while True:
			byte = self._read_byte()
			if byte == terminator:
				return body
			body += byte

	def _read_byte(self):
		if not self._buffer:
			self._buffer = self._socket.recv(RECEIVE_SIZE)
			if not self._buffer:
				raise GdbStubError("the GDB stub closed the connection")
		byte, self._buffer = self._buffer[:1], self._buffer[1:]
		return byte
