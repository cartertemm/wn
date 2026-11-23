import os
import tempfile
import unittest
from unittest import mock

from fakes import share_item
from wn import shares
from wn.shares import (
	FIRST_PORT,
	LAST_PORT,
	MAC_TO_VM,
	Share,
	ShareError,
	check_mac_path,
	check_new_share,
	check_share_name,
	default_share_name,
	normalize_vm_path,
	pick_port,
	shares_for_vm,
	split_shares,
)

CONFIG_FILE = "/home/config.json"


def make_share(name="Projects", vm="Windows", mac_path="/Users/c/Projects", vm_path="C:\\Users\\c\\Projects", port=None):
	return Share(name, vm, MAC_TO_VM, mac_path, vm_path, False, port)


class DefaultNameTests(unittest.TestCase):
	def test_names_from_paths(self):
		for path, name in (
			("/Users/c/My Projects", "My-Projects"),
			("/x/.hidden", "hidden"),
			("/x/-a", "a"),
			("/x/Fotos ü 1", "Fotos-1"),
			("/Users/c/My Projects (2024)", "My-Projects-2024"),
			("/x/Fotos ü", "Fotos"),
			("/x/" + "a" * 31 + " b", "a" * 31),
			("/x/日本", "share"),
			("/", "share"),
			("/x/Docs/", "Docs"),
			("/x/" + "a" * 40, "a" * 32),
			("/x/a..", "a"),
			("/x/" + "a" * 31 + ".b", "a" * 31),
			("/x/CON", "share"),
			("/x/nul.txt", "share"),
			("/x/Com1", "share"),
			("/x/LPT9.", "share"),
			("/x/...", "share"),
		):
			with self.subTest(path=path):
				self.assertEqual(default_share_name(path), name)


class ShareNameTests(unittest.TestCase):
	def test_accepts_valid_names(self):
		for name in ("Projects", "a.b_c-d", "_x", "a" * 32):
			with self.subTest(name=name):
				check_share_name(name)

	def test_refuses_invalid_names(self):
		for name in ("", ".x", "-x", "a" * 33, "a b", "ü", "abc\n"):
			with self.subTest(name=name):
				with self.assertRaisesRegex(ShareError, "--name"):
					check_share_name(name)

	def test_refuses_reserved_device_names(self):
		for name in ("CON", "nul", "COM1", "LPT9", "aux", "CON.txt", "prn", "com0", "Lpt0.a.b"):
			with self.subTest(name=name):
				with self.assertRaisesRegex(ShareError, "'{}' is a reserved Windows device name.*--name".format(name)):
					check_share_name(name)

	def test_refuses_names_that_end_in_a_dot(self):
		for name in ("a.", "a.."):
			with self.subTest(name=name):
				with self.assertRaisesRegex(ShareError, "ends in a dot.*--name"):
					check_share_name(name)

	def test_accepts_names_that_only_start_like_device_names(self):
		for name in ("CONSOLE", "COM10", "nul_x", "a.CON", "LPT"):
			with self.subTest(name=name):
				check_share_name(name)


