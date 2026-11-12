"""Write a UTM 5 ``.utm`` bundle (config.plist version 4) for a Windows 11 ARM64 QEMU VM."""

import os
import plistlib
import random
import shlex
import subprocess
import uuid

from wn.errors import WnError

CONFIGURATION_VERSION = 4
BUNDLE_SUFFIX = ".utm"
CONFIG_FILE_NAME = "config.plist"
DATA_DIRECTORY_NAME = "Data"
DEFAULT_BUNDLE_DIRECTORY = os.path.join("~", "Library", "Containers", "com.utmapp.UTM", "Data", "Documents")
BATTERY_FILE_NAME = "wn-battery.bin"
BATTERY_FILE_SIZE_MIB = 1
BATTERY_MEMDEV_ID = "wnbattery"
BATTERY_DEVICE_ID = "wnbatterynv"
BATTERY_FILE_FORBIDDEN = (",", " ", "\t", "\n")
DEFAULT_QEMU_IMG_PATH = "/opt/homebrew/bin/qemu-img"
QMP_HOST = "127.0.0.1"
AUDIODEV_ID = "ca0"
AUDIO_SPICE = "spice"
AUDIO_COREAUDIO = "coreaudio"
AUDIO_MODES = (AUDIO_SPICE, AUDIO_COREAUDIO)
MAXIMUM_USB_SHARE = 3
INTERFACE_VERSION = 1
QMP_SESSION_PORT_OFFSET = 1
QEMU_MAC_PREFIX = "52:54:00"
MAC_SUFFIX_OCTET_COUNT = 3
OCTET_VALUES = 256

REQUIRED_OPTIONS = ("name", "mac_address")
DEFAULT_OPTIONS = {
	"cpu_count": 8,
	"memory_mib": 8192,
	"disk_image": "disk.qcow2",
	"iso_image": None,
	"driver_iso_image": None,
	"display": "virtio-ramfb",
	"machine_properties": "kernel-irqchip=off",
	"headless": False,
	"qmp_port": 4444,
	"qmp_session_port": None,
	"audio": AUDIO_SPICE,
	"audio_buffer_length_us": 20000,
	"audio_args": None,
	"extra_args": None,
	"battery": False,
	"battery_file": None,
	"tpm": True,
	"uefi": True,
}


class UTMConfigError(WnError):
	"""Raised when VM options are invalid or the bundle or disk image cannot be created."""


def random_mac_address():
	suffix = ["{:02x}".format(random.randrange(OCTET_VALUES)) for _ in range(MAC_SUFFIX_OCTET_COUNT)]
	return ":".join([QEMU_MAC_PREFIX] + suffix)


def _new_uuid():
	return str(uuid.uuid4()).upper()


def session_port(qmp_port, qmp_session_port=None):
	"""Return the QMP port for long-lived clients: ``qmp_session_port``, or the port after ``qmp_port``."""
	return qmp_port + QMP_SESSION_PORT_OFFSET if qmp_session_port is None else qmp_session_port


def _resolve_argument_options(options):
	unknown = sorted(set(options) - set(REQUIRED_OPTIONS) - set(DEFAULT_OPTIONS))
	if unknown:
		raise UTMConfigError("unknown VM options: {}. Known options: {}".format(", ".join(unknown), ", ".join(REQUIRED_OPTIONS + tuple(DEFAULT_OPTIONS))))
	resolved = dict(DEFAULT_OPTIONS, **options)
	if resolved["audio"] not in AUDIO_MODES:
		raise UTMConfigError("audio mode '{}' is not supported. Use one of: {}".format(resolved["audio"], ", ".join(AUDIO_MODES)))
	if resolved["audio_args"]:
		if resolved["audio"] != AUDIO_COREAUDIO:
			raise UTMConfigError("audio_args needs audio mode '{}'".format(AUDIO_COREAUDIO))
		try:
			shlex.split(resolved["audio_args"])
		except ValueError as error:
			raise UTMConfigError("cannot parse audio_args: {}".format(error)) from None
	if resolved["extra_args"]:
		try:
			shlex.split(resolved["extra_args"])
		except ValueError as error:
			raise UTMConfigError("cannot parse extra_args: {}".format(error)) from None
	if resolved["battery"]:
		if not resolved["battery_file"]:
			raise UTMConfigError("the battery option needs battery_file")
		if any(character in resolved["battery_file"] for character in BATTERY_FILE_FORBIDDEN):
			raise UTMConfigError("battery_file must not contain spaces or commas: {}".format(resolved["battery_file"]))
	resolved["qmp_session_port"] = session_port(resolved["qmp_port"], resolved["qmp_session_port"])
	if resolved["qmp_session_port"] == resolved["qmp_port"]:
		raise UTMConfigError("qmp_session_port must differ from qmp_port {}".format(resolved["qmp_port"]))
	return resolved


