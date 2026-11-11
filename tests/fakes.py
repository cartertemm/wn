"""Test doubles shared by the CLI tests."""

from wn.backend import Backend, VMStatus


class FakeBackend(Backend):
	def __init__(self, socket_path="/fake/qmp.sock", ip_address="192.168.64.5", state="running"):
		self.calls = []
		self.socket_path = socket_path
		self.ip_address = ip_address
		self.state = state

	def start(self):
		self.calls.append(("start",))

	def stop(self, force=False):
		self.calls.append(("stop", force))

	def status(self):
		self.calls.append(("status",))
		return VMStatus(name="Windows", state=self.state)

	def ip(self):
		self.calls.append(("ip",))
		return self.ip_address

	def qmp_socket_path(self):
		self.calls.append(("qmp_socket_path",))
		return self.socket_path


class RecordingQMPClient:
	"""Stands in for QMPClient. Every instance appends its commands to the shared ``log``."""

	def __init__(self, log):
		self.log = log

	def __call__(self, socket_path):
		self.log.append(("connect", socket_path))
		return self

	def __enter__(self):
		return self

	def __exit__(self, *exc_info):
		self.log.append(("close",))

	def execute(self, command, arguments=None):
		self.log.append((command, arguments))
		return {}