class NormalizeVmPathTests(unittest.TestCase):
	def test_slashes_become_backslashes(self):
		self.assertEqual(normalize_vm_path("C:/Users/c/Mac"), "C:\\Users\\c\\Mac")

	def test_trailing_backslash_is_removed(self):
		self.assertEqual(normalize_vm_path("C:\\Users\\c\\Mac\\"), "C:\\Users\\c\\Mac")

	def test_lower_case_drive_is_kept(self):
		self.assertEqual(normalize_vm_path("c:\\x"), "c:\\x")

	def test_refuses_paths_that_are_not_absolute_folders(self):
		for path in ("Users\\x", "\\\\server\\share", "C:", "C:\\", "C:/", "C:\\Users\\..\\Windows", "C:\\Users\\.\\c"):
			with self.subTest(path=path):
				with self.assertRaisesRegex(ShareError, "absolute Windows folder"):
					normalize_vm_path(path)

	def test_path_without_backslashes_after_the_drive_names_the_shell(self):
		for path, drive in (("C:Usersyouproj", "C"), ("d:Work", "d"), ("C:Users\\you", "C")):
			with self.subTest(path=path):
				with self.assertRaises(ShareError) as caught:
					normalize_vm_path(path)
				self.assertEqual(str(caught.exception), "'{path}' has no backslash after {drive}:. The Mac shell removes backslashes from a path that is not in quotes. Put the Windows path in single quotes, such as '{drive}:\\Users\\you\\Projects', or use forward slashes, such as {drive}:/Users/you/Projects".format(path=path, drive=drive))

	def assert_part_refused(self, path, reason):
		with self.assertRaises(ShareError) as caught:
			normalize_vm_path(path)
		self.assertIn(path, str(caught.exception))
		self.assertIn(reason, str(caught.exception))

	def test_refuses_empty_parts(self):
		for path in ("C://Windows", "C:\\a\\\\b", "C:\\\\Windows\\System32"):
			with self.subTest(path=path):
				self.assert_part_refused(path, "empty folder name")

	def test_refuses_parts_made_only_of_dots(self):
		with self.assertRaisesRegex(ShareError, "absolute Windows folder"):
			normalize_vm_path("C:\\a\\...\\b")

	def test_refuses_parts_that_end_in_a_dot_or_a_space(self):
		for path in ("C:\\Windows.\\System32", "C:\\Windows \\x", "C:\\a\\b."):
			with self.subTest(path=path):
				self.assert_part_refused(path, "ends in a dot or a space")

	def test_refuses_characters_that_windows_does_not_allow(self):
		for character in '<>:"|?*\n\r\x00\x01\x1f':
			with self.subTest(character=character):
				self.assert_part_refused("C:\\a" + character + "b", "Windows does not allow")

	def test_refuses_reserved_device_names(self):
		for part in ("CON", "nul", "COM1", "lpt0", "AUX.txt", "PRN.a.b", "nul .txt"):
			with self.subTest(part=part):
				self.assert_part_refused("C:\\Users\\" + part + "\\x", "reserved Windows device name")

	def test_refuses_short_names(self):
		for part in ("PROGRA~1", "WINDOW~2", "a~0b"):
			with self.subTest(part=part):
				self.assert_part_refused("C:\\" + part + "\\x", "8.3 short name")

	def test_accepts_and_keeps_names_with_punctuation(self):
		path = "C:\\Users\\O'Brien\\R&D $x `y` %z% (1) a~b"
		self.assertEqual(normalize_vm_path(path), path)


class VmPathTests(unittest.TestCase):
	def test_refuses_system_folders(self):
		for path in ("C:\\Windows", "c:\\windows\\Temp", "C:\\Program Files\\x", "C:\\Program Files (x86)\\x", "C:\\ProgramData\\wn"):
			with self.subTest(path=path):
				with self.assertRaises(ShareError):
					normalize_vm_path(path)

	def test_refuses_system_folders_written_in_other_forms(self):
		for path in ("C:/Windows/System32", "C:\\WINDOWS\\", "C:\\Windows.\\System32", "C://Windows", "C:\\PROGRA~1\\x"):
			with self.subTest(path=path):
				with self.assertRaises(ShareError):
					normalize_vm_path(path)

	def test_accepts_other_folders(self):
		for path in ("C:\\Programs", "C:\\Users\\c\\Mac", "D:\\Data", "C:\\Windowsx", "D:\\Windows"):
			with self.subTest(path=path):
				self.assertEqual(normalize_vm_path(path), path)


