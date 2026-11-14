"""Generate an ``autounattend.xml`` that installs Windows 11 ARM64 with no questions."""

import dataclasses
import os
import re
import xml.etree.ElementTree as ET
from typing import Tuple

from wn.errors import WnError
from wn.windows_timezones import IANA_TO_WINDOWS

UNATTEND_NAMESPACE = "urn:schemas-microsoft-com:unattend"
WCM_NAMESPACE = "http://schemas.microsoft.com/WMIConfig/2002/State"
COMPONENT_ATTRIBUTES = {
	"processorArchitecture": "arm64",
	"publicKeyToken": "31bf3856ad364e35",
	"language": "neutral",
	"versionScope": "nonSxS",
}
DEFAULT_LOCALE = "en-US"
DEFAULT_EDITION = "Pro"
DEFAULT_TIME_ZONE = "UTC"
EDITION_IMAGE_NAME = "Windows 11 {}"
GENERIC_PRODUCT_KEYS = {
	"Home": "YTMG3-N6DKC-DKB77-7M9GH-8HVX7",
	"Home N": "4CPRK-NM3K3-X6XXQ-RXX86-WXCHW",
	"Home Single Language": "BT79Q-G7N6G-PGBYW-4YWX6-6F4BT",
	"Pro": "VK7JG-NPHTM-C97JM-9MPGT-3V66T",
	"Pro N": "2B87N-8KFHP-DKV6R-Y2C8J-PKCKT",
	"Pro Education": "8PTT6-RNW4C-6V7J2-C2D3X-MHBPB",
	"Pro for Workstations": "DXG7C-N36C4-C4HTG-X4T3X-2YV77",
	"Education": "YNMGQ-8RYV3-4PGQ3-C8XTP-7CFBY",
	"Education N": "84NGF-MHBT6-FXBX8-QWJK7-DRR8H",
}
EFI_PARTITION_MIB = 100
MSR_PARTITION_MIB = 16
INSTALL_DISK_ID = 0
WINDOWS_PARTITION_ID = 3
ADMINISTRATORS_GROUP = "Administrators"
AUTO_LOGON_COUNT = 1
DISABLE_EXPRESS_SETTINGS = 3
COMMAND_LINE_MAX_LENGTH = 1024
COMPUTER_NAME_MAX_LENGTH = 15
USERNAME_MAX_LENGTH = 20
USERNAME_FORBIDDEN_CHARACTERS = '"/\\[]:;|=,+*?<>@'
FALLBACK_COMPUTER_NAME = "WINDOWS-VM"
COMPUTER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
LOCALTIME_PATH = "/etc/localtime"
ZONEINFO_MARKER = "zoneinfo/"

PAYLOAD_TAG = r"wn\payload.tag"
VIRTIO_FOLDER = "virtio"
TOOLS_FOLDER = "tools"
EFI_FOLDER = "efi"
MIC_SPEAKER_SCRIPT = "hide-mic-speaker.ps1"
BATTERY_INSTALLER = "install-wn-acpi.ps1"
BATTERY_NOTIFY_INSTALLER = "install-wn-battery-notify.ps1"
BATTERY_NOTIFY_SOURCE = "wn-battery-notify.cs"
WINPE_DRIVER_FOLDER = "$WinPEDriver$"
DRIVER_SUBPATH = ("w11", "ARM64")
FIRST_LOGON_DRIVERS = ("NetKVM", "viogpudo", "vioinput", "viostor", "vioscsi", "vioserial", "Balloon", "viorng")
SETUP_DRIVERS = tuple(name for name in FIRST_LOGON_DRIVERS if name != "viogpudo")
PAYLOAD_DRIVE_LETTERS = "DEFGHIJKLMNOPQRSTUVWXYZ"
SETUP_LOG = r"C:\wn-setup.log"
SETUP_DONE_MARKER = r"C:\wn-setup-done"
SETUP_DONE_TEXT = "done"
SETUP_REBOOTING_TEXT = "rebooting"
DONE_TASK_NAME = "wn-setup-done"
NVDA_INSTALLED_PATH = r"C:\Program Files\NVDA\nvda.exe"
NVDA_INSTALL_WAIT_ROUNDS = 60
DISPLAY_SETTLE_PINGS = 11
REBOOT_DELAY_SECONDS = 5
POWER_BUTTON_SHUT_DOWN = 3
DISPLAY_NEVER_OFF = 0
POWER_SETTINGS = (
	"/setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION {}".format(POWER_BUTTON_SHUT_DOWN),
	"/setdcvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION {}".format(POWER_BUTTON_SHUT_DOWN),
	"/setactive SCHEME_CURRENT",
	"/change monitor-timeout-ac {}".format(DISPLAY_NEVER_OFF),
	"/change monitor-timeout-dc {}".format(DISPLAY_NEVER_OFF),
)


