import json
import os
import plistlib
import sys
import tempfile
import unittest

from wn.utm_config import (
	CONFIGURATION_VERSION,
	DEFAULT_QEMU_IMG_PATH,
	UTMConfigError,
	build_config,
	config_plist_bytes,
	create_disk_image,
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

	def test_headless_coreaudio_config_has_required_keys(self):
		self.assert_valid(build_config(options(headless=True, audio="coreaudio")))

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
		self.assertEqual(arguments_of(build_config(options())), [
			"-qmp", "tcp:127.0.0.1:4444,server=on,wait=off",
			"-qmp", "tcp:127.0.0.1:4445,server=on,wait=off",
		])
		self.assertEqual(arguments_of(build_config(options(qmp_port=4500))), [
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

	def test_spice_audio_is_default(self):
		config = build_config(options())
		self.assertEqual(config["Sound"], [{"Hardware": "intel-hda"}])
		self.assertNotIn("-audiodev", arguments_of(config))

	def test_coreaudio(self):
		config = build_config(options(audio="coreaudio"))
		arguments = arguments_of(config)
		self.assertEqual(config["Sound"], [])
		self.assertTrue(contains_sequence(arguments, ["-audiodev", "coreaudio,id=ca0,out.buffer-length=20000"]))
		self.assertTrue(contains_sequence(arguments, ["-device", "intel-hda", "-device", "hda-output,audiodev=ca0"]))

	def test_coreaudio_buffer_length(self):
		arguments = arguments_of(build_config(options(audio="coreaudio", audio_buffer_length_us=10000)))
		self.assertIn("coreaudio,id=ca0,out.buffer-length=10000", arguments)

	def test_audio_args_replace_coreaudio_block(self):
		config = build_config(options(audio="coreaudio", audio_args="-audiodev coreaudio,id=ca0,timer-period=2500 -device usb-audio,audiodev=ca0"))
		arguments = arguments_of(config)
		self.assertEqual(config["Sound"], [])
		self.assertEqual(arguments[4:], ["-audiodev", "coreaudio,id=ca0,timer-period=2500", "-device", "usb-audio,audiodev=ca0"])

	def test_audio_args_follow_ramfb(self):
		arguments = arguments_of(build_config(options(headless=True, audio="coreaudio", audio_args="-device intel-hda")))
		self.assertEqual(arguments[4:], ["-device", "ramfb", "-device", "intel-hda"])

	def test_extra_args_come_last(self):
		arguments = arguments_of(build_config(options(audio="coreaudio", audio_args="-device intel-hda", extra_args="-device virtio-keyboard-pci,id=vkbd")))
		self.assertEqual(arguments[4:], ["-device", "intel-hda", "-device", "virtio-keyboard-pci,id=vkbd"])

	def test_extra_args_with_default_audio(self):
		arguments = arguments_of(build_config(options(extra_args="-device virtio-keyboard-pci")))
		self.assertEqual(arguments[-2:], ["-device", "virtio-keyboard-pci"])

	def test_qmp_comes_first_in_arguments(self):
		arguments = arguments_of(build_config(options(headless=True, audio="coreaudio")))
		self.assertEqual(arguments[:4], ["-qmp", "tcp:127.0.0.1:4444,server=on,wait=off", "-qmp", "tcp:127.0.0.1:4445,server=on,wait=off"])

	def test_each_build_gets_new_uuids(self):
		first = build_config(options())
		second = build_config(options())
		self.assertNotEqual(first["Information"]["UUID"], second["Information"]["UUID"])


class ConfigValidationTests(unittest.TestCase):
	def test_audio_args_need_coreaudio(self):
		with self.assertRaises(UTMConfigError) as context:
			build_config(options(audio="spice", audio_args="-device intel-hda"))
		self.assertIn("audio_args", str(context.exception))

	def test_unbalanced_quote_in_audio_args(self):
		with self.assertRaises(UTMConfigError) as context:
			build_config(options(audio="coreaudio", audio_args="-device 'intel-hda"))
		self.assertIn("audio_args", str(context.exception))

	def test_unbalanced_quote_in_extra_args(self):
		with self.assertRaises(UTMConfigError) as context:
			build_config(options(extra_args="-device 'virtio-keyboard-pci"))
		self.assertIn("extra_args", str(context.exception))

	def test_unknown_audio_mode(self):
		with self.assertRaises(UTMConfigError) as context:
			build_config(options(audio="pulse"))
		self.assertIn("pulse", str(context.exception))

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


class UpdateAdditionalArgumentsTests(unittest.TestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.bundle = write_bundle(directory.name, options(audio="coreaudio"))
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
		new_arguments = update_additional_arguments(self.bundle, {"audio": "coreaudio", "qmp_port": 4444})
		config = self.read_config()
		self.assertEqual(arguments_of(config), new_arguments)
		self.assertEqual(new_arguments[:4], ["-qmp", "tcp:127.0.0.1:4444,server=on,wait=off", "-qmp", "tcp:127.0.0.1:4445,server=on,wait=off"])
		self.assertIn("coreaudio,id=ca0,out.buffer-length=20000", new_arguments)
		config["QEMU"]["AdditionalArguments"] = self.original["QEMU"]["AdditionalArguments"]
		self.assertEqual(config, self.original)

	def test_matches_a_new_bundle(self):
		update_additional_arguments(self.bundle, {"audio": "coreaudio"})
		self.assertEqual(arguments_of(self.read_config()), arguments_of(build_config(options(audio="coreaudio"))))

	def test_audio_args(self):
		new_arguments = update_additional_arguments(self.bundle, {"audio": "coreaudio", "audio_args": "-audiodev coreaudio,id=ca0 -device intel-hda"})
		self.assertEqual(new_arguments[4:], ["-audiodev", "coreaudio,id=ca0", "-device", "intel-hda"])
		self.assertEqual(arguments_of(self.read_config()), new_arguments)

	def test_extra_args(self):
		new_arguments = update_additional_arguments(self.bundle, {"audio": "coreaudio", "extra_args": "-device virtio-keyboard-pci,id=vkbd"})
		self.assertEqual(new_arguments[-2:], ["-device", "virtio-keyboard-pci,id=vkbd"])
		self.assertEqual(arguments_of(self.read_config()), new_arguments)

	def test_missing_bundle(self):
		with self.assertRaises(UTMConfigError) as context:
			update_additional_arguments(os.path.join(self.bundle, "missing.utm"), {})
		self.assertIn("config.plist", str(context.exception))

	def test_unknown_option(self):
		with self.assertRaises(UTMConfigError):
			update_additional_arguments(self.bundle, {"colour": "red"})
		self.assertEqual(self.read_config(), self.original)


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
