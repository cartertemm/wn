"""A small GDB remote protocol server on TCP for tests. It acts like QEMU's gdbstub in physical memory mode."""

import socket
import threading

ACCEPT_TIMEOUT_SECONDS = 5
ACCEPT_POLL_SECONDS = 0.05
SUPPORTED_REPLY = "PacketSize=4000;qXfer:features:read+;vContSupported+;multiprocess+"
CURRENT_THREAD_REPLY = "QCp01.01"
PROCESS_DETACH = "D;1"
ERROR_BAD_ADDRESS = "E14"
ERROR_BAD_ARGUMENT = "E22"


def checksum(data):
	return "{:02x}".format(sum(data.encode("ascii")) % 256)


def frame(data):
	return "${}#{}".format(data, checksum(data)).encode("ascii")


class FakeGdbServer:
	"""Serves clients one after another. ``regions`` maps a base address to a size of zeroed memory.

	``nack_first`` makes the server answer the first packets with ``-``. ``corrupt_first`` makes it send
	the first replies with a bad checksum. ``drop_after`` closes the connection after that many packets.
	"""

	def __init__(self, regions=None, nack_first=0, corrupt_first=0, drop_after=None):
		self.memory = {base: bytearray(size) for base, size in (regions or {}).items()}
		self.nack_first = nack_first
		self.corrupt_first = corrupt_first
		self.drop_after = drop_after
		self.packets = []
		self.connections = 0
		self.paused = False
		self.physical = False
		self._lock = threading.Lock()
		self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self._listener.bind(("127.0.0.1", 0))
		self._listener.listen(1)
		self._listener.settimeout(ACCEPT_POLL_SECONDS)
		self.port = self._listener.getsockname()[1]
		self._running = True
		self._thread = threading.Thread(target=self._serve, daemon=True)

	def __enter__(self):
		self._thread.start()
		return self

	def __exit__(self, *exc_info):
		self._running = False
		self._thread.join(ACCEPT_TIMEOUT_SECONDS)
		self._listener.close()

	def write(self, address, data):
		region, offset = self._find(address, len(data))
		region[offset:offset + len(data)] = data

	def read(self, address, length):
		region, offset = self._find(address, length)
		return bytes(region[offset:offset + length])

	def _find(self, address, length):
		for base, region in self.memory.items():
			if base <= address and address + length <= base + len(region):
				return region, address - base
		raise KeyError(hex(address))

	def _serve(self):
		while self._running:
			try:
				connection, _ = self._listener.accept()
			except socket.timeout:
				continue
			except OSError:
				return
			with connection:
				connection.settimeout(ACCEPT_TIMEOUT_SECONDS)
				self.connections += 1
				self.paused = True
				self.physical = False
				try:
					self._session(connection)
				except OSError:
					pass

	def _session(self, connection):
		reader = connection.makefile("rb")
		while True:
			byte = reader.read(1)
			if not byte:
				return
			if byte != b"$":
				continue
			body = b""
			while True:
				character = reader.read(1)
				if character == b"#":
					break
				body += character
			received_checksum = reader.read(2).decode("ascii")
			data = body.decode("ascii")
			if received_checksum != checksum(data) or self.nack_first > 0:
				self.nack_first = max(0, self.nack_first - 1)
				connection.sendall(b"-")
				continue
			connection.sendall(b"+")
			self.packets.append(data)
			if self.drop_after is not None and len(self.packets) >= self.drop_after:
				return
			self._reply(connection, reader, self._handle(data))

	def _reply(self, connection, reader, response):
		while True:
			packet = frame(response)
			if self.corrupt_first > 0:
				self.corrupt_first -= 1
				packet = packet[:-2] + b"00" if not packet.endswith(b"00") else packet[:-2] + b"01"
			connection.sendall(packet)
			if reader.read(1) == b"+":
				return

	def _handle(self, data):
		if data.startswith("qSupported"):
			return SUPPORTED_REPLY
		if data == "qC":
			return CURRENT_THREAD_REPLY
		if data == "Qqemu.PhyMemMode:1":
			self.physical = True
			return "OK"
		if data.startswith("m"):
			return self._read_packet(data[1:])
		if data.startswith("M"):
			return self._write_packet(data[1:])
		if data == PROCESS_DETACH:
			self.paused = False
			return "OK"
		if data == "D":
			return ERROR_BAD_ARGUMENT
		return ""

	def _read_packet(self, arguments):
		address, length = (int(part, 16) for part in arguments.split(","))
		try:
			return self.read(address, length).hex()
		except KeyError:
			return ERROR_BAD_ADDRESS

	def _write_packet(self, arguments):
		location, hex_bytes = arguments.split(":")
		address, length = (int(part, 16) for part in location.split(","))
		data = bytes.fromhex(hex_bytes)
		if len(data) != length:
			return ERROR_BAD_ARGUMENT
		try:
			self.write(address, data)
		except KeyError:
			return ERROR_BAD_ADDRESS
		return "OK"
