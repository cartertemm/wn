"""Write a UTM 5 ``.utm`` bundle (config.plist version 4) for a Windows 11 ARM64 QEMU VM."""

import os
import plistlib
import random
import shlex
import uuid

from wn.efivars import EfiVarsError, make_vars_with_cert
from wn.errors import WnError

CONFIGURATION_VERSION = 4
BUNDLE_SUFFIX = ".utm"
CONFIG_FILE_NAME = "config.plist"
DATA_DIRECTORY_NAME = "Data"
EFI_VARS_FILE_NAME = "efi_vars.fd"
SECURE_VARS_TEMPLATE = "/Applications/UTM.app/Contents/Resources/qemu/edk2-arm-secure-vars.fd"
DEFAULT_BUNDLE_DIRECTORY = os.path.join("~", "Library", "Containers", "com.utmapp.UTM", "Data", "Documents")
UTM_GROUP_CONTAINER = os.path.join("~", "Library", "Group Containers", "WDNLXAD4W8.com.utmapp.UTM")
BATTERY_FILE_NAME = "wn-battery-{}.bin"
BATTERY_FILE_SIZE_MIB = 1
BATTERY_MEMDEV_ID = "wnbattery"
BATTERY_DEVICE_ID = "wnbatterynv"
BATTERY_FILE_FORBIDDEN = (",", " ", "\t", "\n")
BATTERY_MACHINE_ARGUMENTS = ("-machine", "nvdimm=on")
RAMFB_ARGUMENTS = ("-device", "ramfb")
AUDIO_DEVICES = ("intel-hda", "ich9-intel-hda", "hda-output", "hda-duplex", "hda-micro")
BLOCK_QMP = "qmp"
BLOCK_RAMFB = "ramfb"
BLOCK_AUDIO = "audio"
BLOCK_BATTERY = "battery"
BLOCK_EXTRA = "extra"
ARGUMENT_BLOCKS = (BLOCK_QMP, BLOCK_RAMFB, BLOCK_AUDIO, BLOCK_BATTERY, BLOCK_EXTRA)
QMP_HOST = "127.0.0.1"
DEFAULT_QMP_PORT = 4444
AUDIODEV_ID = "ca0"
SPICE_AUDIODEV_ID = "audio0"
DEFAULT_AUDIO_ARGUMENTS = (
	"-audiodev", "coreaudio,id={},timer-period=2500,out.buffer-length=5000,out.buffer-count=3,out.frequency=48000,out.fixed-settings=on".format(AUDIODEV_ID),
	"-device", "intel-hda",
	"-device", "hda-output,audiodev={}".format(AUDIODEV_ID),
	"-device", "hda-micro,audiodev={}".format(SPICE_AUDIODEV_ID),
)
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
	"qmp_port": DEFAULT_QMP_PORT,
	"qmp_session_port": None,
	"audio_args": None,
	"extra_args": None,
	"battery": False,
	"battery_file": None,
	"tpm": True,
	"uefi": True,
	"secure_boot_cert": None,
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
	resolved = dict(DEFAULT_OPTIONS, **options)
	if resolved["audio_args"]:
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
		if any(character in _qemu_battery_path(resolved["battery_file"]) for character in BATTERY_FILE_FORBIDDEN):
			raise UTMConfigError("battery_file must not contain spaces or commas: {}".format(resolved["battery_file"]))
	resolved["qmp_session_port"] = session_port(resolved["qmp_port"], resolved["qmp_session_port"])
	if resolved["qmp_session_port"] == resolved["qmp_port"]:
		raise UTMConfigError("qmp_session_port must differ from qmp_port {}".format(resolved["qmp_port"]))
	return resolved


def _resolve_options(options):
	missing = [key for key in REQUIRED_OPTIONS if not options.get(key)]
	if missing:
		raise UTMConfigError("missing VM options: {}".format(", ".join(missing)))
	resolved = _resolve_argument_options(options)
	if resolved["secure_boot_cert"] and not (resolved["tpm"] and resolved["uefi"]):
		raise UTMConfigError("secure_boot_cert needs tpm and uefi, because UTM turns on Secure Boot only for UEFI VMs with a TPM")
	return resolved


def _qmp_server(port):
	return ["-qmp", "tcp:{}:{},server=on,wait=off".format(QMP_HOST, port)]


def default_battery_file(vm_name):
	"""Return the VM's battery file in the UTM group container, the only folder UTM's sandboxed QEMU may open."""
	return os.path.join(os.path.expanduser(UTM_GROUP_CONTAINER), BATTERY_FILE_NAME.format(vm_name))


