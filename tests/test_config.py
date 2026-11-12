import json
import os
import tempfile
import unittest
from unittest import mock

from wn.config import Config, ConfigError, config_path, load_config


class ConfigTests(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(self.home.cleanup)

	def write_config(self, content):
		path = os.path.join(self.home.name, ".config", "wn", "config.json")
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(path, "w") as config_file:
			config_file.write(content)

	def test_path_is_under_home(self):
		self.assertEqual(config_path(), os.path.join(self.home.name, ".config", "wn", "config.json"))

	def test_missing_file_gives_defaults(self):
		self.assertEqual(load_config(), Config())

	def test_values_are_loaded(self):
		self.write_config(json.dumps({
			"vm_name": "Win11",
			"backend": "qemu",
			"qmp_socket": "~/vm/qmp.sock",
			"key_delay_ms": 35,
		}))
		config = load_config()
		self.assertEqual(config.vm_name, "Win11")
		self.assertEqual(config.backend, "qemu")
		self.assertEqual(config.qmp_socket, os.path.join(self.home.name, "vm", "qmp.sock"))
		self.assertEqual(config.key_delay_ms, 35)

	def test_partial_file_keeps_other_defaults(self):
		self.write_config(json.dumps({"vm_name": "Other"}))
		self.assertEqual(load_config().key_delay_ms, Config().key_delay_ms)

	def test_invalid_json(self):
		self.write_config("{not json")
		with self.assertRaises(ConfigError) as context:
			load_config()
		self.assertIn("config.json", str(context.exception))

	def test_unknown_key(self):
		self.write_config(json.dumps({"vm_nmae": "typo"}))
		with self.assertRaises(ConfigError) as context:
			load_config()
		self.assertIn("vm_nmae", str(context.exception))

	def test_wrong_type(self):
		self.write_config(json.dumps({"key_delay_ms": "fast"}))
		with self.assertRaises(ConfigError) as context:
			load_config()
		self.assertIn("key_delay_ms", str(context.exception))

	def test_bool_is_not_an_int(self):
		self.write_config(json.dumps({"key_delay_ms": True}))
		with self.assertRaises(ConfigError):
			load_config()

	def test_keygrab_defaults(self):
		config = load_config()
		self.assertIsNone(config.keygrab_binary)
		self.assertIsNone(config.keygrab_keycodes)
		self.assertTrue(config.keygrab_send_repeats)

	def test_keygrab_settings_are_loaded(self):
		self.write_config(json.dumps({
			"keygrab_binary": "~/bin/wn-keygrab",
			"keygrab_keycodes": "~/keycodes.json",
			"keygrab_send_repeats": False,
		}))
		config = load_config()
		self.assertEqual(config.keygrab_binary, os.path.join(self.home.name, "bin", "wn-keygrab"))
		self.assertEqual(config.keygrab_keycodes, os.path.join(self.home.name, "keycodes.json"))
		self.assertFalse(config.keygrab_send_repeats)

	def test_send_repeats_must_be_bool(self):
		self.write_config(json.dumps({"keygrab_send_repeats": 1}))
		with self.assertRaises(ConfigError) as context:
			load_config()
		self.assertIn("keygrab_send_repeats", str(context.exception))

	def test_utm_defaults(self):
		config = load_config()
		self.assertEqual(config.vm_name, "Windows")
		self.assertEqual(config.utmctl_path, "/Applications/UTM.app/Contents/MacOS/utmctl")
		self.assertEqual(config.qmp_port, 4444)

	def test_utm_settings_are_loaded(self):
		self.write_config(json.dumps({"utmctl_path": "~/bin/utmctl", "qmp_port": 4500}))
		config = load_config()
		self.assertEqual(config.utmctl_path, os.path.join(self.home.name, "bin", "utmctl"))
		self.assertEqual(config.qmp_port, 4500)

	def test_session_port_and_screen_size_default_to_none(self):
		config = load_config()
		self.assertIsNone(config.qmp_session_port)
		self.assertIsNone(config.screen_size)

	def test_session_port_and_screen_size_are_loaded(self):
		self.write_config(json.dumps({"qmp_session_port": 4600, "screen_size": "1920x1080"}))
		config = load_config()
		self.assertEqual(config.qmp_session_port, 4600)
		self.assertEqual(config.screen_size, "1920x1080")

	def test_qmp_port_must_be_int(self):
		for value in ("4444", True, 4444.0):
			with self.subTest(value=value):
				self.write_config(json.dumps({"qmp_port": value}))
				with self.assertRaises(ConfigError) as context:
					load_config()
				self.assertIn("qmp_port", str(context.exception))

	def test_utmctl_path_must_be_string(self):
		self.write_config(json.dumps({"utmctl_path": None}))
		with self.assertRaises(ConfigError) as context:
			load_config()
		self.assertIn("utmctl_path", str(context.exception))

	def test_battery_sync_defaults(self):
		self.assertTrue(load_config().battery_sync)

	def test_battery_sync_settings_are_loaded(self):
		self.write_config(json.dumps({"battery_sync": False}))
		self.assertFalse(load_config().battery_sync)

	def test_battery_sync_type(self):
		self.write_config(json.dumps({"battery_sync": 1}))
		with self.assertRaises(ConfigError) as context:
			load_config()
		self.assertIn("battery_sync", str(context.exception))

	def test_battery_file_default(self):
		self.assertIsNone(load_config().battery_file)

	def test_battery_file_is_loaded(self):
		self.write_config(json.dumps({"battery_file": "~/wn-battery.bin"}))
		self.assertEqual(load_config().battery_file, os.path.expanduser("~/wn-battery.bin"))

	def test_top_level_must_be_object(self):
		self.write_config("[]")
		with self.assertRaises(ConfigError):
			load_config()


if __name__ == "__main__":
	unittest.main()