def _resolve_options(options):
	missing = [key for key in REQUIRED_OPTIONS if not options.get(key)]
	if missing:
		raise UTMConfigError("missing VM options: {}".format(", ".join(missing)))
	return _resolve_argument_options(options)


def _qmp_server(port):
	return ["-qmp", "tcp:{}:{},server=on,wait=off".format(QMP_HOST, port)]


def battery_file_in_bundle(bundle_path):
	return os.path.join(bundle_path, DATA_DIRECTORY_NAME, BATTERY_FILE_NAME)


def _battery_arguments(options):
	"""Map the battery file into the guest as an NVDIMM. The second -machine and -m merge with UTM's own."""
	return [
		"-machine", "nvdimm=on",
		"-m", "maxmem={}M,slots=1".format(options["memory_mib"] + BATTERY_FILE_SIZE_MIB),
		"-object", "memory-backend-file,id={},share=on,mem-path={},size={}M".format(BATTERY_MEMDEV_ID, options["battery_file"], BATTERY_FILE_SIZE_MIB),
		"-device", "nvdimm,id={},memdev={}".format(BATTERY_DEVICE_ID, BATTERY_MEMDEV_ID),
	]


def _additional_arguments(options):
	arguments = _qmp_server(options["qmp_port"]) + _qmp_server(options["qmp_session_port"])
	if options["headless"]:
		arguments += ["-device", "ramfb"]
	if options["audio_args"]:
		arguments += shlex.split(options["audio_args"])
	elif options["audio"] == AUDIO_COREAUDIO:
		arguments += [
			"-audiodev", "coreaudio,id={},out.buffer-length={}".format(AUDIODEV_ID, options["audio_buffer_length_us"]),
			"-device", "intel-hda",
			"-device", "hda-output,audiodev={}".format(AUDIODEV_ID),
		]
	if options["battery"]:
		arguments += _battery_arguments(options)
	if options["extra_args"]:
		arguments += shlex.split(options["extra_args"])
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
		"Hardware": options["display"],
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
	bundle = os.path.join(path, options.get("name", "") + BUNDLE_SUFFIX)
	if options.get("battery") and not options.get("battery_file"):
		options = dict(options, battery_file=battery_file_in_bundle(bundle))
	config = build_config(options)
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


def _edit_config(bundle_path, edit):
	"""Load the bundle's config.plist, pass it to ``edit``, and write it back atomically."""
	config_path = os.path.join(bundle_path, CONFIG_FILE_NAME)
	try:
		with open(config_path, "rb") as config_file:
			config = plistlib.load(config_file)
		edit(config)
		temporary_path = config_path + ".tmp"
		with open(temporary_path, "wb") as config_file:
			config_file.write(config_plist_bytes(config))
		os.replace(temporary_path, config_path)
	except OSError as error:
		raise UTMConfigError("cannot update {}: {}".format(config_path, error)) from None
	except (plistlib.InvalidFileException, KeyError, TypeError):
		raise UTMConfigError("{} is not a UTM QEMU configuration".format(config_path)) from None


def update_additional_arguments(bundle_path, options):
	"""Replace the AdditionalArguments of an existing bundle's config.plist and return the new list.

	``options`` takes the same argument options as ``build_config``, such as
	``qmp_port``, ``qmp_session_port``, ``headless``, ``audio``, ``audio_args``, ``battery``, ``battery_file``, and
	``extra_args``. With ``battery``, the memory size comes from the bundle, and ``battery_file`` defaults to
	Data/wn-battery.bin in the bundle. Nothing else in config.plist changes.
	"""
	if options.get("battery") and not options.get("battery_file"):
		options = dict(options, battery_file=battery_file_in_bundle(bundle_path))
	resolved = _resolve_argument_options(options)
	arguments = []

	def replace_arguments(config):
		if resolved["battery"]:
			resolved["memory_mib"] = config["System"]["MemorySize"]
		arguments.extend(_additional_arguments(resolved))
		config["QEMU"]["AdditionalArguments"] = arguments

	_edit_config(bundle_path, replace_arguments)
	return arguments


def set_display_hardware(bundle_path, hardware):
	"""Set the emulated display device of every display in an existing bundle, such as ``virtio-gpu-pci``."""

	def replace_hardware(config):
		for display in config["Display"]:
			display["Hardware"] = hardware

	_edit_config(bundle_path, replace_hardware)


def detach_drives(bundle_path, image_names):
	"""Remove the drives that use ``image_names`` from an existing bundle and delete those image files."""

	def remove_drives(config):
		config["Drive"] = [drive for drive in config["Drive"] if drive.get("ImageName") not in image_names]

	_edit_config(bundle_path, remove_drives)
	for image_name in image_names:
		image_path = os.path.join(bundle_path, DATA_DIRECTORY_NAME, image_name)
		if os.path.exists(image_path):
			os.remove(image_path)


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
