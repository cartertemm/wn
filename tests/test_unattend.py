import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from wn.unattend import (
	PAYLOAD_TAG,
	SETUP_DRIVERS,
	UnattendError,
	UnattendOptions,
	build_unattend,
	computer_name_for,
	mac_time_zone,
	setup_commands,
	windows_time_zone,
)

UNATTEND = "urn:schemas-microsoft-com:unattend"
WCM = "http://schemas.microsoft.com/WMIConfig/2002/State"
NS = {"u": UNATTEND}
PUBLIC_KEY_TOKEN = "31bf3856ad364e35"

COMPONENT_PASSES = {
	"Microsoft-Windows-International-Core-WinPE": {"windowsPE"},
	"Microsoft-Windows-Setup": {"windowsPE"},
	"Microsoft-Windows-Shell-Setup": {"specialize", "oobeSystem", "auditSystem", "auditUser", "offlineServicing", "generalize"},
	"Microsoft-Windows-International-Core": {"specialize", "oobeSystem"},
	"Microsoft-Windows-Deployment": {"specialize", "auditSystem", "auditUser", "oobeSystem", "generalize"},
}
SETTING_PASSES = {
	("Microsoft-Windows-Setup", "DiskConfiguration"): {"windowsPE"},
	("Microsoft-Windows-Setup", "ImageInstall"): {"windowsPE"},
	("Microsoft-Windows-Setup", "UserData"): {"windowsPE"},
	("Microsoft-Windows-Shell-Setup", "ComputerName"): {"specialize", "offlineServicing"},
	("Microsoft-Windows-Shell-Setup", "TimeZone"): {"specialize", "oobeSystem", "auditSystem"},
	("Microsoft-Windows-Shell-Setup", "OOBE"): {"oobeSystem"},
	("Microsoft-Windows-Shell-Setup", "UserAccounts"): {"oobeSystem", "auditSystem"},
	("Microsoft-Windows-Shell-Setup", "AutoLogon"): {"specialize", "oobeSystem", "auditSystem"},
	("Microsoft-Windows-Shell-Setup", "FirstLogonCommands"): {"oobeSystem"},
	("Microsoft-Windows-Deployment", "RunSynchronous"): {"specialize", "auditSystem", "auditUser", "generalize"},
}


def options(**overrides):
	values = {"username": "carter", "password": "", "computer_name": "WINDOWS", "time_zone": "UTC", "first_logon_commands": ("cmd /c one", "cmd /c two")}
	values.update(overrides)
	return UnattendOptions(**values)


def parse(unattend_options=None):
	return ET.fromstring(build_unattend(unattend_options or options()))


def component(root, pass_name, name):
	for settings in root.findall("u:settings", NS):
		if settings.get("pass") != pass_name:
			continue
		for element in settings.findall("u:component", NS):
			if element.get("name") == name:
				return element
	raise AssertionError("no {} in {}".format(name, pass_name))


def text(element, path):
	found = element.find(path, NS)
	if found is None:
		raise AssertionError("missing " + path)
	return found.text or ""


class StructureTests(unittest.TestCase):
	def test_root_and_passes(self):
		root = parse()
		self.assertEqual(root.tag, "{%s}unattend" % UNATTEND)
		self.assertEqual([settings.get("pass") for settings in root.findall("u:settings", NS)], ["windowsPE", "specialize", "oobeSystem"])

	def test_every_component_is_arm64_and_allowed_in_its_pass(self):
		root = parse()
		for settings in root.findall("u:settings", NS):
			for element in settings.findall("u:component", NS):
				name = element.get("name")
				self.assertIn(name, COMPONENT_PASSES)
				self.assertIn(settings.get("pass"), COMPONENT_PASSES[name])
				self.assertEqual(element.get("processorArchitecture"), "arm64")
				self.assertEqual(element.get("publicKeyToken"), PUBLIC_KEY_TOKEN)
				self.assertEqual(element.get("language"), "neutral")
				self.assertEqual(element.get("versionScope"), "nonSxS")
				for setting in element:
					key = (name, setting.tag.split("}")[1])
					if key in SETTING_PASSES:
						self.assertIn(settings.get("pass"), SETTING_PASSES[key], key)

	def test_list_items_use_wcm_add(self):
		root = parse()
		for tag in ("Disk", "CreatePartition", "ModifyPartition", "MetaData", "LocalAccount", "SynchronousCommand", "RunSynchronousCommand"):
			for element in root.iter("{%s}%s" % (UNATTEND, tag)):
				self.assertEqual(element.get("{%s}action" % WCM), "add", tag)


