"""A minimal QEMU Machine Protocol (QMP) client over a Unix socket."""

import json
import socket

from wn.errors import WnError

DEFAULT_TIMEOUT_SECONDS = 5.0


class QMPConnectionError(WnError):
	"""Raised when the QMP socket cannot be reached or closes early."""


class QMPError(WnError):
	"""Raised when QEMU answers a command with an error."""

	def __init__(self, error_class, description):
		super().__init__("QMP error {}: {}".format(error_class, description))
		self.error_class = error_class
		self.description = description


class QMPClient:
	"""Connects to QEMU, negotiates capabilities, and runs commands.

	Asynchronous events that arrive while waiting for a reply are stored in ``events``.
	"""

	def __init__(self, socket_path, timeout=DEFAULT_TIMEOUT_SECONDS):
		self.socket_path = socket_path
		self.timeout = timeout
		self.greeting = None
		self.events = []
		self._socket = None
		self._stream = None
		self._next_id = 0

	def __enter__(self):
		self.connect()
		return self

	def __exit__(self, *exc_info):
		self.close()

	def connect(self):
		self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self._socket.settimeout(self.timeout)
		try:
			self._socket.connect(self.socket_path)
		except OSError as error:
			self.close()
			raise QMPConnectionError("cannot connect to QMP socket {}: {}".format(self.socket_path, error)) from None
		self._stream = self._socket.makefile("rwb")
		self.greeting = self._read_message()
		if "QMP" not in self.greeting:
			raise QMPConnectionError("{} did not send a QMP greeting".format(self.socket_path))
		self.execute("qmp_capabilities")

	def close(self):
		if self._stream is not None:
			self._stream.close()
			self._stream = None
		if self._socket is not None:
			self._socket.close()
			self._socket = None

	def execute(self, command, arguments=None):
		"""Run ``command`` and return its ``return`` value, or raise ``QMPError``."""
		self._next_id += 1
		request_id = self._next_id
		request = {"execute": command, "id": request_id}
		if arguments is not None:
			request["arguments"] = arguments
		self._write_message(request)
		return self._read_reply(request_id)

	def _read_reply(self, request_id):
		while True:
			message = self._read_message()
			if "event" in message:
				self.events.append(message)
			elif message.get("id") != request_id:
				continue
			elif "error" in message:
				error = message["error"]
				raise QMPError(error.get("class", "UnknownError"), error.get("desc", ""))
			else:
				return message.get("return")

	def _write_message(self, message):
		try:
			self._stream.write(json.dumps(message).encode() + b"\n")
			self._stream.flush()
		except OSError as error:
			raise QMPConnectionError("cannot write to QMP socket {}: {}".format(self.socket_path, error)) from None

	def _read_message(self):
		try:
			line = self._stream.readline()
		except OSError as error:
			raise QMPConnectionError("cannot read from QMP socket {}: {}".format(self.socket_path, error)) from None
		if not line:
			raise QMPConnectionError("QMP socket {} closed the connection".format(self.socket_path))
		return json.loads(line)
