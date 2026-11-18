"""Share data: the Share record, name and path checks, overlap checks, port choice, and config validation."""

import dataclasses
import os
import re
from typing import Optional

from wn.errors import WnError

SHARE_COMMAND = "share"
LIST_COMMAND = "shares"
REMOVE_COMMAND = "unshare"
HOST_ADDRESS = "192.168.64.1"
FIRST_PORT = 47321
LAST_PORT = 47420
NAME_MAX_LENGTH = 32
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{{0,{}}}$".format(NAME_MAX_LENGTH - 1))
NAME_INVALID_RUN = re.compile(r"[^A-Za-z0-9._-]+")
NAME_LEADING = ".-"
NAME_RULE = "name must be 1 to {} characters from A-Z, a-z, 0-9, '.', '_' and '-', and must not start with '.' or '-'".format(NAME_MAX_LENGTH)
DEFAULT_NAME = "share"
MAC_TO_VM = "mac_to_vm"
VM_TO_MAC = "vm_to_mac"
SUPPORTED_DIRECTIONS = (MAC_TO_VM,)
SHARE_KEYS = ("name", "vm", "direction", "mac_path", "vm_path", "read_only", "port")
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:\\")
WINDOWS_SEPARATOR = "\\"
MAC_SEPARATOR = "/"
RELATIVE_PARTS = (".", "..")
VM_PATH_EXAMPLE = "C:\\Users\\you\\Mac"
REFUSED_VM_FOLDERS = ("C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)", "C:\\ProgramData")
SUPPORT_DIRECTORY = os.path.join("~", "Library", "Application Support", "wn")
CONFIG_DIRECTORY = os.path.join("~", ".config", "wn")
WN_DIRECTORIES = (SUPPORT_DIRECTORY, CONFIG_DIRECTORY)
GUEST_ROOT = "C:\\ProgramData\\wn"


class ShareError(WnError):
	"""Raised when a share or a share command has a bad value."""


@dataclasses.dataclass(frozen=True)
class Share:
	name: str
	vm: str
	direction: str
	mac_path: str
	vm_path: str
	read_only: bool
	port: Optional[int] = None

	def to_dict(self):
		return {key: getattr(self, key) for key in SHARE_KEYS if not (key == "port" and self.port is None)}


def _is_inside(path, folder, separator):
	return path == folder or path.startswith(folder + separator)


def _overlaps(first, second, separator):
	first = first.casefold()
	second = second.casefold()
	return _is_inside(first, second, separator) or _is_inside(second, first, separator)


def _real_folder(path):
	return os.path.realpath(os.path.expanduser(path))


def shares_for_vm(shares, vm_name):
	return tuple(share for share in shares if share.vm == vm_name and share.direction == MAC_TO_VM)


def _is_port(value):
	return isinstance(value, int) and not isinstance(value, bool) and FIRST_PORT <= value <= LAST_PORT


def _item_problem(item, names, ports):
	if not isinstance(item, dict):
		return "each share must be an object"
	missing = [key for key in SHARE_KEYS if key not in item]
	if missing:
		return "missing keys: {}".format(", ".join(missing))
	unknown = sorted(key for key in item if key not in SHARE_KEYS)
	if unknown:
		return "unknown keys: {}".format(", ".join(unknown))
	name = item["name"]
	if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
		return NAME_RULE
	if name.casefold() in names:
		return "share name '{}' is used twice (names ignore case)".format(name)
	names.add(name.casefold())
	if not isinstance(item["vm"], str):
		return "vm must be a string"
	if item["direction"] not in SUPPORTED_DIRECTIONS:
		return "direction must be {}".format(" or ".join(SUPPORTED_DIRECTIONS))
	if not isinstance(item["mac_path"], str) or not os.path.isabs(item["mac_path"]):
		return "mac_path must be an absolute Mac path"
	if not isinstance(item["vm_path"], str) or not WINDOWS_ABSOLUTE.match(item["vm_path"]):
		return "vm_path must be an absolute Windows path such as '{}'".format(VM_PATH_EXAMPLE)
	if not isinstance(item["read_only"], bool):
		return "read_only must be true or false"
	port = item["port"]
	if not _is_port(port):
		return "port must be a whole number from {} to {}".format(FIRST_PORT, LAST_PORT)
	if port in ports:
		return "port {} is used twice".format(port)
	ports.add(port)
	return None


