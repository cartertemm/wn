"""Write a UTM 5 ``.utm`` bundle (config.plist version 4) for a Windows 11 ARM64 QEMU VM."""

import os
import plistlib
import subprocess
import uuid

from wn.errors import WnError

CONFIGURATION_VERSION = 4
BUNDLE_SUFFIX = ".utm"
CONFIG_FILE_NAME = "config.plist"
DATA_DIRECTORY_NAME = "Data"
DEFAULT_QEMU_IMG_PATH = "/opt/homebrew/bin/qemu-img"
QMP_HOST = "127.0.0.1"
AUDIODEV_ID = "ca0"
AUDIO_SPICE = "spice"
AUDIO_COREAUDIO = "coreaudio"
AUDIO_MODES = (AUDIO_SPICE, AUDIO_COREAUDIO)
MAXIMUM_USB_SHARE = 3
INTERFACE_VERSION = 1

REQUIRED_OPTIONS = ("name", "mac_address")
DEFAULT_OPTIONS = {
	"cpu_count": 8,
	"memory_mib": 8192,
	"disk_image": "disk.qcow2",
	"iso_image": None,
	"driver_iso_image": None,
	"machine_properties": "kernel-irqchip=off",
	"headless": False,
	"qmp_port": 4444,
	"audio": AUDIO_SPICE,
	"audio_buffer_length_us": 20000,
	"tpm": True,
	"uefi": True,
}


class UTMConfigError(WnError):
	"""Raised when VM options are invalid or the bundle or disk image cannot be created."""


def _new_uuid():
	return str(uuid.uuid4()).upper()


def _resolve_options(options):
	unknown = sorted(set(options) - set(REQUIRED_OPTIONS) - set(DEFAULT_OPTIONS))
	if unknown:
		raise UTMConfigError("unknown VM options: {}. Known options: {}".format(", ".join(unknown), ", ".join(REQUIRED_OPTIONS + tuple(DEFAULT_OPTIONS))))
	missing = [key for key in REQUIRED_OPTIONS if not options.get(key)]
	if missing:
		raise UTMConfigError("missing VM options: {}".format(", ".join(missing)))
	resolved = dict(DEFAULT_OPTIONS, **options)
	if resolved["audio"] not in AUDIO_MODES:
		raise UTMConfigError("audio mode '{}' is not supported. Use one of: {}".format(resolved["audio"], ", ".join(AUDIO_MODES)))
	return resolved


def _additional_arguments(options):
	arguments = ["-qmp", "tcp:{}:{},server=on,wait=off".format(QMP_HOST, options["qmp_port"])]
	if options["headless"]:
		arguments += ["-device", "ramfb"]
	if options["audio"] == AUDIO_COREAUDIO:
		arguments += [
			"-audiodev", "coreaudio,id={},out.buffer-length={}".format(AUDIODEV_ID, options["audio_buffer_length_us"]),
			"-device", "intel-hda",
			"-device", "hda-output,audiodev={}".format(AUDIODEV_ID),
		]
	return arguments


def _drive(image_name, image_type, interface, read_only):
	return {
		"Identifier": _new_uuid(),
		"ImageName": image_name,
		"ImageType": image_type,
		"Interface": interface,
		"InterfaceVersion": INTERFACE_VERSION,
		"ReadOnly": read_only,
	}


def _drives(options):
	drives = []
	for image in (options["iso_image"], options["driver_iso_image"]):
		if image:
			drives.append(_drive(image, "CD", "USB", True))
	drives.append(_drive(options["disk_image"], "Disk", "NVMe", False))
	return drives


def _displays(options):
	if options["headless"]:
		return []
	return [{
		"Hardware": "virtio-ramfb",
		"DynamicResolution": True,
		"NativeResolution": False,
		"UpscalingFilter": "Linear",
		"DownscalingFilter": "Linear",
	}]