class WindowsPETests(unittest.TestCase):
	def setUp(self):
		self.root = parse(options(locale="de-DE"))
		self.setup = component(self.root, "windowsPE", "Microsoft-Windows-Setup")

	def test_language(self):
		international = component(self.root, "windowsPE", "Microsoft-Windows-International-Core-WinPE")
		self.assertEqual(text(international, "u:SetupUILanguage/u:UILanguage"), "de-DE")
		for setting in ("InputLocale", "SystemLocale", "UILanguage", "UserLocale"):
			self.assertEqual(text(international, "u:" + setting), "de-DE")

	def test_partitions(self):
		disk = self.setup.find("u:DiskConfiguration/u:Disk", NS)
		self.assertEqual(text(disk, "u:DiskID"), "0")
		self.assertEqual(text(disk, "u:WillWipeDisk"), "true")
		created = [(text(p, "u:Order"), text(p, "u:Type"), p.findtext("u:Size", None, NS), p.findtext("u:Extend", None, NS)) for p in disk.findall("u:CreatePartitions/u:CreatePartition", NS)]
		self.assertEqual(created, [("1", "EFI", "100", None), ("2", "MSR", "16", None), ("3", "Primary", None, "true")])
		formats = {text(p, "u:PartitionID"): p.findtext("u:Format", None, NS) for p in disk.findall("u:ModifyPartitions/u:ModifyPartition", NS)}
		self.assertEqual(formats, {"1": "FAT32", "2": None, "3": "NTFS"})

	def test_install_target_and_edition(self):
		image = self.setup.find("u:ImageInstall/u:OSImage", NS)
		self.assertEqual(text(image, "u:InstallTo/u:DiskID"), "0")
		self.assertEqual(text(image, "u:InstallTo/u:PartitionID"), "3")
		self.assertEqual(text(image, "u:InstallFrom/u:MetaData/u:Key"), "/IMAGE/NAME")
		self.assertEqual(text(image, "u:InstallFrom/u:MetaData/u:Value"), "Windows 11 Pro")

	def test_edition_by_name(self):
		setup = component(parse(options(edition="Home")), "windowsPE", "Microsoft-Windows-Setup")
		self.assertEqual(text(setup, "u:ImageInstall/u:OSImage/u:InstallFrom/u:MetaData/u:Value"), "Windows 11 Home")

	def test_eula_and_generic_key(self):
		self.assertEqual(text(self.setup, "u:UserData/u:AcceptEula"), "true")
		self.assertEqual(text(self.setup, "u:UserData/u:ProductKey/u:Key"), "VK7JG-NPHTM-C97JM-9MPGT-3V66T")
		self.assertEqual(text(self.setup, "u:UserData/u:ProductKey/u:WillShowUI"), "OnError")

	def test_unknown_edition(self):
		with self.assertRaises(UnattendError) as context:
			build_unattend(options(edition="Ultimate"))
		self.assertIn("Pro", str(context.exception))


class SpecializeTests(unittest.TestCase):
	def test_computer_name_and_time_zone(self):
		shell = component(parse(options(computer_name="MYPC", time_zone="US Mountain Standard Time")), "specialize", "Microsoft-Windows-Shell-Setup")
		self.assertEqual(text(shell, "u:ComputerName"), "MYPC")
		self.assertEqual(text(shell, "u:TimeZone"), "US Mountain Standard Time")

	def test_network_and_privacy_bypass(self):
		deployment = component(parse(), "specialize", "Microsoft-Windows-Deployment")
		paths = [text(command, "u:Path") for command in deployment.findall("u:RunSynchronous/u:RunSynchronousCommand", NS)]
		self.assertTrue(any("BypassNRO" in path for path in paths))
		self.assertTrue(any("DisablePrivacyExperience" in path for path in paths))


