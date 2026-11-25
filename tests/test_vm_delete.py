import io
import json
import os
import unittest
from unittest import mock

from fakes import FakeBackend, share_item, short_temporary_directory, write_config
from wn.agent import LOG_PATH, launch_agent_path
from wn.config import load_config
from wn.share_sftp import share_paths
from wn.shares import FIRST_PORT, SUPPORT_DIRECTORY
from wn.utm_config import camera_socket_file, default_battery_file
from wn.vm_delete import DeleteItem, plan_delete, run_delete

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

	def labels(self, name, in_utm=True, include_all=False):
		return [item.label for item in plan_delete(name, load_config(), self.backend, in_utm, include_all)]


class PlanDeleteTests(DeleteTestCase):
	def test_nothing_exists(self):
		self.assertEqual(self.labels("Windows", in_utm=False), [])

	def test_lists_every_item_in_removal_order(self):
		write_config(self.home.name, {"vm_name": "Windows", "shares": [share_item()]})
		touch(launch_agent_path())
		touch(default_battery_file("Windows"))
		touch(camera_socket_file("Windows"))
		self.assertEqual(self.labels("Windows"), [
			"the wn agent launch agent, {}, and its log {}".format(launch_agent_path(), os.path.expanduser(LOG_PATH)),
			"the VM Windows in UTM, with its disk",
			"the battery file {}".format(default_battery_file("Windows")),
			"the camera socket {}".format(camera_socket_file("Windows")),
			"the host keys and the log of share Projects",
			"the settings for Windows in {}".format(os.path.join(self.home.name, ".config", "wn", "config.json")),
		])

	def test_another_vm_leaves_the_agent_and_other_shares_alone(self):
		write_config(self.home.name, {"vm_name": "Windows", "shares": [share_item(), OTHER_SHARE]})
		touch(launch_agent_path())
		self.assertEqual(self.labels("Other", in_utm=False), [
			"the host keys and the log of share Other",
			"the settings for Other in {}".format(os.path.join(self.home.name, ".config", "wn", "config.json")),
		])

	def test_all_adds_the_support_folder_before_the_settings(self):
		write_config(self.home.name, {"vm_name": "Windows", "shares": [share_item()]})
		os.makedirs(os.path.expanduser(SUPPORT_DIRECTORY))
		self.assertEqual(self.labels("Windows", in_utm=False, include_all=True), [
			"the host keys and the log of share Projects",
			"the folder {} with the download cache, the share files, and wn-agent.app".format(os.path.expanduser(SUPPORT_DIRECTORY)),
			"the settings for Windows in {}".format(os.path.join(self.home.name, ".config", "wn", "config.json")),
		])

	def test_all_without_the_support_folder_leaves_it_out(self):
		self.assertEqual(self.labels("Windows", in_utm=False, include_all=True), [])

	def test_all_for_another_vm_includes_the_launch_agent(self):
		write_config(self.home.name, {"vm_name": "Windows"})
		touch(launch_agent_path())
		self.assertIn("the wn agent launch agent, {}, and its log {}".format(launch_agent_path(), os.path.expanduser(LOG_PATH)), self.labels("Other", in_utm=False, include_all=True))

	def test_all_folder_item_removes_the_folder(self):
		touch(os.path.join(os.path.expanduser(SUPPORT_DIRECTORY), "downloads", "file.zip"))
		plan_delete("Windows", load_config(), self.backend, False, True)[-1].remove()
		self.assertFalse(os.path.exists(os.path.expanduser(SUPPORT_DIRECTORY)))

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

	def test_share_item_removes_the_host_keys_and_the_log(self):
		write_config(self.home.name, {"vm_name": "Windows", "shares": [share_item()]})
		paths = share_paths()
		touch(paths.host_key("Projects"))
		touch(paths.host_public_key("Projects"))
		touch(paths.log_file("Projects"))
		items = plan_delete("Windows", load_config(), self.backend, False)
		next(item for item in items if item.label == "the host keys and the log of share Projects").remove()
		self.assertFalse(os.path.exists(paths.host_key("Projects")))
		self.assertFalse(os.path.exists(paths.host_public_key("Projects")))
		self.assertFalse(os.path.exists(paths.log_file("Projects")))

	def test_launch_agent_item_removes_the_agent_log(self):
		write_config(self.home.name, {"vm_name": "Windows"})
		touch(launch_agent_path())
		touch(os.path.expanduser(LOG_PATH))
		with mock.patch("wn.vm_delete.uninstall_launch_agent") as uninstall:
			plan_delete("Windows", load_config(), self.backend, False)[0].remove()
		uninstall.assert_called_once_with()
		self.assertFalse(os.path.exists(os.path.expanduser(LOG_PATH)))


class RunDeleteTests(unittest.TestCase):
	def run_items(self, items):
		output = io.StringIO()
		errors = io.StringIO()
		exit_code = run_delete(items, output, errors)
		return exit_code, output.getvalue(), errors.getvalue()

	def test_removes_every_item_in_order(self):
		removed = []
		items = [DeleteItem("first", lambda: removed.append("first")), DeleteItem("second", lambda: removed.append("second"))]
		exit_code, output, errors = self.run_items(items)
		self.assertEqual(exit_code, 0)
		self.assertEqual(removed, ["first", "second"])
		self.assertEqual(output, "Removed first\nRemoved second\n")
		self.assertEqual(errors, "")

	def test_a_failure_does_not_stop_the_rest(self):
		removed = []

		def fail():
			raise OSError("Permission denied")
		items = [DeleteItem("first", fail), DeleteItem("second", lambda: removed.append("second"))]
		exit_code, output, errors = self.run_items(items)
		self.assertEqual(exit_code, 1)
		self.assertEqual(removed, ["second"])
		self.assertEqual(output, "Removed second\n")
		self.assertEqual(errors, "wn: error: cannot remove first: Permission denied\nwn: error: some items were not removed. Fix the errors above, then run wn delete again\n")


if __name__ == "__main__":
	unittest.main()