class UnattendError(WnError):
	"""Raised when the answer file options are not valid for Windows Setup."""


@dataclasses.dataclass(frozen=True)
class UnattendOptions:
	username: str
	password: str = ""
	computer_name: str = "WINDOWS"
	locale: str = DEFAULT_LOCALE
	edition: str = DEFAULT_EDITION
	time_zone: str = DEFAULT_TIME_ZONE
	first_logon_commands: Tuple[str, ...] = ()


def windows_time_zone(iana_name):
	return IANA_TO_WINDOWS.get(iana_name, DEFAULT_TIME_ZONE)


def mac_time_zone(localtime_path=LOCALTIME_PATH):
	"""Return the Windows time zone ID that matches the Mac's time zone, or UTC."""
	try:
		target = os.readlink(localtime_path)
	except OSError:
		return DEFAULT_TIME_ZONE
	_, marker, iana_name = target.partition(ZONEINFO_MARKER)
	return windows_time_zone(iana_name) if marker else DEFAULT_TIME_ZONE


def computer_name_for(vm_name):
	name = re.sub(r"[^A-Za-z0-9-]", "", vm_name).upper()[:COMPUTER_NAME_MAX_LENGTH].strip("-")
	return name if name and not name.isdigit() else FALLBACK_COMPUTER_NAME


def _on_payload_drive(command):
	"""Wrap ``command`` so it runs once with ``%d:`` set to the drive that holds the wn payload."""
	letters = " ".join(PAYLOAD_DRIVE_LETTERS)
	return "cmd /c for %d in ({}) do @if exist %d:\\{} {}".format(letters, PAYLOAD_TAG, command)


def _logged(command):
	return "{} >> {} 2>&1".format(command, SETUP_LOG)


def setup_commands(guest_tools_name, nvda_installer_name=None, battery=True):
	"""Return the first-logon commands that finish the VM: drivers, display, guest tools, NVDA, power settings, audio, battery driver and service, then a reboot."""
	driver_folder = "\\".join(DRIVER_SUBPATH)
	commands = [
		_on_payload_drive(_logged("pnputil /add-driver %d:\\{}\\{}\\{}\\*.inf /install".format(VIRTIO_FOLDER, driver, driver_folder)))
		for driver in FIRST_LOGON_DRIVERS
	]
	commands.append("cmd /c ping -n {} 127.0.0.1 >nul & DisplaySwitch.exe /internal".format(DISPLAY_SETTLE_PINGS))
	commands.append(_on_payload_drive(_logged("%d:\\{}\\{} /S".format(TOOLS_FOLDER, guest_tools_name))))
	if nvda_installer_name:
		commands.append(_on_payload_drive(_logged("%d:\\{}\\{} --install-silent --enable-start-on-logon=True".format(TOOLS_FOLDER, nvda_installer_name))))
		commands.append("cmd /c for /l %i in (1,1,{}) do @if not exist \"{}\" ping -n 3 127.0.0.1 >nul".format(NVDA_INSTALL_WAIT_ROUNDS, NVDA_INSTALLED_PATH))
		commands.append(_logged("cmd /c reg add \"HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Accessibility\" /v Configuration /t REG_SZ /d nvda_nvda_v1 /f"))
	commands += [_logged("cmd /c powercfg {}".format(setting)) for setting in POWER_SETTINGS]
	commands.append(_on_payload_drive(_logged("powershell -ExecutionPolicy Bypass -File %d:\\{}\\{}".format(TOOLS_FOLDER, MIC_SPEAKER_SCRIPT))))
	if battery:
		for installer in (BATTERY_INSTALLER, BATTERY_NOTIFY_INSTALLER):
			commands.append(_on_payload_drive(_logged("powershell -ExecutionPolicy Bypass -File %d:\\{}\\{}".format(EFI_FOLDER, installer))))
	done_task = "schtasks /create /f /tn {name} /sc onstart /ru SYSTEM /tr \"cmd /c echo {done}>{marker} & schtasks /delete /tn {name} /f\"".format(
		name=DONE_TASK_NAME, done=SETUP_DONE_TEXT, marker=SETUP_DONE_MARKER)
	commands.append("cmd /c echo {}>{} & {} & shutdown /r /t {}".format(SETUP_REBOOTING_TEXT, SETUP_DONE_MARKER, done_task, REBOOT_DELAY_SECONDS))
	return commands


