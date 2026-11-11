"""Control a UTM 5 QEMU virtual machine with utmctl, and reach its extra QMP server over TCP."""

import ipaddress
import subprocess

from wn.backend import Backend, BackendError, VMStatus
from wn.qmp import TCP_ADDRESS_PREFIX
from wn.utm_config import session_port

DEFAULT_VM_NAME = "Windows"
DEFAULT_UTMCTL_PATH = "/Applications/UTM.app/Contents/MacOS/utmctl"
DEFAULT_QMP_PORT = 4444
QMP_HOST = "127.0.0.1"
LIST_HEADER_PREFIX = "UUID"
LIST_COLUMN_COUNT = 3
VM_NOT_FOUND_MESSAGE = "Error: Virtual machine not found."
UTM_STATES = {
	"stopped": "stopped",
	"starting": "starting",
	"started": "running",
	"pausing": "pausing",
	"paused": "paused",
	"resuming": "resuming",
	"stopping": "stopping",
}


class UTMCtlError(BackendError):
	"""Raised when utmctl fails or prints something wn cannot parse."""


class UTMCtlMissingError(UTMCtlError):
	"""Raised when the utmctl executable cannot be run."""


class UnknownVMError(UTMCtlError):
	"""Raised when UTM has no VM with the configured name."""


def _setting(config, key, default):
	if isinstance(config, dict):
		value = config.get(key)
	else:
		value = getattr(config, key, None)
	return default if value is None else value


def _as_command(path):
	return [path] if isinstance(path, str) else list(path)


def parse_list_output(output):
	"""Return ``(uuid, status, name)`` tuples from ``utmctl list`` output."""
	machines = []
	for line in output.splitlines():
		if not line.strip() or line.startswith(LIST_HEADER_PREFIX):
			continue
		fields = line.split(None, LIST_COLUMN_COUNT - 1)
		if len(fields) != LIST_COLUMN_COUNT:
			raise UTMCtlError("cannot parse utmctl list line: {!r}".format(line))
		machines.append(tuple(fields))
	return machines


class UTMBackend(Backend):
	"""Drives one UTM VM through utmctl. Config keys: ``vm_name``, ``utmctl_path``, ``qmp_port``, ``qmp_session_port``.

	``utmctl_path`` is a path, or a list of arguments that runs utmctl.
	"""

	def __init__(self, config):
		self.vm_name = _setting(config, "vm_name", DEFAULT_VM_NAME)
		self.utmctl_command = _as_command(_setting(config, "utmctl_path", DEFAULT_UTMCTL_PATH))
		self.qmp_port = _setting(config, "qmp_port", DEFAULT_QMP_PORT)
		self.qmp_session_port = session_port(self.qmp_port, _setting(config, "qmp_session_port", None))

	def start(self):
		self._run_for_vm("start", self.vm_name)

	def stop(self, force=False):
		self._run_for_vm("stop", self.vm_name, "--force" if force else "--request")

	def status(self):
		for uuid, utm_state, name in self._list():
			if self.vm_name in (name, uuid):
				return VMStatus(name=name, state=self._map_state(utm_state))
		raise self._unknown_vm_error()

	def ip(self):
		output = self._run_for_vm("ip-address", self.vm_name)
		for line in output.split():
			try:
				address = ipaddress.ip_address(line)
			except ValueError:
				continue
			if address.version == 4:
				return str(address)
		raise UTMCtlError("utmctl reported no IPv4 address for '{}'. Is the QEMU guest agent running?".format(self.vm_name))

	def qmp_socket_path(self, session=False):
		port = self.qmp_session_port if session else self.qmp_port
		return "{}{}:{}".format(TCP_ADDRESS_PREFIX, QMP_HOST, port)

	@staticmethod
	def _map_state(utm_state):
		try:
			return UTM_STATES[utm_state]
		except KeyError:
			raise UTMCtlError("utmctl reported an unknown VM state '{}'".format(utm_state)) from None

	def _list(self):
		return parse_list_output(self._run("list"))

	def _unknown_vm_error(self, machines=None):
		machines = self._list() if machines is None else machines
		names = ", ".join(name for _, _, name in machines) or "none"
		return UnknownVMError("UTM has no VM named '{}'. Known VMs: {}".format(self.vm_name, names))

	def _run_for_vm(self, *arguments):
		"""Run a utmctl command about the VM. On failure, report an unknown VM name before the raw error."""
		try:
			return self._run(*arguments)
		except UTMCtlMissingError:
			raise
		except UTMCtlError as error:
			try:
				machines = self._list()
			except UTMCtlError:
				raise error from None
			if isinstance(error, UnknownVMError) or not any(self.vm_name in (name, uuid) for uuid, _, name in machines):
				raise self._unknown_vm_error(machines) from None
			raise

	def _run(self, *arguments):
		command = self.utmctl_command + list(arguments)
		try:
			result = subprocess.run(command, capture_output=True, text=True)
		except OSError as error:
			raise UTMCtlMissingError("cannot run utmctl at {}: {}. Install UTM or set utmctl_path".format(self.utmctl_command[0], error)) from None
		if result.returncode != 0:
			detail = result.stderr.strip() or result.stdout.strip() or "no output"
			if detail == VM_NOT_FOUND_MESSAGE:
				raise UnknownVMError("UTM has no VM named '{}'".format(self.vm_name))
			raise UTMCtlError("utmctl {} failed with exit code {}: {}".format(arguments[0], result.returncode, detail))
		return result.stdout
