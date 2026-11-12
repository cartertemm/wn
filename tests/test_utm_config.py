import json
import os
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

from test_efivars import MICROSOFT_DB, secure_flash, x509_list
from wn.efivars import EFI_IMAGE_SECURITY_DATABASE_GUID, WN_SIGNATURE_OWNER_GUID, find, qcow2_image, read_flash
from wn.utm_config import (
	CONFIGURATION_VERSION,
	EFI_VARS_FILE_NAME,
	SECURE_VARS_TEMPLATE,
	DEFAULT_QEMU_IMG_PATH,
	ARGUMENT_BLOCKS,
	BLOCK_AUDIO,
	BLOCK_BATTERY,
	BLOCK_EXTRA,
	BLOCK_QMP,
	BLOCK_RAMFB,
	UTMConfigError,
	argument_units,
	battery_file_for_bundle,
	block_of,
	build_config,
	default_battery_file,
	config_plist_bytes,
	create_disk_image,
	detach_drives,
	merge_argument_blocks,
	set_display_hardware,
	update_additional_arguments,
	write_bundle,
)

MAC_ADDRESS = "42:9C:1E:00:00:01"

REQUIRED_TOP_LEVEL = ["Information", "System", "QEMU", "Input", "Sharing", "Display", "Drive", "Network", "Serial", "Sound", "Backend", "ConfigurationVersion"]
REQUIRED_INFORMATION = ["Name", "IconCustom", "UUID"]
REQUIRED_SYSTEM = ["Architecture", "Target", "CPU", "CPUFlagsAdd", "CPUFlagsRemove", "CPUCount", "ForceMulticore", "MemorySize", "JITCacheSize"]
REQUIRED_QEMU = ["DebugLog", "UEFIBoot", "RNGDevice", "BalloonDevice", "TPMDevice", "Hypervisor", "RTCLocalTime", "PS2Controller", "AdditionalArguments"]
REQUIRED_INPUT = ["UsbBusSupport", "UsbSharing", "MaximumUsbShare"]
REQUIRED_SHARING = ["DirectoryShareMode", "DirectoryShareReadOnly", "ClipboardSharing"]
REQUIRED_DISPLAY = ["Hardware", "DynamicResolution", "UpscalingFilter", "DownscalingFilter", "NativeResolution"]
REQUIRED_SOUND = ["Hardware"]
REQUIRED_NETWORK = ["Mode", "Hardware", "MacAddress", "IsolateFromHost", "PortForward"]
REQUIRED_DRIVE = ["ImageType", "Interface", "Identifier"]


def options(**overrides):
	values = {"name": "Win11", "mac_address": MAC_ADDRESS, "disk_image": "disk.qcow2", "iso_image": "win11-arm64.iso"}
	values.update(overrides)
	return values


def arguments_of(config):
	return config["QEMU"]["AdditionalArguments"]


def contains_sequence(items, sequence):
	return any(items[index:index + len(sequence)] == sequence for index in range(len(items)))


class RequiredKeyTests(unittest.TestCase):
	def assert_keys(self, mapping, keys):
		missing = [key for key in keys if key not in mapping]
		self.assertEqual(missing, [])

	def assert_valid(self, config):
		self.assert_keys(config, REQUIRED_TOP_LEVEL)
		self.assert_keys(config["Information"], REQUIRED_INFORMATION)
		self.assert_keys(config["System"], REQUIRED_SYSTEM)
		self.assert_keys(config["QEMU"], REQUIRED_QEMU)
		self.assert_keys(config["Input"], REQUIRED_INPUT)
		self.assert_keys(config["Sharing"], REQUIRED_SHARING)
		for display in config["Display"]:
			self.assert_keys(display, REQUIRED_DISPLAY)
		for sound in config["Sound"]:
			self.assert_keys(sound, REQUIRED_SOUND)
		for network in config["Network"]:
			self.assert_keys(network, REQUIRED_NETWORK)
		for drive in config["Drive"]:
			self.assert_keys(drive, REQUIRED_DRIVE)

	def test_default_config_has_required_keys(self):
		self.assert_valid(build_config(options()))

	def test_headless_config_has_required_keys(self):
		self.assert_valid(build_config(options(headless=True)))

	def test_round_trip_through_plistlib(self):
		config = build_config(options())
		data = config_plist_bytes(config)
		self.assertTrue(data.startswith(b"<?xml"))
		self.assertEqual(plistlib.loads(data), config)
		self.assert_valid(plistlib.loads(data))