def _validate(options):
	if options.edition not in GENERIC_PRODUCT_KEYS:
		raise UnattendError("unknown edition '{}'. Known editions: {}".format(options.edition, ", ".join(GENERIC_PRODUCT_KEYS)))
	name = options.computer_name
	if not name or len(name) > COMPUTER_NAME_MAX_LENGTH or not COMPUTER_NAME_PATTERN.match(name) or name.isdigit():
		raise UnattendError("computer name '{}' must be 1 to {} letters, digits, or hyphens, and not only digits".format(name, COMPUTER_NAME_MAX_LENGTH))
	username = options.username
	if not username or len(username) > USERNAME_MAX_LENGTH or any(character in USERNAME_FORBIDDEN_CHARACTERS for character in username) or not username.strip(". "):
		raise UnattendError("user name '{}' must be 1 to {} characters and must not contain {}".format(username, USERNAME_MAX_LENGTH, USERNAME_FORBIDDEN_CHARACTERS))
	for command in options.first_logon_commands:
		if len(command) > COMMAND_LINE_MAX_LENGTH:
			raise UnattendError("first logon command is longer than {} characters: {}".format(COMMAND_LINE_MAX_LENGTH, command[:80]))


def _tag(name):
	return "{%s}%s" % (UNATTEND_NAMESPACE, name)


def _add(parent, name, value=None, list_item=False):
	element = ET.SubElement(parent, _tag(name))
	if list_item:
		element.set("{%s}action" % WCM_NAMESPACE, "add")
	if value is not None:
		element.text = str(value).lower() if isinstance(value, bool) else str(value)
	return element


def _add_values(parent, values):
	for name, value in values:
		_add(parent, name, value)


def _component(settings, name):
	element = _add(settings, "component")
	element.set("name", name)
	for attribute, value in COMPONENT_ATTRIBUTES.items():
		element.set(attribute, value)
	return element


def _locales(parent, locale):
	_add_values(parent, [(name, locale) for name in ("InputLocale", "SystemLocale", "UILanguage", "UserLocale")])


def _password(parent, password):
	element = _add(parent, "Password")
	_add_values(element, [("Value", password), ("PlainText", True)])


def _disk_configuration(setup):
	disk = _add(_add(setup, "DiskConfiguration"), "Disk", list_item=True)
	_add_values(disk, [("DiskID", INSTALL_DISK_ID), ("WillWipeDisk", True)])
	created = _add(disk, "CreatePartitions")
	for order, partition_type, size in ((1, "EFI", EFI_PARTITION_MIB), (2, "MSR", MSR_PARTITION_MIB), (3, "Primary", None)):
		partition = _add(created, "CreatePartition", list_item=True)
		_add_values(partition, [("Order", order), ("Type", partition_type)])
		if size is None:
			_add(partition, "Extend", True)
		else:
			_add(partition, "Size", size)
	modified = _add(disk, "ModifyPartitions")
	for order, extra in ((1, [("Format", "FAT32"), ("Label", "System")]), (2, []), (3, [("Format", "NTFS"), ("Label", "Windows"), ("Letter", "C")])):
		partition = _add(modified, "ModifyPartition", list_item=True)
		_add_values(partition, [("Order", order), ("PartitionID", order)] + extra)


