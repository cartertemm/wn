import unittest

from fakes import FakeBackend
from wn.backend import Backend, BackendError, VMStatus, create_backend
from wn.config import Config


class BackendTests(unittest.TestCase):
	def test_backend_is_abstract(self):
		with self.assertRaises(TypeError):
			Backend()

	def test_share_and_net_are_not_implemented_by_default(self):
		backend = FakeBackend()
		with self.assertRaises(NotImplementedError) as context:
			backend.share("/tmp")
		self.assertIn("share", str(context.exception))
		with self.assertRaises(NotImplementedError) as context:
			backend.net()
		self.assertIn("net", str(context.exception))

	def test_vm_status_fields(self):
		status = VMStatus(name="Windows", state="stopped")
		self.assertEqual((status.name, status.state), ("Windows", "stopped"))

	def test_unknown_backend_name(self):
		with self.assertRaises(BackendError) as context:
			create_backend(Config(backend="nope"))
		self.assertIn("nope", str(context.exception))

	def test_utm_backend_is_registered(self):
		from wn.backends.utm import UTMBackend
		backend = create_backend(Config(backend="utm", vm_name="Win11"))
		self.assertIsInstance(backend, UTMBackend)
		self.assertEqual(backend.vm_name, "Win11")

	def test_utm_is_the_default_backend(self):
		self.assertEqual(type(create_backend(Config())).__name__, "UTMBackend")

	def test_registered_backend_is_created_with_config(self):
		created = []
		registry = {"fake": lambda config: created.append(config) or FakeBackend()}
		config = Config(backend="fake")
		self.assertIsInstance(create_backend(config, registry), FakeBackend)
		self.assertEqual(created, [config])


if __name__ == "__main__":
	unittest.main()