def validate_shares(path, items):
	names = set()
	ports = set()
	for index, item in enumerate(items):
		problem = _item_problem(item, names, ports)
		if problem:
			raise ShareError("{}: shares[{}]: {}".format(path, index, problem))


def shares_from_values(items):
	return tuple(Share(**item) for item in items)


def default_share_name(mac_path):
	name = NAME_INVALID_RUN.sub("-", os.path.basename(mac_path.rstrip(MAC_SEPARATOR)))
	return name.lstrip(NAME_LEADING)[:NAME_MAX_LENGTH] or DEFAULT_NAME


def check_share_name(name):
	if not NAME_PATTERN.fullmatch(name):
		raise ShareError("share {}. Pick another with --name".format(NAME_RULE))


def normalize_vm_path(text):
	path = text.replace(MAC_SEPARATOR, WINDOWS_SEPARATOR).rstrip(WINDOWS_SEPARATOR)
	parts = path.split(WINDOWS_SEPARATOR)
	if not WINDOWS_ABSOLUTE.match(path) or any(part in RELATIVE_PARTS for part in parts):
		raise ShareError("'{}' is not an absolute Windows folder. Give an absolute Windows folder such as '{}'".format(text, VM_PATH_EXAMPLE))
	return path


def check_vm_path(vm_path):
	for folder in REFUSED_VM_FOLDERS:
		if _is_inside(vm_path.casefold(), folder.casefold(), WINDOWS_SEPARATOR):
			raise ShareError("wn does not share into {} or folders inside it. Pick another Windows folder".format(folder))


def check_mac_path(path, read_only):
	real = _real_folder(path)
	if not os.path.isdir(real):
		raise ShareError("{} is not a folder on the Mac".format(path))
	if real == MAC_SEPARATOR:
		raise ShareError("wn does not share the Mac root folder /. Pick a folder inside it")
	support = _real_folder(SUPPORT_DIRECTORY)
	if _is_inside(real, support, MAC_SEPARATOR):
		raise ShareError("{} is inside wn's own folder {}. Pick another folder".format(path, support))
	if not read_only:
		for folder in map(_real_folder, WN_DIRECTORIES):
			if _is_inside(folder, real, MAC_SEPARATOR):
				raise ShareError("{} contains wn's own folder {}, so Windows could change wn's settings or read its keys. Use --read-only or pick another folder".format(path, folder))
	return real


def check_new_share(share, existing):
	for other in existing:
		if share.name.casefold() == other.name.casefold():
			raise ShareError("a share named '{}' already exists. Pick another name with --name".format(other.name))
		if _overlaps(share.mac_path, other.mac_path, MAC_SEPARATOR):
			raise ShareError("{} overlaps {}, the Mac folder of share '{}'. Shares must not contain each other".format(share.mac_path, other.mac_path, other.name))
		if share.vm == other.vm and _overlaps(share.vm_path, other.vm_path, WINDOWS_SEPARATOR):
			raise ShareError("{} overlaps {}, the Windows folder of share '{}'. Shares must not contain each other".format(share.vm_path, other.vm_path, other.name))


def pick_port(shares, can_bind):
	used = {share.port for share in shares}
	for port in range(FIRST_PORT, LAST_PORT + 1):
		if port not in used and can_bind(port):
			return port
	raise ShareError("no free port from {} to {} on {}. Remove a share or stop the program that uses these ports".format(FIRST_PORT, LAST_PORT, HOST_ADDRESS))