def _sounds(options):
	if options["audio"] == AUDIO_COREAUDIO:
		return []
	return [{"Hardware": "intel-hda"}]


def _qemu(options):
	qemu = {
		"DebugLog": True,
		"UEFIBoot": options["uefi"],
		"RNGDevice": True,
		"BalloonDevice": False,
		"TPMDevice": options["tpm"],
		"Hypervisor": True,
		"TSO": False,
		"RTCLocalTime": True,
		"PS2Controller": False,
		"AdditionalArguments": _additional_arguments(options),
	}
	if options["machine_properties"]:
		qemu["MachinePropertyOverride"] = options["machine_properties"]
	return qemu


def build_config(options):
	"""Return the config.plist contents as a dict. ``options`` needs ``name`` and ``mac_address``."""
	options = _resolve_options(options)
	return {
		"Backend": "QEMU",
		"ConfigurationVersion": CONFIGURATION_VERSION,
		"Information": {
			"Name": options["name"],
			"Icon": "windows",
			"IconCustom": False,
			"UUID": _new_uuid(),
		},
		"System": {
			"Architecture": "aarch64",
			"Target": "virt",
			"CPU": "default",
			"CPUFlagsAdd": [],
			"CPUFlagsRemove": [],
			"CPUCount": options["cpu_count"],
			"ForceMulticore": False,
			"MemorySize": options["memory_mib"],
			"JITCacheSize": 0,
		},
		"QEMU": _qemu(options),
		"Input": {
			"UsbBusSupport": "3.0",
			"UsbSharing": True,
			"MaximumUsbShare": MAXIMUM_USB_SHARE,
		},
		"Sharing": {
			"DirectoryShareMode": "None",
			"DirectoryShareReadOnly": False,
			"ClipboardSharing": True,
		},
		"Display": _displays(options),
		"Drive": _drives(options),
		"Network": [{
			"Mode": "Shared",
			"Hardware": "virtio-net-pci",
			"MacAddress": options["mac_address"],
			"IsolateFromHost": False,
			"PortForward": [],
		}],
		"Serial": [],
		"Sound": _sounds(options),
	}


def config_plist_bytes(config):
	return plistlib.dumps(config, fmt=plistlib.FMT_XML)


def write_bundle(path, options):
	"""Create ``<path>/<name>.utm`` with config.plist and an empty Data directory. Return the bundle path.

	The caller places the disk image and ISO in Data afterwards.
	"""
	config = build_config(options)
	bundle = os.path.join(path, config["Information"]["Name"] + BUNDLE_SUFFIX)
	config_path = os.path.join(bundle, CONFIG_FILE_NAME)
	if os.path.exists(config_path):
		raise UTMConfigError("{} already exists. Remove it first to write a new configuration".format(config_path))
	try:
		os.makedirs(os.path.join(bundle, DATA_DIRECTORY_NAME), exist_ok=True)
		with open(config_path, "wb") as config_file:
			config_file.write(config_plist_bytes(config))
	except OSError as error:
		raise UTMConfigError("cannot write UTM bundle {}: {}".format(bundle, error)) from None
	return bundle


def create_disk_image(path, size_gib, qemu_img_path=DEFAULT_QEMU_IMG_PATH):
	"""Create an empty qcow2 image of ``size_gib`` GiB. ``qemu_img_path`` is a path or a list of arguments."""
	command = [qemu_img_path] if isinstance(qemu_img_path, str) else list(qemu_img_path)
	command += ["create", "-f", "qcow2", path, "{}G".format(size_gib)]
	try:
		result = subprocess.run(command, capture_output=True, text=True)
	except OSError as error:
		raise UTMConfigError("cannot run qemu-img at {}: {}".format(command[0], error)) from None
	if result.returncode != 0:
		detail = result.stderr.strip() or result.stdout.strip() or "no output"
		raise UTMConfigError("qemu-img create failed with exit code {}: {}".format(result.returncode, detail))