class ConfigContentTests(unittest.TestCase):
	def test_top_level(self):
		config = build_config(options())
		self.assertEqual(config["Backend"], "QEMU")
		self.assertEqual(config["ConfigurationVersion"], CONFIGURATION_VERSION)
		self.assertEqual(CONFIGURATION_VERSION, 4)

	def test_information(self):
		information = build_config(options())["Information"]
		self.assertEqual(information["Name"], "Win11")
		self.assertEqual(information["Icon"], "windows")
		self.assertIs(information["IconCustom"], False)
		self.assertRegex(information["UUID"], r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")

	def test_system_defaults(self):
		system = build_config(options())["System"]
		self.assertEqual(system["Architecture"], "aarch64")
		self.assertEqual(system["Target"], "virt")
		self.assertEqual(system["CPU"], "default")
		self.assertEqual(system["CPUCount"], 8)
		self.assertEqual(system["MemorySize"], 8192)

	def test_cpu_and_memory(self):
		system = build_config(options(cpu_count=4, memory_mib=6144))["System"]
		self.assertEqual(system["CPUCount"], 4)
		self.assertEqual(system["MemorySize"], 6144)

	def test_qemu_defaults(self):
		qemu = build_config(options())["QEMU"]
		self.assertIs(qemu["UEFIBoot"], True)
		self.assertIs(qemu["TPMDevice"], True)
		self.assertIs(qemu["Hypervisor"], True)
		self.assertIs(qemu["RTCLocalTime"], True)
		self.assertIs(qemu["PS2Controller"], False)

	def test_machine_properties_default_turns_off_kernel_irqchip(self):
		self.assertEqual(build_config(options())["QEMU"]["MachinePropertyOverride"], "kernel-irqchip=off")

	def test_machine_properties(self):
		self.assertEqual(build_config(options(machine_properties="highmem=on"))["QEMU"]["MachinePropertyOverride"], "highmem=on")

	def test_empty_machine_properties_omits_key(self):
		self.assertNotIn("MachinePropertyOverride", build_config(options(machine_properties=""))["QEMU"])

	def test_tpm_and_uefi_off(self):
		qemu = build_config(options(tpm=False, uefi=False))["QEMU"]
		self.assertIs(qemu["TPMDevice"], False)
		self.assertIs(qemu["UEFIBoot"], False)

	def test_qmp_server_argument(self):
		self.assertEqual(arguments_of(build_config(options()))[:4], [
			"-qmp", "tcp:127.0.0.1:4444,server=on,wait=off",
			"-qmp", "tcp:127.0.0.1:4445,server=on,wait=off",
		])
		self.assertEqual(arguments_of(build_config(options(qmp_port=4500)))[:4], [
			"-qmp", "tcp:127.0.0.1:4500,server=on,wait=off",
			"-qmp", "tcp:127.0.0.1:4501,server=on,wait=off",
		])

	def test_qmp_session_port(self):
		self.assertEqual(arguments_of(build_config(options(qmp_session_port=5000)))[:4], [
			"-qmp", "tcp:127.0.0.1:4444,server=on,wait=off",
			"-qmp", "tcp:127.0.0.1:5000,server=on,wait=off",
		])

	def test_qmp_ports_must_differ(self):
		with self.assertRaises(UTMConfigError):
			build_config(options(qmp_session_port=4444))

	def test_drives(self):
		cd, disk = build_config(options())["Drive"]
		self.assertEqual((cd["ImageName"], cd["ImageType"], cd["Interface"], cd["ReadOnly"]), ("win11-arm64.iso", "CD", "USB", True))
		self.assertEqual((disk["ImageName"], disk["ImageType"], disk["Interface"], disk["ReadOnly"]), ("disk.qcow2", "Disk", "NVMe", False))
		self.assertEqual(cd["InterfaceVersion"], 1)
		self.assertNotEqual(cd["Identifier"], disk["Identifier"])

	def test_driver_iso_is_second_cd(self):
		drives = build_config(options(driver_iso_image="virtio-win.iso"))["Drive"]
		self.assertEqual([(drive["ImageName"], drive["ImageType"], drive["Interface"], drive["ReadOnly"]) for drive in drives], [
			("win11-arm64.iso", "CD", "USB", True),
			("virtio-win.iso", "CD", "USB", True),
			("disk.qcow2", "Disk", "NVMe", False),
		])

	def test_display_hardware(self):
		config = build_config(options(display="virtio-gpu-pci"))
		self.assertEqual([display["Hardware"] for display in config["Display"]], ["virtio-gpu-pci"])

	def test_no_iso_means_disk_only(self):
		drives = build_config(options(iso_image=None))["Drive"]
		self.assertEqual([drive["ImageType"] for drive in drives], ["Disk"])

	def test_network(self):
		self.assertEqual(build_config(options())["Network"], [{
			"Mode": "Shared",
			"Hardware": "virtio-net-pci",
			"MacAddress": MAC_ADDRESS,
			"IsolateFromHost": False,
			"PortForward": [],
		}])

	def test_display_when_not_headless(self):
		config = build_config(options())
		self.assertEqual([display["Hardware"] for display in config["Display"]], ["virtio-ramfb"])
		self.assertNotIn("ramfb", arguments_of(config))

	def test_headless(self):
		config = build_config(options(headless=True))
		self.assertEqual(config["Display"], [])
		self.assertTrue(contains_sequence(arguments_of(config), ["-device", "ramfb"]))
		self.assertEqual(config["Serial"], [])

	def test_coreaudio(self):
		config = build_config(options())
		arguments = arguments_of(config)
		self.assertEqual(config["Sound"], [])
		self.assertTrue(contains_sequence(arguments, ["-audiodev", "coreaudio,id=ca0,out.buffer-length=20000"]))
		self.assertTrue(contains_sequence(arguments, ["-device", "intel-hda", "-device", "hda-output,audiodev=ca0"]))

	def test_coreaudio_buffer_length(self):
		arguments = arguments_of(build_config(options(audio_buffer_length_us=10000)))
		self.assertIn("coreaudio,id=ca0,out.buffer-length=10000", arguments)

	def test_audio_args_replace_coreaudio_block(self):
		config = build_config(options(audio_args="-audiodev coreaudio,id=ca0,timer-period=2500 -device usb-audio,audiodev=ca0"))
		arguments = arguments_of(config)
		self.assertEqual(config["Sound"], [])
		self.assertEqual(arguments[4:], ["-audiodev", "coreaudio,id=ca0,timer-period=2500", "-device", "usb-audio,audiodev=ca0"])

	def test_audio_args_follow_ramfb(self):
		arguments = arguments_of(build_config(options(headless=True, audio_args="-device intel-hda")))
		self.assertEqual(arguments[4:], ["-device", "ramfb", "-device", "intel-hda"])

	def test_extra_args_come_last(self):
		arguments = arguments_of(build_config(options(audio_args="-device intel-hda", extra_args="-device virtio-keyboard-pci,id=vkbd")))
		self.assertEqual(arguments[4:], ["-device", "intel-hda", "-device", "virtio-keyboard-pci,id=vkbd"])

	def test_extra_args_with_default_audio(self):
		arguments = arguments_of(build_config(options(extra_args="-device virtio-keyboard-pci")))
		self.assertEqual(arguments[-2:], ["-device", "virtio-keyboard-pci"])

	def test_qmp_comes_first_in_arguments(self):
		arguments = arguments_of(build_config(options(headless=True)))
		self.assertEqual(arguments[:4], ["-qmp", "tcp:127.0.0.1:4444,server=on,wait=off", "-qmp", "tcp:127.0.0.1:4445,server=on,wait=off"])

	def test_each_build_gets_new_uuids(self):
		first = build_config(options())
		second = build_config(options())
		self.assertNotEqual(first["Information"]["UUID"], second["Information"]["UUID"])


BATTERY_FILE = "/vm/Windows.utm/Data/wn-battery.bin"
GROUP_CONTAINER_FILE = "/home/me/Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM/wn-battery-Win11.bin"


def battery_arguments(memory_mib, path):
	return [
		"-machine", "nvdimm=on",
		"-m", "maxmem={}M,slots=1".format(memory_mib + 1),
		"-object", "memory-backend-file,id=wnbattery,share=on,mem-path={},size=1M".format(path),
		"-device", "nvdimm,id=wnbatterynv,memdev=wnbattery",
	]


class BatteryArgumentTests(unittest.TestCase):
	def test_no_nvdimm_by_default(self):
		self.assertNotIn("nvdimm,id=wnbatterynv,memdev=wnbattery", arguments_of(build_config(options())))

	def test_battery_adds_the_nvdimm(self):
		arguments = arguments_of(build_config(options(battery=True, battery_file=BATTERY_FILE)))
		self.assertTrue(contains_sequence(arguments, battery_arguments(8192, BATTERY_FILE)))

	def test_maxmem_follows_memory_size(self):
		arguments = arguments_of(build_config(options(battery=True, battery_file=BATTERY_FILE, memory_mib=4096)))
		self.assertIn("maxmem=4097M,slots=1", arguments)

	def test_battery_comes_before_extra_args(self):
		arguments = arguments_of(build_config(options(audio_args="-device intel-hda", battery=True, battery_file=BATTERY_FILE, extra_args="-device virtio-keyboard-pci")))
		self.assertEqual(arguments[4:], ["-device", "intel-hda"] + battery_arguments(8192, BATTERY_FILE) + ["-device", "virtio-keyboard-pci"])

	def test_machine_properties_are_unchanged(self):
		config = build_config(options(battery=True, battery_file=BATTERY_FILE))
		self.assertEqual(config["QEMU"]["MachinePropertyOverride"], "kernel-irqchip=off")

	def test_battery_needs_a_file(self):
		with self.assertRaises(UTMConfigError) as context:
			build_config(options(battery=True))
		self.assertIn("battery_file", str(context.exception))

	def test_battery_file_without_spaces_or_commas(self):
		for path in ("/vm/My VM.utm/Data/wn-battery.bin", "/vm/a,b/wn-battery.bin"):
			with self.subTest(path=path):
				with self.assertRaises(UTMConfigError) as context:
					build_config(options(battery=True, battery_file=path))
				self.assertIn("battery_file", str(context.exception))

	def test_default_battery_file_is_in_the_utm_group_container(self):
		with mock.patch.dict(os.environ, {"HOME": "/home/me"}):
			self.assertEqual(default_battery_file("Win11"), GROUP_CONTAINER_FILE)

	def test_battery_file_for_bundle_uses_the_bundle_name(self):
		with mock.patch.dict(os.environ, {"HOME": "/home/me"}):
			self.assertEqual(battery_file_for_bundle("/vm/Win11.utm"), GROUP_CONTAINER_FILE)

	def test_group_container_file_gets_a_bare_name(self):
		with mock.patch.dict(os.environ, {"HOME": "/home/me"}):
			arguments = arguments_of(build_config(options(battery=True, battery_file=GROUP_CONTAINER_FILE)))
		self.assertTrue(contains_sequence(arguments, battery_arguments(8192, "wn-battery-Win11.bin")))

	def test_group_container_file_name_without_spaces(self):
		with mock.patch.dict(os.environ, {"HOME": "/home/me"}):
			with self.assertRaises(UTMConfigError) as context:
				build_config(options(battery=True, battery_file=default_battery_file("My VM")))
		self.assertIn("battery_file", str(context.exception))

	def test_write_bundle_puts_the_file_in_the_group_container(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		bundle = write_bundle(directory.name, options(name="Win11", battery=True))
		with open(os.path.join(bundle, "config.plist"), "rb") as plist_file:
			arguments = arguments_of(plistlib.load(plist_file))
		self.assertTrue(contains_sequence(arguments, battery_arguments(8192, "wn-battery-Win11.bin")))


class ConfigValidationTests(unittest.TestCase):
	def test_unbalanced_quote_in_audio_args(self):
		with self.assertRaises(UTMConfigError) as context:
			build_config(options(audio_args="-device 'intel-hda"))
		self.assertIn("audio_args", str(context.exception))

	def test_unbalanced_quote_in_extra_args(self):
		with self.assertRaises(UTMConfigError) as context:
			build_config(options(extra_args="-device 'virtio-keyboard-pci"))
		self.assertIn("extra_args", str(context.exception))

	def test_unknown_option(self):
		with self.assertRaises(UTMConfigError) as context:
			build_config(options(cpus=4))
		self.assertIn("cpus", str(context.exception))

	def test_missing_required_option(self):
		for key in ("name", "mac_address"):
			values = options()
			del values[key]
			with self.subTest(key=key):
				with self.assertRaises(UTMConfigError) as context:
					build_config(values)
				self.assertIn(key, str(context.exception))


class WriteBundleTests(unittest.TestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.directory = directory.name

	def test_creates_bundle(self):
		bundle = write_bundle(self.directory, options())
		self.assertEqual(bundle, os.path.join(self.directory, "Win11.utm"))
		self.assertEqual(sorted(os.listdir(bundle)), ["Data", "config.plist"])
		self.assertEqual(os.listdir(os.path.join(bundle, "Data")), [])
		with open(os.path.join(bundle, "config.plist"), "rb") as plist_file:
			config = plistlib.load(plist_file)
		self.assertEqual(config["Information"]["Name"], "Win11")
		self.assertEqual(config["ConfigurationVersion"], 4)

	def test_refuses_to_overwrite(self):
		write_bundle(self.directory, options())
		with self.assertRaises(UTMConfigError) as context:
			write_bundle(self.directory, options())
		self.assertIn("config.plist", str(context.exception))

	def test_keeps_existing_data(self):
		data = os.path.join(self.directory, "Win11.utm", "Data")
		os.makedirs(data)
		open(os.path.join(data, "disk.qcow2"), "w").close()
		write_bundle(self.directory, options())
		self.assertEqual(os.listdir(data), ["disk.qcow2"])


class SecureBootCertTests(unittest.TestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.directory = directory.name
		self.template = os.path.join(self.directory, "edk2-arm-secure-vars.fd")
		self.cert = os.path.join(self.directory, "wn-sb.cer")
		with open(self.template, "wb") as template_file:
			template_file.write(qcow2_image(secure_flash()))
		with open(self.cert, "wb") as cert_file:
			cert_file.write(b"our cert")
		patcher = mock.patch("wn.utm_config.SECURE_VARS_TEMPLATE", self.template)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_template_is_utm_secure_vars(self):
		self.assertEqual(SECURE_VARS_TEMPLATE, "/Applications/UTM.app/Contents/Resources/qemu/edk2-arm-secure-vars.fd")

	def test_writes_efi_vars_with_the_cert(self):
		bundle = write_bundle(self.directory, options(secure_boot_cert=self.cert))
		vars_path = os.path.join(bundle, "Data", EFI_VARS_FILE_NAME)
		self.assertEqual(EFI_VARS_FILE_NAME, "efi_vars.fd")
		db = find(read_flash(vars_path), "db", EFI_IMAGE_SECURITY_DATABASE_GUID)
		self.assertEqual(db.data, MICROSOFT_DB + x509_list(WN_SIGNATURE_OWNER_GUID, b"our cert"))

	def test_config_is_unchanged(self):
		bundle = write_bundle(self.directory, options(secure_boot_cert=self.cert))
		with open(os.path.join(bundle, "config.plist"), "rb") as plist_file:
			config = plistlib.load(plist_file)
		self.assertNotIn(self.cert, config_plist_bytes(config).decode())
		self.assertTrue(config["QEMU"]["TPMDevice"])

	def test_no_vars_file_without_the_option(self):
		bundle = write_bundle(self.directory, options())
		self.assertEqual(os.listdir(os.path.join(bundle, "Data")), [])

	def test_needs_tpm_and_uefi(self):
		for overrides in ({"tpm": False}, {"uefi": False}):
			with self.assertRaises(UTMConfigError) as context:
				build_config(options(secure_boot_cert=self.cert, **overrides))
			self.assertIn("secure_boot_cert", str(context.exception))

	def test_refuses_to_overwrite_vars(self):
		data = os.path.join(self.directory, "Win11.utm", "Data")
		os.makedirs(data)
		open(os.path.join(data, EFI_VARS_FILE_NAME), "w").close()
		with self.assertRaises(UTMConfigError) as context:
			write_bundle(self.directory, options(secure_boot_cert=self.cert))
		self.assertIn(EFI_VARS_FILE_NAME, str(context.exception))

	def test_missing_cert(self):
		with self.assertRaises(UTMConfigError) as context:
			write_bundle(self.directory, options(secure_boot_cert=os.path.join(self.directory, "missing.cer")))
		self.assertIn("missing.cer", str(context.exception))
		self.assertFalse(os.path.exists(os.path.join(self.directory, "Win11.utm", "config.plist")))


class EditBundleTests(unittest.TestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.bundle = write_bundle(directory.name, options(driver_iso_image="wnsetup.iso"))
		for image in ("win11-arm64.iso", "wnsetup.iso", "disk.qcow2"):
			open(os.path.join(self.bundle, "Data", image), "w").close()

	def read_config(self):
		with open(os.path.join(self.bundle, "config.plist"), "rb") as plist_file:
			return plistlib.load(plist_file)

	def test_set_display_hardware(self):
		before = self.read_config()
		set_display_hardware(self.bundle, "virtio-gpu-pci")
		after = self.read_config()
		self.assertEqual(after["Display"][0]["Hardware"], "virtio-gpu-pci")
		after["Display"][0]["Hardware"] = before["Display"][0]["Hardware"]
		self.assertEqual(after, before)

	def test_detach_drives_removes_entries_and_files(self):
		detach_drives(self.bundle, ["win11-arm64.iso", "wnsetup.iso"])
		self.assertEqual([drive["ImageName"] for drive in self.read_config()["Drive"]], ["disk.qcow2"])
		self.assertEqual(os.listdir(os.path.join(self.bundle, "Data")), ["disk.qcow2"])

	def test_edit_missing_bundle(self):
		with self.assertRaises(UTMConfigError):
			set_display_hardware(os.path.join(self.bundle, "missing.utm"), "virtio-gpu-pci")


class UpdateAdditionalArgumentsTests(unittest.TestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.bundle = write_bundle(directory.name, options())
		self.plist_path = os.path.join(self.bundle, "config.plist")
		with open(self.plist_path, "rb") as plist_file:
			config = plistlib.load(plist_file)
		config["QEMU"]["AdditionalArguments"] = ["-qmp", "tcp:127.0.0.1:4444,server=on,wait=off"]
		with open(self.plist_path, "wb") as plist_file:
			plistlib.dump(config, plist_file)
		self.original = config

	def read_config(self):
		with open(self.plist_path, "rb") as plist_file:
			return plistlib.load(plist_file)

	def test_rewrites_only_additional_arguments(self):
		before, after = update_additional_arguments(self.bundle, {"qmp_port": 4444}, ARGUMENT_BLOCKS)
		config = self.read_config()
		self.assertEqual(before, ["-qmp", "tcp:127.0.0.1:4444,server=on,wait=off"])
		self.assertEqual(arguments_of(config), after)
		self.assertEqual(after[:4], ["-qmp", "tcp:127.0.0.1:4444,server=on,wait=off", "-qmp", "tcp:127.0.0.1:4445,server=on,wait=off"])
		self.assertIn("coreaudio,id=ca0,out.buffer-length=20000", after)
		config["QEMU"]["AdditionalArguments"] = self.original["QEMU"]["AdditionalArguments"]
		self.assertEqual(config, self.original)

	def test_every_block_matches_a_new_bundle(self):
		update_additional_arguments(self.bundle, {}, ARGUMENT_BLOCKS)
		self.assertEqual(arguments_of(self.read_config()), arguments_of(build_config(options())))

	def test_audio_args(self):
		_, after = update_additional_arguments(self.bundle, {"audio_args": "-audiodev coreaudio,id=ca0 -device intel-hda"}, [BLOCK_AUDIO])
		self.assertEqual(after, ["-qmp", "tcp:127.0.0.1:4444,server=on,wait=off", "-audiodev", "coreaudio,id=ca0", "-device", "intel-hda"])
		self.assertEqual(arguments_of(self.read_config()), after)

	def test_extra_args(self):
		_, after = update_additional_arguments(self.bundle, {"extra_args": "-device virtio-keyboard-pci,id=vkbd"}, [BLOCK_EXTRA])
		self.assertEqual(after[-2:], ["-device", "virtio-keyboard-pci,id=vkbd"])
		self.assertEqual(arguments_of(self.read_config()), after)

	def test_battery_uses_the_bundle_memory_size_and_group_container_file(self):
		config = self.read_config()
		config["System"]["MemorySize"] = 6144
		with open(self.plist_path, "wb") as plist_file:
			plistlib.dump(config, plist_file)
		_, after = update_additional_arguments(self.bundle, {"battery": True}, [BLOCK_BATTERY])
		expected = battery_arguments(6144, "wn-battery-Win11.bin")
		self.assertTrue(contains_sequence(after, expected))
		self.assertEqual(arguments_of(self.read_config()), after)

	def test_battery_with_a_configured_file(self):
		_, after = update_additional_arguments(self.bundle, {"battery": True, "battery_file": BATTERY_FILE}, [BLOCK_BATTERY])
		self.assertTrue(contains_sequence(after, battery_arguments(8192, BATTERY_FILE)))

	def test_missing_bundle(self):
		with self.assertRaises(UTMConfigError) as context:
			update_additional_arguments(os.path.join(self.bundle, "missing.utm"), {}, [])
		self.assertIn("config.plist", str(context.exception))

	def test_unknown_option(self):
		with self.assertRaises(UTMConfigError):
			update_additional_arguments(self.bundle, {"colour": "red"}, [])
		self.assertEqual(self.read_config(), self.original)

	def test_unknown_block(self):
		with self.assertRaises(UTMConfigError):
			update_additional_arguments(self.bundle, {}, ["display"])

	def set_arguments(self, arguments):
		config = self.read_config()
		config["QEMU"]["AdditionalArguments"] = arguments
		with open(self.plist_path, "wb") as plist_file:
			plistlib.dump(config, plist_file)

	def test_no_blocks_keeps_every_argument_and_the_file(self):
		self.set_arguments(CUSTOM_ARGUMENTS)
		with open(self.plist_path, "rb") as plist_file:
			original = plist_file.read()
		before, after = update_additional_arguments(self.bundle, {}, [])
		self.assertEqual(before, CUSTOM_ARGUMENTS)
		self.assertEqual(after, CUSTOM_ARGUMENTS)
		with open(self.plist_path, "rb") as plist_file:
			self.assertEqual(plist_file.read(), original)

	def test_battery_keeps_tuned_audio_ramfb_and_extra_args(self):
		self.set_arguments(CUSTOM_ARGUMENTS)
		_, after = update_additional_arguments(self.bundle, {"battery": True, "battery_file": BATTERY_FILE}, [BLOCK_BATTERY])
		self.assertEqual(after, CUSTOM_ARGUMENTS[:-2] + battery_arguments(8192, BATTERY_FILE) + CUSTOM_ARGUMENTS[-2:])

	def test_battery_is_replaced_in_place(self):
		self.set_arguments(CUSTOM_ARGUMENTS[:-2] + battery_arguments(4096, BATTERY_FILE) + CUSTOM_ARGUMENTS[-2:])
		_, after = update_additional_arguments(self.bundle, {"battery": True, "battery_file": BATTERY_FILE}, [BLOCK_BATTERY])
		self.assertEqual(after, CUSTOM_ARGUMENTS[:-2] + battery_arguments(8192, BATTERY_FILE) + CUSTOM_ARGUMENTS[-2:])

	def test_removing_the_battery(self):
		self.set_arguments(CUSTOM_ARGUMENTS[:-2] + battery_arguments(8192, BATTERY_FILE) + CUSTOM_ARGUMENTS[-2:])
		_, after = update_additional_arguments(self.bundle, {}, [BLOCK_BATTERY])
		self.assertEqual(after, CUSTOM_ARGUMENTS)

	def test_qmp_block_keeps_its_place(self):
		self.set_arguments(CUSTOM_ARGUMENTS)
		_, after = update_additional_arguments(self.bundle, {"qmp_port": 5000}, [BLOCK_QMP])
		self.assertEqual(after[:4], ["-qmp", "tcp:127.0.0.1:5000,server=on,wait=off", "-qmp", "tcp:127.0.0.1:5001,server=on,wait=off"])
		self.assertEqual(after[4:], CUSTOM_ARGUMENTS[4:])


CUSTOM_ARGUMENTS = [
	"-qmp", "tcp:127.0.0.1:4444,server=on,wait=off",
	"-qmp", "tcp:127.0.0.1:4445,server=on,wait=off",
	"-device", "ramfb",
	"-audiodev", "coreaudio,id=ca0,timer-period=2500,out.buffer-length=5000,out.buffer-count=3",
	"-device", "intel-hda",
	"-device", "hda-output,audiodev=ca0",
	"-gdb", "tcp:127.0.0.1:4446",
]


class ArgumentBlockTests(unittest.TestCase):
	def test_units(self):
		self.assertEqual(argument_units(["-device", "ramfb", "-S", "-m", "maxmem=9M,slots=1"]), [["-device", "ramfb"], ["-S"], ["-m", "maxmem=9M,slots=1"]])

	def test_blocks(self):
		units = argument_units(CUSTOM_ARGUMENTS + battery_arguments(8192, BATTERY_FILE) + ["-machine", "highmem=on", "-device", "virtio-keyboard-pci"])
		self.assertEqual([block_of(unit) for unit in units], [
			BLOCK_QMP, BLOCK_QMP, BLOCK_RAMFB, BLOCK_AUDIO, BLOCK_AUDIO, BLOCK_AUDIO, BLOCK_EXTRA,
			BLOCK_BATTERY, BLOCK_BATTERY, BLOCK_BATTERY, BLOCK_BATTERY, BLOCK_EXTRA, BLOCK_EXTRA,
		])

	def test_missing_block_goes_before_later_blocks(self):
		merged = merge_argument_blocks(["-qmp", "a", "-gdb", "x"], {BLOCK_AUDIO: ["-device", "intel-hda"]})
		self.assertEqual(merged, ["-qmp", "a", "-device", "intel-hda", "-gdb", "x"])


FAKE_QEMU_IMG_SOURCE = """
import json, os, sys
with open(os.environ["FAKE_QEMU_IMG_LOG"], "w") as log:
	json.dump(sys.argv[1:], log)
if os.environ.get("FAKE_QEMU_IMG_FAIL"):
	print("qemu-img: Could not create: Permission denied", file=sys.stderr)
	sys.exit(1)
"""


class CreateDiskImageTests(unittest.TestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.directory = directory.name
		self.fake = os.path.join(self.directory, "fake_qemu_img.py")
		with open(self.fake, "w") as fake_file:
			fake_file.write(FAKE_QEMU_IMG_SOURCE)
		self.log = os.path.join(self.directory, "log.json")
		self.set_environment("FAKE_QEMU_IMG_LOG", self.log)

	def set_environment(self, key, value):
		previous = os.environ.get(key)
		os.environ[key] = value
		self.addCleanup(lambda: os.environ.pop(key) if previous is None else os.environ.__setitem__(key, previous))

	def test_default_path(self):
		self.assertEqual(DEFAULT_QEMU_IMG_PATH, "/opt/homebrew/bin/qemu-img")

	def test_runs_qemu_img_create(self):
		image = os.path.join(self.directory, "disk.qcow2")
		create_disk_image(image, 64, qemu_img_path=[sys.executable, self.fake])
		with open(self.log) as log:
			self.assertEqual(json.load(log), ["create", "-f", "qcow2", image, "64G"])

	def test_failure_includes_stderr(self):
		self.set_environment("FAKE_QEMU_IMG_FAIL", "1")
		with self.assertRaises(UTMConfigError) as context:
			create_disk_image(os.path.join(self.directory, "disk.qcow2"), 64, qemu_img_path=[sys.executable, self.fake])
		self.assertIn("Permission denied", str(context.exception))

	def test_missing_qemu_img(self):
		with self.assertRaises(UTMConfigError) as context:
			create_disk_image(os.path.join(self.directory, "disk.qcow2"), 64, qemu_img_path="/no/such/qemu-img")
		self.assertIn("/no/such/qemu-img", str(context.exception))


if __name__ == "__main__":
	unittest.main()