class OOBETests(unittest.TestCase):
	def setUp(self):
		self.shell = component(parse(options(password="s3cret")), "oobeSystem", "Microsoft-Windows-Shell-Setup")

	def test_oobe_pages_hidden(self):
		oobe = self.shell.find("u:OOBE", NS)
		for setting in ("HideEULAPage", "HideOEMRegistrationScreen", "HideOnlineAccountScreens", "HideWirelessSetupInOOBE"):
			self.assertEqual(text(oobe, "u:" + setting), "true")
		self.assertEqual(text(oobe, "u:ProtectYourPC"), "3")

	def test_local_admin_account(self):
		account = self.shell.find("u:UserAccounts/u:LocalAccounts/u:LocalAccount", NS)
		self.assertEqual(text(account, "u:Name"), "carter")
		self.assertEqual(text(account, "u:Group"), "Administrators")
		self.assertEqual(text(account, "u:Password/u:Value"), "s3cret")
		self.assertEqual(text(account, "u:Password/u:PlainText"), "true")

	def test_empty_password(self):
		shell = component(parse(options(password="")), "oobeSystem", "Microsoft-Windows-Shell-Setup")
		self.assertEqual(text(shell, "u:UserAccounts/u:LocalAccounts/u:LocalAccount/u:Password/u:Value"), "")

	def test_autologon_once(self):
		logon = self.shell.find("u:AutoLogon", NS)
		self.assertEqual(text(logon, "u:Enabled"), "true")
		self.assertEqual(text(logon, "u:LogonCount"), "1")
		self.assertEqual(text(logon, "u:Username"), "carter")
		self.assertEqual(text(logon, "u:Password/u:Value"), "s3cret")

	def test_first_logon_commands_in_order(self):
		commands = self.shell.findall("u:FirstLogonCommands/u:SynchronousCommand", NS)
		self.assertEqual([text(c, "u:Order") for c in commands], ["1", "2"])
		self.assertEqual([text(c, "u:CommandLine") for c in commands], ["cmd /c one", "cmd /c two"])

	def test_special_characters_are_escaped(self):
		root = parse(options(password="a<b&c", first_logon_commands=("cmd /c echo x >> C:\\log 2>&1",)))
		shell = component(root, "oobeSystem", "Microsoft-Windows-Shell-Setup")
		self.assertEqual(text(shell, "u:FirstLogonCommands/u:SynchronousCommand/u:CommandLine"), "cmd /c echo x >> C:\\log 2>&1")
		self.assertEqual(text(shell, "u:AutoLogon/u:Password/u:Value"), "a<b&c")


class ValidationTests(unittest.TestCase):
	def test_rejects_bad_computer_name(self):
		for name in ("", "A" * 16, "has space", "12345"):
			with self.assertRaises(UnattendError):
				build_unattend(options(computer_name=name))

	def test_rejects_bad_username(self):
		for name in ("", "a/b", "x" * 21):
			with self.assertRaises(UnattendError):
				build_unattend(options(username=name))

	def test_rejects_long_command(self):
		with self.assertRaises(UnattendError):
			build_unattend(options(first_logon_commands=("x" * 1025,)))

	def test_computer_name_for_vm_name(self):
		self.assertEqual(computer_name_for("Windows"), "WINDOWS")
		self.assertEqual(computer_name_for("My Windows 11 Machine"), "MYWINDOWS11MACH")
		self.assertEqual(computer_name_for("123"), "WINDOWS-VM")