class MacPathTests(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(self.home.cleanup)
		self.home_path = os.path.realpath(self.home.name)
		self.support = os.path.join(self.home_path, "Library", "Application Support", "wn")

	def make_folder(self, *parts):
		path = os.path.join(self.home_path, *parts)
		os.makedirs(path)
		return path

	def test_returns_realpath_of_folder(self):
		folder = self.make_folder("Projects")
		self.assertEqual(check_mac_path(os.path.join(self.home.name, "Projects"), False), folder)

	def test_follows_symlink_to_folder(self):
		folder = self.make_folder("Projects")
		link = os.path.join(self.home_path, "link")
		os.symlink(folder, link)
		self.assertEqual(check_mac_path(link, False), folder)

	def test_expands_home(self):
		folder = self.make_folder("Projects")
		self.assertEqual(check_mac_path("~/Projects", False), folder)

	def test_refuses_missing_path(self):
		with self.assertRaisesRegex(ShareError, "is not a folder on the Mac"):
			check_mac_path(os.path.join(self.home_path, "missing"), False)

	def test_refuses_file(self):
		path = os.path.join(self.home_path, "file.txt")
		with open(path, "w") as handle:
			handle.write("x")
		with self.assertRaisesRegex(ShareError, "is not a folder on the Mac"):
			check_mac_path(path, False)

	def test_refuses_root(self):
		with self.assertRaises(ShareError):
			check_mac_path("/", True)

	def test_refuses_support_folder_also_read_only(self):
		os.makedirs(self.support)
		with self.assertRaises(ShareError):
			check_mac_path(self.support, True)

	def test_refuses_folder_inside_support_folder(self):
		folder = self.make_folder("Library", "Application Support", "wn", "shares")
		with self.assertRaises(ShareError):
			check_mac_path(folder, True)

	def test_refuses_home_read_write(self):
		with self.assertRaisesRegex(ShareError, "--read-only") as caught:
			check_mac_path(self.home_path, False)
		self.assertIn(self.support, str(caught.exception))

	def test_allows_home_read_only(self):
		self.assertEqual(check_mac_path(self.home_path, True), self.home_path)

	def test_refuses_config_parent_read_write(self):
		folder = self.make_folder(".config")
		with self.assertRaisesRegex(ShareError, "--read-only") as caught:
			check_mac_path(folder, False)
		self.assertIn(os.path.join(folder, "wn"), str(caught.exception))

	def test_allows_sibling_folder_read_write(self):
		folder = self.make_folder("Library", "Application Support", "wn-other")
		self.assertEqual(check_mac_path(folder, False), folder)

	def test_allows_sibling_of_existing_support_folder_read_write(self):
		os.makedirs(self.support)
		folder = self.make_folder("Library", "Application Support", "wn-other")
		self.assertEqual(check_mac_path(folder, False), folder)

	def test_refuses_symlink_to_support_folder(self):
		os.makedirs(self.support)
		link = os.path.join(self.home_path, "link")
		os.symlink(self.support, link)
		with self.assertRaisesRegex(ShareError, "inside wn's own folder"):
			check_mac_path(link, True)

	def test_allows_other_folder_when_config_folder_is_missing(self):
		os.makedirs(self.support)
		folder = self.make_folder("Projects")
		self.assertEqual(check_mac_path(folder, False), folder)

	@mock.patch.object(shares, "WN_DIRECTORIES", (shares.CONFIG_DIRECTORY,))
	def test_refuses_config_folder_parent_when_config_folder_is_missing(self):
		with self.assertRaisesRegex(ShareError, "--read-only") as caught:
			check_mac_path(self.home_path, False)
		self.assertIn(os.path.join(self.home_path, ".config", "wn"), str(caught.exception))

	def skip_if_case_sensitive(self):
		probe = os.path.join(self.home_path, "case-probe")
		open(probe, "w").close()
		self.addCleanup(os.remove, probe)
		if not os.path.exists(probe.upper()):
			self.skipTest("the temp file system is case-sensitive")

	def test_refuses_inside_support_folder_in_other_case(self):
		self.skip_if_case_sensitive()
		self.make_folder("Library", "Application Support", "wn", "shares")
		with self.assertRaisesRegex(ShareError, "inside wn's own folder"):
			check_mac_path(os.path.join(self.home_path, "library", "application support", "WN", "shares"), True)

	def test_refuses_home_in_other_case_read_write(self):
		self.skip_if_case_sensitive()
		os.makedirs(self.support)
		with self.assertRaisesRegex(ShareError, "--read-only"):
			check_mac_path(self.home_path.swapcase(), False)

	def test_refuses_config_parent_in_other_case_read_write(self):
		self.skip_if_case_sensitive()
		self.make_folder(".config")
		with self.assertRaisesRegex(ShareError, "--read-only"):
			check_mac_path(os.path.join(self.home_path, ".CONFIG"), False)


DATA_VOLUME = "/System/Volumes/Data"


@unittest.skipUnless(os.path.isdir(DATA_VOLUME), "no macOS data volume")
class MacPathDataVolumeTests(unittest.TestCase):
	def setUp(self):
		self.home = os.path.realpath(os.path.expanduser("~"))

	def test_refuses_support_folder_through_data_volume(self):
		support = os.path.join(self.home, "Library", "Application Support", "wn")
		if not os.path.isdir(support):
			self.skipTest("no wn support folder")
		with self.assertRaisesRegex(ShareError, "inside wn's own folder"):
			check_mac_path(DATA_VOLUME + support, True)

	def test_refuses_home_through_data_volume_read_write(self):
		with self.assertRaisesRegex(ShareError, "--read-only"):
			check_mac_path(DATA_VOLUME + self.home, False)

	def test_refuses_data_volume_read_write(self):
		with self.assertRaisesRegex(ShareError, "--read-only"):
			check_mac_path(DATA_VOLUME, False)


class NewShareTests(unittest.TestCase):
	def setUp(self):
		self.existing = (make_share(name="Projects", mac_path="/a/b", vm_path="C:\\a\\b"),)

	def test_refuses_same_name_in_other_case(self):
		share = make_share(name="projects", mac_path="/x", vm_path="C:\\x")
		with self.assertRaisesRegex(ShareError, "a share named 'Projects' already exists. Pick another name with --name"):
			check_new_share(share, self.existing)

	def test_refuses_overlapping_mac_path(self):
		for mac_path in ("/a/b", "/A/B", "/a", "/a/b/c"):
			with self.subTest(mac_path=mac_path):
				share = make_share(name="Other", mac_path=mac_path, vm_path="C:\\x")
				with self.assertRaisesRegex(ShareError, "Projects"):
					check_new_share(share, self.existing)

	def test_refuses_overlapping_mac_path_of_another_vm(self):
		share = Share("Other", "Linux", MAC_TO_VM, "/a/b/c", "C:\\x", False)
		with self.assertRaisesRegex(ShareError, "Projects"):
			check_new_share(share, self.existing)

	def test_accepts_mac_path_with_same_prefix(self):
		check_new_share(make_share(name="Other", mac_path="/a/bc", vm_path="C:\\x"), self.existing)

	def test_refuses_overlapping_vm_path_on_same_vm(self):
		for vm_path in ("C:\\a\\b", "c:\\A\\B", "C:\\a", "C:\\a\\b\\c"):
			with self.subTest(vm_path=vm_path):
				share = make_share(name="Other", mac_path="/x", vm_path=vm_path)
				with self.assertRaisesRegex(ShareError, "Projects"):
					check_new_share(share, self.existing)

	def test_accepts_vm_path_with_same_prefix(self):
		check_new_share(make_share(name="Other", mac_path="/x", vm_path="C:\\a\\bc"), self.existing)

	def test_accepts_same_vm_path_for_another_vm(self):
		check_new_share(Share("Other", "Linux", MAC_TO_VM, "/x", "C:\\a\\b", False), self.existing)


class NewShareOnDiskTests(unittest.TestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.root = os.path.realpath(directory.name)
		self.folder = os.path.join(self.root, "a")
		os.makedirs(os.path.join(self.folder, "b"))
		self.existing = (make_share(name="Projects", mac_path=self.folder, vm_path="C:\\a"),)

	def test_refuses_folder_inside_through_a_symlink(self):
		link = os.path.join(self.root, "link")
		os.symlink(os.path.join(self.folder, "b"), link)
		with self.assertRaisesRegex(ShareError, "Projects"):
			check_new_share(make_share(name="Other", mac_path=link, vm_path="C:\\x"), self.existing)

	def test_accepts_sibling_folder(self):
		sibling = os.path.join(self.root, "ab")
		os.makedirs(sibling)
		check_new_share(make_share(name="Other", mac_path=sibling, vm_path="C:\\x"), self.existing)

	@unittest.skipUnless(os.path.isdir(DATA_VOLUME), "no macOS data volume")
	def test_refuses_home_through_data_volume(self):
		home = os.path.realpath(os.path.expanduser("~"))
		existing = (make_share(name="Projects", mac_path=home, vm_path="C:\\a"),)
		with self.assertRaisesRegex(ShareError, "Projects"):
			check_new_share(make_share(name="Other", mac_path=DATA_VOLUME + home, vm_path="C:\\x"), existing)


class PickPortTests(unittest.TestCase):
	def test_no_shares_gives_first_port(self):
		self.assertEqual(pick_port((), lambda port: True), FIRST_PORT)

	def test_skips_used_port(self):
		self.assertEqual(pick_port((make_share(port=FIRST_PORT),), lambda port: True), FIRST_PORT + 1)

	def test_skips_port_that_cannot_bind(self):
		self.assertEqual(pick_port((), lambda port: port != FIRST_PORT), FIRST_PORT + 1)

	def test_all_ports_used_raises(self):
		shares = tuple(make_share(name="s{}".format(port), port=port) for port in range(FIRST_PORT, LAST_PORT + 1))
		with self.assertRaises(ShareError) as caught:
			pick_port(shares, lambda port: True)
		self.assertIn("47321", str(caught.exception))
		self.assertIn("47420", str(caught.exception))


class ValidateSharesTests(unittest.TestCase):
	def assert_refused(self, items, problem, index=0):
		problems = split_shares(CONFIG_FILE, items)[1]
		self.assertTrue(problems)
		text = problems[0]
		self.assertTrue(text.startswith("{}: shares[{}]: ".format(CONFIG_FILE, index)), text)
		self.assertIn(problem, text)

	def test_valid_list_passes(self):
		self.assertEqual(split_shares(CONFIG_FILE, [share_item(), share_item(name="Other", port=FIRST_PORT + 1, read_only=True)])[1], [])

	def test_item_not_object(self):
		self.assert_refused(["x"], "object")

	def test_missing_keys(self):
		item = share_item()
		del item["vm"]
		del item["port"]
		self.assert_refused([item], "missing keys: vm, port")

	def test_one_bad_field_is_refused(self):
		for changes, problem in (
			({"extra": 1}, "unknown keys: extra"),
			({"name": 3}, "name must be 1 to 32 characters"),
			({"name": ".x"}, "name must be 1 to 32 characters"),
			({"vm": None}, "vm must be a string"),
			({"direction": "sideways"}, "direction must be mac_to_vm"),
			({"direction": "vm_to_mac"}, "direction must be mac_to_vm"),
			({"mac_path": 1}, "mac_path"),
			({"mac_path": "Projects"}, "mac_path"),
			({"vm_path": 1}, "vm_path"),
			({"vm_path": "Users\\c"}, "vm_path"),
			({"name": "CON"}, "name 'CON'"),
			({"name": "nul.txt"}, "name 'nul.txt'"),
			({"name": "a."}, "name 'a.'"),
			({"mac_path": "/"}, "mac_path"),
			({"mac_path": "//"}, "mac_path"),
			({"mac_path": "/."}, "mac_path"),
			({"mac_path": "/.."}, "mac_path"),
			({"vm_path": "C:\\Windows"}, "vm_path"),
			({"vm_path": "C:\\Data\\"}, "vm_path"),
			({"vm_path": "C:/Data"}, "vm_path"),
			({"vm_path": "C:\\Windows.\\x"}, "vm_path"),
			({"vm_path": "C:\\a\\\\b"}, "vm_path"),
			({"read_only": "yes"}, "read_only"),
			({"port": "47321"}, "port"),
			({"port": True}, "port"),
			({"port": FIRST_PORT - 1}, "port"),
			({"port": LAST_PORT + 1}, "port"),
		):
			with self.subTest(changes=changes):
				self.assert_refused([share_item(**changes)], problem)

	def test_duplicate_name_ignoring_case(self):
		items = [share_item(), share_item(name="projects", port=FIRST_PORT + 1)]
		self.assert_refused(items, "share name 'projects' is used twice (names ignore case)", index=1)

	def test_missing_mac_folder_passes(self):
		self.assertEqual(split_shares(CONFIG_FILE, [share_item(mac_path="/Volumes/Unplugged/Projects")])[1], [])

	def test_duplicate_port(self):
		items = [share_item(), share_item(name="Other")]
		self.assert_refused(items, "port 47321 is used twice", index=1)


class ShareValueTests(unittest.TestCase):
	def test_to_dict_keeps_key_order(self):
		self.assertEqual(list(make_share(port=FIRST_PORT).to_dict()), ["name", "vm", "direction", "mac_path", "vm_path", "read_only", "port"])

	def test_to_dict_matches_the_config_items(self):
		items = [share_item(), share_item(name="Other", port=FIRST_PORT + 1)]
		self.assertEqual([Share(**item).to_dict() for item in items], items)

	def test_shares_for_vm_keeps_only_that_vm(self):
		windows = make_share(name="A")
		linux = make_share(name="B", vm="Linux")
		self.assertEqual(shares_for_vm((windows, linux), "Windows"), (windows,))


if __name__ == "__main__":
	unittest.main()