def battery_file_for_bundle(bundle_path):
	name = os.path.basename(os.path.normpath(bundle_path))
	if name.endswith(BUNDLE_SUFFIX):
		name = name[:-len(BUNDLE_SUFFIX)]
	return default_battery_file(name)


def _qemu_battery_path(battery_file):
	"""QEMU runs in the UTM group container, so a file there gets a bare name, which avoids the space in the path."""
	if os.path.dirname(battery_file) == os.path.expanduser(UTM_GROUP_CONTAINER):
		return os.path.basename(battery_file)
	return battery_file


def _battery_arguments(options):
	"""Map the battery file into the guest as an NVDIMM. The second -machine and -m merge with UTM's own."""
	return list(BATTERY_MACHINE_ARGUMENTS) + [
		"-m", "maxmem={}M,slots=1".format(options["memory_mib"] + BATTERY_FILE_SIZE_MIB),
		"-object", "memory-backend-file,id={},share=on,mem-path={},size={}M".format(BATTERY_MEMDEV_ID, _qemu_battery_path(options["battery_file"]), BATTERY_FILE_SIZE_MIB),
		"-device", "nvdimm,id={},memdev={}".format(BATTERY_DEVICE_ID, BATTERY_MEMDEV_ID),
	]


def _audio_arguments(options):
	if options["audio_args"]:
		return shlex.split(options["audio_args"])
	return list(DEFAULT_AUDIO_ARGUMENTS)


def _argument_blocks(options):
	"""Return the AdditionalArguments as ``{block: arguments}``, one entry for each name in ARGUMENT_BLOCKS."""
	return {
		BLOCK_QMP: _qmp_server(options["qmp_port"]) + _qmp_server(options["qmp_session_port"]),
		BLOCK_RAMFB: list(RAMFB_ARGUMENTS) if options["headless"] else [],
		BLOCK_AUDIO: _audio_arguments(options),
		BLOCK_BATTERY: _battery_arguments(options) if options["battery"] else [],
		BLOCK_EXTRA: shlex.split(options["extra_args"]) if options["extra_args"] else [],
	}


def _additional_arguments(options):
	blocks = _argument_blocks(options)
	return [argument for block in ARGUMENT_BLOCKS for argument in blocks[block]]


def argument_units(arguments):
	"""Split QEMU arguments into units of one option and the values that follow it, such as ``["-device", "ramfb"]``."""
	units = []
	for argument in arguments:
		if argument.startswith("-") or not units:
			units.append([argument])
		else:
			units[-1].append(argument)
	return units


def _device_name(value):
	return value.split(",", 1)[0]


def block_of(unit):
	"""Return the block that wn writes ``unit`` in. Arguments that wn does not write belong to BLOCK_EXTRA."""
	option, value = unit[0], ",".join(unit[1:])
	if option == "-qmp":
		return BLOCK_QMP
	if unit == list(RAMFB_ARGUMENTS):
		return BLOCK_RAMFB
	if option == "-audiodev" or (option == "-device" and _device_name(value) in AUDIO_DEVICES):
		return BLOCK_AUDIO
	if (option, value) == BATTERY_MACHINE_ARGUMENTS or (option == "-m" and value.startswith("maxmem=")):
		return BLOCK_BATTERY
	if option in ("-object", "-device") and ("id={}".format(BATTERY_MEMDEV_ID) in value.split(",") or "id={}".format(BATTERY_DEVICE_ID) in value.split(",")):
		return BLOCK_BATTERY
	return BLOCK_EXTRA


def merge_argument_blocks(current, replacements):
	"""Return ``current`` with the blocks named in ``replacements`` swapped for the new arguments, in place.

	Every other argument stays where it was. A replaced block that was missing goes before the first argument
	of a later block, in ARGUMENT_BLOCKS order, or at the end.
	"""
	segments = []
	for unit in argument_units(current):
		block = block_of(unit)
		if block not in replacements:
			segments.append((block, unit))
		elif block not in (name for name, _ in segments if name in replacements):
			segments.append((block, replacements[block]))
	for block, arguments in sorted(replacements.items(), key=lambda item: ARGUMENT_BLOCKS.index(item[0])):
		if any(name == block for name, _ in segments):
			continue
		later = ARGUMENT_BLOCKS[ARGUMENT_BLOCKS.index(block) + 1:]
		position = next((index for index, (name, _) in enumerate(segments) if name in later), len(segments))
		segments.insert(position, (block, arguments))
	return [argument for _, arguments in segments for argument in arguments]


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
		"Sound": [],
	}


def config_plist_bytes(config):
	return plistlib.dumps(config, fmt=plistlib.FMT_XML)