def _windows_pe(root, options):
	settings = _add(root, "settings")
	settings.set("pass", "windowsPE")
	international = _component(settings, "Microsoft-Windows-International-Core-WinPE")
	_add(_add(international, "SetupUILanguage"), "UILanguage", options.locale)
	_locales(international, options.locale)
	setup = _component(settings, "Microsoft-Windows-Setup")
	_disk_configuration(setup)
	image = _add(_add(setup, "ImageInstall"), "OSImage")
	install_from = _add(image, "InstallFrom")
	metadata = _add(install_from, "MetaData", list_item=True)
	_add_values(metadata, [("Key", "/IMAGE/NAME"), ("Value", EDITION_IMAGE_NAME.format(options.edition))])
	_add_values(_add(image, "InstallTo"), [("DiskID", INSTALL_DISK_ID), ("PartitionID", WINDOWS_PARTITION_ID)])
	user_data = _add(setup, "UserData")
	_add(user_data, "AcceptEula", True)
	product_key = _add(user_data, "ProductKey")
	_add_values(product_key, [("Key", GENERIC_PRODUCT_KEYS[options.edition]), ("WillShowUI", "OnError")])


def _specialize(root, options):
	settings = _add(root, "settings")
	settings.set("pass", "specialize")
	shell = _component(settings, "Microsoft-Windows-Shell-Setup")
	_add_values(shell, [("ComputerName", options.computer_name), ("TimeZone", options.time_zone)])
	deployment = _component(settings, "Microsoft-Windows-Deployment")
	commands = _add(deployment, "RunSynchronous")
	for order, path in enumerate((
		r"reg add HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE /v BypassNRO /t REG_DWORD /d 1 /f",
		r"reg add HKLM\SOFTWARE\Policies\Microsoft\Windows\OOBE /v DisablePrivacyExperience /t REG_DWORD /d 1 /f",
		r"reg add HKLM\SYSTEM\CurrentControlSet\Control\BitLocker /v PreventDeviceEncryption /t REG_DWORD /d 1 /f",
		r'reg add "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power" /v HiberbootEnabled /t REG_DWORD /d 0 /f',
	), start=1):
		_add_values(_add(commands, "RunSynchronousCommand", list_item=True), [("Order", order), ("Path", path)])


def _oobe_system(root, options):
	settings = _add(root, "settings")
	settings.set("pass", "oobeSystem")
	_locales(_component(settings, "Microsoft-Windows-International-Core"), options.locale)
	shell = _component(settings, "Microsoft-Windows-Shell-Setup")
	_add_values(_add(shell, "OOBE"), [
		("HideEULAPage", True),
		("HideOEMRegistrationScreen", True),
		("HideOnlineAccountScreens", True),
		("HideWirelessSetupInOOBE", True),
		("ProtectYourPC", DISABLE_EXPRESS_SETTINGS),
	])
	account = _add(_add(_add(shell, "UserAccounts"), "LocalAccounts"), "LocalAccount", list_item=True)
	_password(account, options.password)
	_add_values(account, [("DisplayName", options.username), ("Group", ADMINISTRATORS_GROUP), ("Name", options.username)])
	logon = _add(shell, "AutoLogon")
	_password(logon, options.password)
	_add_values(logon, [("Enabled", True), ("LogonCount", AUTO_LOGON_COUNT), ("Username", options.username)])
	commands = _add(shell, "FirstLogonCommands")
	for order, command_line in enumerate(options.first_logon_commands, start=1):
		command = _add(commands, "SynchronousCommand", list_item=True)
		_add_values(command, [("Order", order), ("CommandLine", command_line), ("RequiresUserInput", False)])


def build_unattend(options):
	"""Return the answer file as UTF-8 XML bytes."""
	_validate(options)
	ET.register_namespace("", UNATTEND_NAMESPACE)
	ET.register_namespace("wcm", WCM_NAMESPACE)
	root = ET.Element(_tag("unattend"))
	_windows_pe(root, options)
	_specialize(root, options)
	_oobe_system(root, options)
	ET.indent(root, space="\t")
	return ET.tostring(root, encoding="utf-8", xml_declaration=True)
