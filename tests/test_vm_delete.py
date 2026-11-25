import json
import os
import unittest
from unittest import mock

from fakes import FakeBackend, share_item, short_temporary_directory, write_config
from wn.agent import launch_agent_path
from wn.config import load_config
from wn.share_sftp import share_paths
from wn.shares import FIRST_PORT
from wn.utm_config import camera_socket_file, default_battery_file
from wn.vm_delete import plan_delete

OTHER_SHARE = share_item(name="Other", vm="Other", mac_path="/Users/c/Other", vm_path="C:\\Users\\c\\Other", port=FIRST_PORT + 1)


def touch(path):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w"):
		pass


class DeleteTestCase(unittest.TestCase):
	def setUp(self):
		self.home = short_temporary_directory()
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(self.home.cleanup)
		self.backend = FakeBackend(state="stopped")

	def config_values(self):
		with open(os.path.join(self.home.name, ".config", "wn", "config.json")) as config_file:
			return json.load(config_file)

	def labels(self, name, in_utm=True):
		return [item.label for item in plan_delete(name, load_config(), self.backend, in_utm)]


class PlanDeleteTests(DeleteTestCase):
	def test_nothing_exists(self):
		self.assertEqual(self.labels("Windows", in_utm=False), [])

	def test_lists_every_item_in_removal_order(self):
		write_config(self.home.name, {"vm_name": "Windows", "shares": [share_item()]})
		touch(launch_agent_path())
		touch(default_battery_file("Windows"))
		touch(camera_socket_file("Windows"))
		self.assertEqual(self.labels("Windows"), [
			"the wn agent launch agent, {}".format(launch_agent_path()),
			"the VM Windows in UTM, with its disk",
			"the battery file {}".format(default_battery_file("Windows")),
			"the camera socket {}".format(camera_socket_file("Windows")),
			"the host keys of share Projects",
			"the settings for Windows in {}".format(os.path.join(self.home.name, ".config", "wn", "config.json")),
		])

	def test_another_vm_leaves_the_agent_and_other_shares_alone(self):
		write_config(self.home.name, {"vm_name": "Windows", "shares": [share_item(), OTHER_SHARE]})
		touch(launch_agent_path())
		self.assertEqual(self.labels("Other", in_utm=False), [
			"the host keys of share Other",
			"the settings for Other in {}".format(os.path.join(self.home.name, ".config", "wn", "config.json")),
		])

	def test_active_vm_uses_the_battery_file_from_the_config(self):
		battery = os.path.join(self.home.name, "custom-battery.bin")
		write_config(self.home.name, {"vm_name": "Windows", "battery_file": battery})
		touch(battery)
		self.assertIn("the battery file {}".format(battery), self.labels("Windows", in_utm=False))


class ConfigUpdateTests(DeleteTestCase):
	def remove_config_item(self, name):
		plan_delete(name, load_config(), self.backend, False)[-1].remove()

	def test_active_vm_removes_vm_name_and_its_shares_and_keeps_other_settings(self):
		write_config(self.home.name, {"vm_name": "Windows", "camera": True, "shares": [share_item(), OTHER_SHARE]})
		self.remove_config_item("Windows")
		self.assertEqual(self.config_values(), {"camera": True, "shares": [OTHER_SHARE]})

	def test_another_vm_keeps_vm_name(self):
		write_config(self.home.name, {"vm_name": "Windows", "shares": [share_item(), OTHER_SHARE]})
		self.remove_config_item("Other")
		self.assertEqual(self.config_values(), {"vm_name": "Windows", "shares": [share_item()]})

	def test_last_share_removes_the_shares_setting(self):
		write_config(self.home.name, {"vm_name": "Windows", "shares": [OTHER_SHARE]})
		self.remove_config_item("Other")
		self.assertEqual(self.config_values(), {"vm_name": "Windows"})

	def test_share_item_removes_the_host_keys(self):
		write_config(self.home.name, {"vm_name": "Windows", "shares": [share_item()]})
		paths = share_paths()
		touch(paths.host_key("Projects"))
		touch(paths.host_public_key("Projects"))
		items = plan_delete("Windows", load_config(), self.backend, False)
		next(item for item in items if item.label == "the host keys of share Projects").remove()
		self.assertFalse(os.path.exists(paths.host_key("Projects")))
		self.assertFalse(os.path.exists(paths.host_public_key("Projects")))


if __name__ == "__main__":
	unittest.main()