class TimeZoneTests(unittest.TestCase):
	def test_known_zone(self):
		self.assertEqual(windows_time_zone("America/Phoenix"), "US Mountain Standard Time")
		self.assertEqual(windows_time_zone("Europe/Berlin"), "W. Europe Standard Time")

	def test_unknown_zone_is_utc(self):
		self.assertEqual(windows_time_zone("Mars/Olympus"), "UTC")

	def test_mac_time_zone_from_link(self):
		with tempfile.TemporaryDirectory() as directory:
			link = os.path.join(directory, "localtime")
			os.symlink("/var/db/timezone/zoneinfo/America/New_York", link)
			self.assertEqual(mac_time_zone(link), "Eastern Standard Time")

	def test_mac_time_zone_missing_link(self):
		self.assertEqual(mac_time_zone("/nonexistent/localtime"), "UTC")


class SetupCommandTests(unittest.TestCase):
	def setUp(self):
		self.commands = setup_commands("utm-guest-tools-0.1.271.exe", "nvda_2026.3.1.exe")

	def test_drivers_first_in_order(self):
		drivers = ["NetKVM", "viogpudo", "vioinput", "viostor", "vioscsi", "vioserial", "Balloon", "viorng"]
		for index, driver in enumerate(drivers):
			self.assertIn("pnputil /add-driver %d:\\virtio\\{}\\w11\\ARM64\\*.inf /install".format(driver), self.commands[index])
			self.assertIn(PAYLOAD_TAG, self.commands[index])
		self.assertIn("DisplaySwitch.exe /internal", self.commands[len(drivers)])

	def test_guest_tools_then_nvda_then_reboot(self):
		joined = "\n".join(self.commands)
		guest_tools = joined.index("utm-guest-tools-0.1.271.exe /S")
		nvda = joined.index("nvda_2026.3.1.exe --install-silent --enable-start-on-logon=True")
		start_after_sign_in = joined.index("nvda_nvda_v1")
		self.assertLess(guest_tools, nvda)
		self.assertLess(nvda, start_after_sign_in)
		self.assertIn("C:\\wn-setup-done", self.commands[-1])
		self.assertTrue(self.commands[-1].endswith("shutdown /r /t 5"))

	def test_redirections_run_in_cmd(self):
		for command in self.commands:
			if ">" in command:
				self.assertTrue(command.startswith("cmd /c "), command)

	def test_without_nvda(self):
		commands = setup_commands("utm-guest-tools-0.1.271.exe", None)
		self.assertNotIn("nvda", "\n".join(commands).lower())

	def test_commands_fit_unattend_limit(self):
		build_unattend(options(first_logon_commands=tuple(self.commands)))

	def test_without_wn_tools(self):
		self.assertNotIn("wn-tools", "\n".join(self.commands))

	def test_wn_tools_build_and_install_before_reboot(self):
		commands = setup_commands("utm-guest-tools-0.1.271.exe", "nvda_2026.3.1.exe", wn_tools=True)
		joined = "\n".join(commands)
		copy = joined.index('xcopy /e /i /q /y %d:\\tools\\wn-tools "%TEMP%\\wn-tools"')
		build = joined.index('cmd /c call "%TEMP%\\wn-tools\\build.cmd" >> C:\\wn-setup.log 2>&1')
		install = joined.index('cmd /c call "%TEMP%\\wn-tools\\install.cmd" >> C:\\wn-setup.log 2>&1')
		self.assertLess(joined.index("nvda_nvda_v1"), copy)
		self.assertLess(copy, build)
		self.assertLess(build, install)
		self.assertTrue(commands[-1].endswith("shutdown /r /t 5"))
		self.assertIn(PAYLOAD_TAG, [command for command in commands if "xcopy" in command][0])
		build_unattend(options(first_logon_commands=tuple(commands)))

	def test_setup_drivers_exclude_display(self):
		self.assertNotIn("viogpudo", SETUP_DRIVERS)
		self.assertIn("NetKVM", SETUP_DRIVERS)
		self.assertIn("viostor", SETUP_DRIVERS)


if __name__ == "__main__":
	unittest.main()
