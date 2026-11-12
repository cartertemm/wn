"""The interface every VM backend implements, and the registry that picks one."""

import abc
import dataclasses

from wn.errors import WnError


class BackendError(WnError):
	"""Raised when a backend cannot be found or cannot carry out a request."""


@dataclasses.dataclass(frozen=True)
class VMStatus:
	name: str
	state: str


class Backend(abc.ABC):
	"""Controls one VM. Subclasses wrap a specific VMM, such as UTM or plain QEMU."""

	@abc.abstractmethod
	def start(self):
		"""Start the VM. Return once the VMM has accepted the request."""

	@abc.abstractmethod
	def stop(self, force=False):
		"""Ask the guest to shut down, or power it off at once when ``force`` is True."""

	@abc.abstractmethod
	def status(self):
		"""Return a ``VMStatus``."""

	@abc.abstractmethod
	def ip(self):
		"""Return the guest IP address as a string, or None when it is not known."""

	@abc.abstractmethod
	def qmp_socket_path(self, session=False):
		"""Return the VM's QMP address. With ``session``, return the one for long-lived clients such as grab."""

	def share(self, host_path):
		raise NotImplementedError("share is not implemented by the {} backend yet".format(type(self).__name__))

	def net(self):
		raise NotImplementedError("net is not implemented by the {} backend yet".format(type(self).__name__))


def _create_utm_backend(config):
	from wn.backends.utm import UTMBackend
	return UTMBackend(config)


BACKENDS = {
	"utm": _create_utm_backend,
}


def create_backend(config, registry=None):
	"""Return the backend named in ``config``. ``registry`` maps names to factories that take the config."""
	registry = BACKENDS if registry is None else registry
	try:
		factory = registry[config.backend]
	except KeyError:
		available = ", ".join(sorted(registry)) or "none yet"
		raise BackendError("backend '{}' is not available. Available backends: {}".format(config.backend, available)) from None
	return factory(config)
