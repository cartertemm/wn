"""Test doubles and helpers shared by the tests."""

import json
import os
import socket
import subprocess
import tempfile

from wn.appbundle import CLANG_PATH
from wn.backend import Backend, VMStatus
from wn.backends.utm import UTMCtlError


def write_config(home, values):
	directory = os.path.join(home, ".config", "wn")
	os.makedirs(directory, exist_ok=True)
	with open(os.path.join(directory, "config.json"), "w") as config_file:
		json.dump(values, config_file)


SHORT_TEMPORARY_ROOT = "/tmp"


def short_temporary_directory():
	"""Return a temporary directory short enough for a home whose QMP socket paths fit in sun_path."""
	return tempfile.TemporaryDirectory(dir=SHORT_TEMPORARY_ROOT)


def clang_works():
	"""Return True when clang runs, so the camera tests can build their C code."""
	try:
		return subprocess.run([CLANG_PATH, "--version"], capture_output=True).returncode == 0
	except OSError:
		return False


def unused_port():
	with socket.socket() as probe:
		probe.bind(("127.0.0.1", 0))
		return probe.getsockname()[1]


class FakeBackend(Backend):
	def __init__(self, socket_path="/fake/qmp.sock", session_socket_path="/fake/qmp-session.sock", ip_address="192.168.64.5", state="running"):
		self.calls = []
		self.socket_path = socket_path
		self.session_socket_path = session_socket_path
		self.ip_address = ip_address
		self.state = state
		self.files = {}
		self.on_exec = None

	def start(self):
		self.calls.append(("start",))

	def stop(self, force=False):
		self.calls.append(("stop", force))
		self.state = "stopped"

	def status(self):
		self.calls.append(("status",))
		return VMStatus(name="Windows", state=self.state)

	def ip(self):
		self.calls.append(("ip",))
		return self.ip_address

	def qmp_socket_path(self, session=False):
		self.calls.append(("qmp_socket_path", session))
		return self.session_socket_path if session else self.socket_path

	def pull_file(self, guest_path):
		self.calls.append(("pull_file", guest_path))
		if guest_path not in self.files:
			raise UTMCtlError("utmctl file failed with exit code 1: Error: cannot open file")
		return self.files[guest_path]

	def push_file(self, guest_path, data):
		self.calls.append(("push_file", guest_path))
		self.files[guest_path] = data.decode("utf-8")

	def exec_command(self, *argv):
		self.calls.append(("exec_command",) + argv)
		if self.on_exec is not None:
			self.on_exec(argv)


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
