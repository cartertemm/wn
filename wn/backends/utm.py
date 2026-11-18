"""Control a UTM 5 QEMU virtual machine with utmctl, and reach its extra QMP servers over Unix sockets."""

import ipaddress
import subprocess

from wn.backend import Backend, BackendError, VMStatus
from wn.utm_config import qmp_socket_file

DEFAULT_VM_NAME = "Windows"
DEFAULT_UTMCTL_PATH = "/Applications/UTM.app/Contents/MacOS/utmctl"
LIST_HEADER_PREFIX = "UUID"
LIST_COLUMN_COUNT = 3
VM_NOT_FOUND_MESSAGE = "Error: Virtual machine not found."
UTMCTL_TIMEOUT_SECONDS = 30
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


class UTMCtlTimeoutError(UTMCtlError):
	"""Raised when utmctl does not finish in time, which happens when UTM waits on a dialog."""


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
	"""Drives one UTM VM through utmctl. Config keys: ``vm_name`` and ``utmctl_path``.

	``utmctl_path`` is a path, or a list of arguments that runs utmctl.
	"""

	def __init__(self, config):
		self.vm_name = _setting(config, "vm_name", DEFAULT_VM_NAME)
		self.utmctl_command = _as_command(_setting(config, "utmctl_path", DEFAULT_UTMCTL_PATH))

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

	def machines(self):
		"""Return ``(uuid, utm_state, name)`` for every VM UTM knows."""
		return self._list()

	def exists(self):
		return any(self.vm_name in (name, uuid) for uuid, _, name in self._list())

	def delete(self):
		self._run_for_vm("delete", self.vm_name)

	def pull_file(self, guest_path):
		"""Return the text of a file in the guest. Needs the guest agent."""
		return self._run_for_vm("file", "pull", self.vm_name, guest_path)

	def push_file(self, guest_path, data):
		"""Write ``data`` (bytes) to a file in the guest. Needs the guest agent."""
		self._run_for_vm("file", "push", self.vm_name, guest_path, input_bytes=data)

	def exec_command(self, *argv):
		"""Start a program in the guest as SYSTEM. utmctl returns before it finishes and does not report its exit code."""
		self._run_for_vm("exec", self.vm_name, "--cmd", *argv)

	def qmp_socket_path(self, session=False):
		return qmp_socket_file(self.vm_name, session)

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

	def _run_for_vm(self, *arguments, input_bytes=None):
		"""Run a utmctl command about the VM. On failure, report an unknown VM name before the raw error."""
		try:
			return self._run(*arguments, input_bytes=input_bytes)
		except (UTMCtlMissingError, UTMCtlTimeoutError):
			raise
		except UTMCtlError as error:
			try:
				machines = self._list()
			except UTMCtlError:
				raise error from None
			if isinstance(error, UnknownVMError) or not any(self.vm_name in (name, uuid) for uuid, _, name in machines):
				raise self._unknown_vm_error(machines) from None
			raise

	def _run(self, *arguments, input_bytes=None):
		command = self.utmctl_command + list(arguments)
		try:
			if input_bytes is None:
				result = subprocess.run(command, capture_output=True, text=True, timeout=UTMCTL_TIMEOUT_SECONDS)
			else:
				result = subprocess.run(command, input=input_bytes, capture_output=True, timeout=UTMCTL_TIMEOUT_SECONDS)
		except subprocess.TimeoutExpired:
			raise UTMCtlTimeoutError("utmctl {} did not finish within {} s. UTM may be showing a dialog or waiting for an Automation permission prompt. Check UTM and System Settings > Privacy & Security > Automation, then try again".format(arguments[0], UTMCTL_TIMEOUT_SECONDS)) from None
		except OSError as error:
			raise UTMCtlMissingError("cannot run utmctl at {}: {}. Install UTM or set utmctl_path".format(self.utmctl_command[0], error)) from None
		if input_bytes is not None:
			result.stdout = result.stdout.decode("utf-8", "replace")
			result.stderr = result.stderr.decode("utf-8", "replace")
		if result.returncode != 0:
			detail = result.stderr.strip() or result.stdout.strip() or "no output"
			if detail == VM_NOT_FOUND_MESSAGE:
				raise UnknownVMError("UTM has no VM named '{}'".format(self.vm_name))
			raise UTMCtlError("utmctl {} failed with exit code {}: {}".format(arguments[0], result.returncode, detail))
		return result.stdout