def write_bundle(path, options):
	"""Create ``<path>/<name>.utm`` with config.plist and an empty Data directory. Return the bundle path.

	With ``secure_boot_cert`` (a DER certificate file), Data also gets efi_vars.fd made from UTM's Secure Boot
	template with the certificate added to db. UTM copies its template only when efi_vars.fd is missing, so
	this file is the one the VM uses. The caller places the disk image and ISO in Data afterwards.
	"""
	bundle = os.path.join(path, options.get("name", "") + BUNDLE_SUFFIX)
	if options.get("battery") and not options.get("battery_file"):
		options = dict(options, battery_file=battery_file_for_bundle(bundle))
	config = build_config(options)
	config_path = os.path.join(bundle, CONFIG_FILE_NAME)
	if os.path.exists(config_path):
		raise UTMConfigError("{} already exists. Remove it first to write a new configuration".format(config_path))
	try:
		os.makedirs(os.path.join(bundle, DATA_DIRECTORY_NAME), exist_ok=True)
	except OSError as error:
		raise UTMConfigError("cannot write UTM bundle {}: {}".format(bundle, error)) from None
	if options.get("secure_boot_cert"):
		prepare_vars_file(bundle, options["secure_boot_cert"])
	try:
		with open(config_path, "wb") as config_file:
			config_file.write(config_plist_bytes(config))
	except OSError as error:
		raise UTMConfigError("cannot write UTM bundle {}: {}".format(bundle, error)) from None
	return bundle


def prepare_vars_file(bundle_path, cert_path):
	"""Write the bundle's Data/efi_vars.fd from UTM's Secure Boot template with ``cert_path`` enrolled in db."""
	vars_path = os.path.join(bundle_path, DATA_DIRECTORY_NAME, EFI_VARS_FILE_NAME)
	try:
		make_vars_with_cert(SECURE_VARS_TEMPLATE, cert_path, vars_path)
	except FileExistsError:
		raise UTMConfigError("{} already exists. Remove it first to write new UEFI variables".format(vars_path)) from None
	except (OSError, EfiVarsError) as error:
		raise UTMConfigError("cannot write {}: {}".format(vars_path, error)) from None


def _edit_config(bundle_path, edit):
	"""Load the bundle's config.plist, pass it to ``edit``, and write it back atomically."""
	config_path = os.path.join(bundle_path, CONFIG_FILE_NAME)
	try:
		with open(config_path, "rb") as config_file:
			config = plistlib.load(config_file)
		if edit(config) is False:
			return
		temporary_path = config_path + ".tmp"
		with open(temporary_path, "wb") as config_file:
			config_file.write(config_plist_bytes(config))
		os.replace(temporary_path, config_path)
	except OSError as error:
		raise UTMConfigError("cannot update {}: {}".format(config_path, error)) from None
	except (plistlib.InvalidFileException, KeyError, TypeError):
		raise UTMConfigError("{} is not a UTM QEMU configuration".format(config_path)) from None


def update_additional_arguments(bundle_path, options, blocks):
	"""Replace the ``blocks`` (names from ARGUMENT_BLOCKS) in an existing bundle's AdditionalArguments.

	``options`` takes the same argument options as ``build_config``, such as
	``qmp_port``, ``qmp_session_port``, ``headless``, ``audio_args``, ``battery``, ``battery_file``, and
	``extra_args``. Only the named blocks are built from them; every other argument stays as it is, and
	BLOCK_EXTRA means every argument that is in no other block. With ``battery``, the memory size comes from
	the bundle, and ``battery_file`` defaults to wn-battery-<name>.bin in the UTM group container. Nothing else
	in config.plist changes, and the file is not written when the arguments stay the same.
	Return the arguments before and after.
	"""
	unknown = sorted(set(blocks) - set(ARGUMENT_BLOCKS))
	if unknown:
		raise UTMConfigError("unknown argument blocks: {}".format(", ".join(unknown)))
	if options.get("battery") and not options.get("battery_file"):
		options = dict(options, battery_file=battery_file_for_bundle(bundle_path))
	resolved = _resolve_argument_options(options)
	result = {}

	def replace_arguments(config):
		if resolved["battery"]:
			resolved["memory_mib"] = config["System"]["MemorySize"]
		before = list(config["QEMU"].get("AdditionalArguments", []))
		new_blocks = _argument_blocks(resolved)
		after = merge_argument_blocks(before, {block: new_blocks[block] for block in blocks})
		result.update(before=before, after=after)
		config["QEMU"]["AdditionalArguments"] = after
		return before != after

	_edit_config(bundle_path, replace_arguments)
	return result["before"], result["after"]


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
