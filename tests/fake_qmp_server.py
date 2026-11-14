"""A small QMP server on a Unix socket for tests. It speaks the real greeting and capabilities handshake."""

import json
import os
import shutil
import socket
import tempfile
import threading

GREETING = {
	"QMP": {
		"version": {"qemu": {"micro": 0, "minor": 1, "major": 11}, "package": ""},
		"capabilities": ["oob"],
	}
}
ACCEPT_TIMEOUT_SECONDS = 5


def event(name, data=None):
	return {"event": name, "data": data or {}, "timestamp": {"seconds": 1, "microseconds": 0}}


class FakeQMPServer:
	"""Serves one client. ``handlers`` maps a command name to a function that returns a list of messages to send back.

	A handler gets the request dict. Use ``reply(request, value)`` and ``error_reply(request, cls, desc)``
	to build responses, and add ``event(...)`` messages around them to test interleaving.
	"""

	def __init__(self, handlers=None, events_after_greeting=(), socket_path=None):
		self.handlers = handlers or {}
		self.events_after_greeting = list(events_after_greeting)
		self.received = []
		self._directory = tempfile.mkdtemp(prefix="wnqmp")
		self.socket_path = socket_path or os.path.join(self._directory, "qmp.sock")
		self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self._listener.bind(self.socket_path)
		self._listener.listen(1)
		self._listener.settimeout(ACCEPT_TIMEOUT_SECONDS)
		self._thread = threading.Thread(target=self._serve, daemon=True)

	def __enter__(self):
		self._thread.start()
		return self

	def __exit__(self, *exc_info):
		self._listener.close()
		self._thread.join(ACCEPT_TIMEOUT_SECONDS)
		if os.path.lexists(self.socket_path):
			os.remove(self.socket_path)
		shutil.rmtree(self._directory, ignore_errors=True)

	def _serve(self):
		try:
			connection, _ = self._listener.accept()
		except OSError:
			return
		with connection, connection.makefile("rwb") as stream:
			self._send(stream, [GREETING] + self.events_after_greeting)
			negotiated = False
			for line in stream:
				request = json.loads(line)
				self.received.append(request)
				command = request.get("execute")
				if command == "qmp_capabilities":
					negotiated = True
					self._send(stream, [reply(request, {})])
				elif not negotiated:
					self._send(stream, [error_reply(request, "CommandNotFound", "Expecting capabilities negotiation with 'qmp_capabilities'")])
				elif command in self.handlers:
					self._send(stream, self.handlers[command](request))
				else:
					self._send(stream, [error_reply(request, "CommandNotFound", "The command {} has not been found".format(command))])

	@staticmethod
	def _send(stream, messages):
		for message in messages:
			stream.write(json.dumps(message).encode() + b"\r\n")
		stream.flush()


def _with_id(request, message):
	if "id" in request:
		message["id"] = request["id"]
	return message


def reply(request, value):
	return _with_id(request, {"return": value})


def error_reply(request, error_class, description):
	return _with_id(request, {"error": {"class": error_class, "desc": description}})


class TCPFakeQMPServer(FakeQMPServer):
	"""The fake QMP server, listening on 127.0.0.1 instead of a Unix socket."""

	def __init__(self, handlers=None, events_after_greeting=()):
		super().__init__(handlers, events_after_greeting)
		self._listener.close()
		self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self._listener.bind(("127.0.0.1", 0))
		self._listener.listen(1)
		self._listener.settimeout(ACCEPT_TIMEOUT_SECONDS)
		self.port = self._listener.getsockname()[1]
		self.address = "tcp:127.0.0.1:{}".format(self.port)
