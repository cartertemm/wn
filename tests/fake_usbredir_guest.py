"""Plays QEMU's usb-redir device (the usb-guest side of usbredir) on a Unix socket, for wn-camera tests."""

import socket
import struct

HELLO = 0
DEVICE_CONNECT = 1
DEVICE_DISCONNECT = 2
RESET = 3
INTERFACE_INFO = 4
EP_INFO = 5
SET_CONFIGURATION = 6
CONFIGURATION_STATUS = 8
CANCEL_DATA_PACKET = 21
CONTROL_PACKET = 100
BULK_PACKET = 101
STATUS_SUCCESS = 0
STATUS_CANCELLED = 1
STATUS_STALL = 4
QEMU_CAPS = 0xFF
QEMU_VERSION = b"qemu usb-redir guest 10.0.12"
HELLO_VERSION_SIZE = 64
SHORT_HEADER = struct.Struct("<III")
HEADER = struct.Struct("<IIQ")
CAPS = struct.Struct("<I")
CONTROL = struct.Struct("<BBBBHHH")
BULK = struct.Struct("<BBHIH")
STREAMING_ENDPOINT = 0x81
LOW_16_BITS = 0xFFFF


class FakeUsbredirGuest:
	def __init__(self, path):
		self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self.listener.bind(path)
		self.listener.listen(1)
		self.connection = None
		self.long_ids = False

	def accept(self, timeout):
		if self.connection is not None:
			self.connection.close()
		self.listener.settimeout(timeout)
		self.connection, _ = self.listener.accept()
		self.connection.settimeout(timeout)
		self.long_ids = False

	def close(self):
		if self.connection is not None:
			self.connection.close()
			self.connection = None
		self.listener.close()

	def _read_exact(self, size):
		data = b""
		while len(data) < size:
			chunk = self.connection.recv(size - len(data))
			if not chunk:
				raise EOFError("wn-camera closed the socket")
			data += chunk
		return data

	def read_packet(self):
		"""Return (type, id, body) of the next packet from wn-camera."""
		layout = HEADER if self.long_ids else SHORT_HEADER
		packet_type, length, packet_id = layout.unpack(self._read_exact(layout.size))
		return packet_type, packet_id, self._read_exact(length)

	def send(self, packet_type, packet_id=0, body=b""):
		self.connection.sendall(HEADER.pack(packet_type, len(body), packet_id) + body)

	def send_hello(self, caps=QEMU_CAPS):
		body = QEMU_VERSION.ljust(HELLO_VERSION_SIZE, b"\0") + CAPS.pack(caps)
		self.connection.sendall(SHORT_HEADER.pack(HELLO, len(body), 0) + body)
		self.long_ids = True

	def control(self, requesttype, request, value, index, length, data=b"", packet_id=1):
		"""Send one control transfer and return (status, data) of the answer."""
		endpoint = 0x80 if requesttype & 0x80 else 0x00
		self.send(CONTROL_PACKET, packet_id, CONTROL.pack(endpoint, request, requesttype, 0, value, index, length) + data)
		packet_type, answer_id, body = self.read_packet()
		if (packet_type, answer_id) != (CONTROL_PACKET, packet_id):
			raise AssertionError("expected the control answer {}, got type {} id {}".format(packet_id, packet_type, answer_id))
		return body[3], body[CONTROL.size:]

	def bulk_request(self, packet_id, length, endpoint=STREAMING_ENDPOINT):
		self.send(BULK_PACKET, packet_id, BULK.pack(endpoint, 0, length & LOW_16_BITS, 0, length >> 16))

	def read_bulk(self):
		"""Return (id, status, data) of the next bulk answer."""
		packet_type, packet_id, body = self.read_packet()
		if packet_type != BULK_PACKET:
			raise AssertionError("expected a bulk answer, got type {}".format(packet_type))
		_, status, low, _, high = BULK.unpack(body[:BULK.size])
		data = body[BULK.size:]
		if len(data) != high << 16 | low:
			raise AssertionError("bulk length field {} does not match {} data bytes".format(high << 16 | low, len(data)))
		return packet_id, status, data
