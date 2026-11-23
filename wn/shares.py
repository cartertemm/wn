"""Share data: the Share record, name and path checks, overlap checks, port choice, and config validation."""

import dataclasses
import os
import re
import string
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
NAME_TRIMMED = ".-"
NAME_RULE = "name must be 1 to {} characters from A-Z, a-z, 0-9, '.', '_' and '-', and must not start with '.' or '-'".format(NAME_MAX_LENGTH)
DEFAULT_NAME = "share"
DOT = "."
SPACE = " "
RESERVED_WINDOWS_NAMES = frozenset(("con", "prn", "aux", "nul") + tuple(prefix + digit for prefix in ("com", "lpt") for digit in string.digits))
MAC_TO_VM = "mac_to_vm"
SHARE_KEYS = ("name", "vm", "direction", "mac_path", "vm_path", "read_only", "port")
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:\\")
SHELL_STRIPPED_PATH = re.compile(r"^([A-Za-z]):[^\\/]")
SHELL_STRIPPED_MESSAGE = "'{path}' has no backslash after {drive}:. The Mac shell removes backslashes from a path that is not in quotes. Put the Windows path in single quotes, such as '{drive}:\\Users\\you\\Projects', or use forward slashes, such as {drive}:/Users/you/Projects"
WINDOWS_SEPARATOR = "\\"
MAC_SEPARATOR = "/"
VM_PART_INVALID_CHARACTER = re.compile(r'[<>:"|?*\x00-\x1f]')
VM_PART_SHORT_NAME = re.compile(r"~[0-9]")
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
		return dataclasses.asdict(self)


def _is_inside(path, folder, separator):
	return path == folder or path.startswith(folder + separator)


def _overlaps(first, second, separator):
	first = first.casefold()
	second = second.casefold()
	return _is_inside(first, second, separator) or _is_inside(second, first, separator)


def _contains_on_disk(folder, path):
	while not os.path.exists(path):
		path = os.path.dirname(path)
	ancestor = path
	while True:
		inner = os.path.join(folder, os.path.relpath(path, ancestor))
		if os.path.exists(inner) and os.path.samefile(inner, path):
			return True
		parent = os.path.dirname(ancestor)
		if parent == ancestor:
			return False
		ancestor = parent


def _mac_paths_overlap(first, second):
	if os.path.exists(first) and os.path.exists(second):
		first = os.path.realpath(first)
		second = os.path.realpath(second)
		return _contains_on_disk(first, second) or _contains_on_disk(second, first)
	return _overlaps(first, second, MAC_SEPARATOR)


def _real_folder(path):
	return os.path.realpath(os.path.expanduser(path))


def _is_mac_root(path):
	return not os.path.normpath(path).strip(MAC_SEPARATOR)


def _is_reserved_name(name):
	return name.split(DOT)[0].rstrip(SPACE).casefold() in RESERVED_WINDOWS_NAMES


def _name_problem(name):
	if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
		return NAME_RULE
	if name.endswith(DOT):
		return "name '{}' ends in a dot, which Windows drops from folder names".format(name)
	if _is_reserved_name(name):
		return "name '{}' is a reserved Windows device name".format(name)
	return None


def shares_for_vm(shares, vm_name):
	return tuple(share for share in shares if share.vm == vm_name)


def _is_port(value):
	return isinstance(value, int) and not isinstance(value, bool) and FIRST_PORT <= value <= LAST_PORT


def _vm_path_problem(vm_path):
	if not isinstance(vm_path, str) or not WINDOWS_ABSOLUTE.match(vm_path):
		return "vm_path must be an absolute Windows path such as '{}'".format(VM_PATH_EXAMPLE)
	try:
		normalized = normalize_vm_path(vm_path)
		if normalized != vm_path:
			return "vm_path '{}' must be written as '{}'".format(vm_path, normalized)
	except ShareError as error:
		return "vm_path {}".format(error)
	return None


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
	problem = _name_problem(name)
	if problem:
		return problem
	if name.casefold() in names:
		return "share name '{}' is used twice (names ignore case)".format(name)
	if not isinstance(item["vm"], str):
		return "vm must be a string"
	if item["direction"] != MAC_TO_VM:
		return "direction must be {}".format(MAC_TO_VM)
	if not isinstance(item["mac_path"], str) or not os.path.isabs(item["mac_path"]):
		return "mac_path must be an absolute Mac path"
	if _is_mac_root(item["mac_path"]):
		return "mac_path must not be the Mac root folder /"
	problem = _vm_path_problem(item["vm_path"])
	if problem:
		return problem
	if not isinstance(item["read_only"], bool):
		return "read_only must be true or false"
	port = item["port"]
	if not _is_port(port):
		return "port must be a whole number from {} to {}".format(FIRST_PORT, LAST_PORT)
	if port in ports:
		return "port {} is used twice".format(port)
	return None


def split_shares(path, items):
	"""Return the valid share items and a message for each invalid one. An invalid item does not take a name or a port from the items after it."""
	names = set()
	ports = set()
	valid = []
	problems = []
	for index, item in enumerate(items):
		problem = _item_problem(item, names, ports)
		if problem:
			problems.append("{}: shares[{}]: {}".format(path, index, problem))
		else:
			valid.append(item)
			names.add(item["name"].casefold())
			ports.add(item["port"])
	return valid, problems


def default_share_name(mac_path):
	name = NAME_INVALID_RUN.sub("-", os.path.basename(mac_path.rstrip(MAC_SEPARATOR)))
	name = name.lstrip(NAME_TRIMMED)[:NAME_MAX_LENGTH].rstrip(NAME_TRIMMED)
	if not name or _is_reserved_name(name):
		return DEFAULT_NAME
	return name


def check_share_name(name):
	problem = _name_problem(name)
	if problem:
		raise ShareError("share {}. Pick another with --name".format(problem))


def _vm_part_problem(part):
	if not part:
		return "has an empty folder name"
	character = VM_PART_INVALID_CHARACTER.search(part)
	if character:
		return "has the character {!r}, which Windows does not allow in folder names".format(character.group())
	if part.endswith((DOT, SPACE)):
		return "has the folder name '{}', which ends in a dot or a space".format(part)
	if _is_reserved_name(part):
		return "has the folder name '{}', which is a reserved Windows device name".format(part)
	if VM_PART_SHORT_NAME.search(part):
		return "has the folder name '{}', which looks like an 8.3 short name that can stand for another folder".format(part)
	return None


def normalize_vm_path(text):
	stripped = SHELL_STRIPPED_PATH.match(text)
	if stripped:
		raise ShareError(SHELL_STRIPPED_MESSAGE.format(path=text, drive=stripped.group(1)))
	path =text.replace(MAC_SEPARATOR, WINDOWS_SEPARATOR).rstrip(WINDOWS_SEPARATOR)
	parts = path.split(WINDOWS_SEPARATOR)
	if not WINDOWS_ABSOLUTE.match(path) or any(part and not part.strip(DOT) for part in parts):
		raise ShareError("'{}' is not an absolute Windows folder. Give an absolute Windows folder such as '{}'".format(text, VM_PATH_EXAMPLE))
	for part in parts[1:]:
		problem = _vm_part_problem(part)
		if problem:
			raise ShareError("'{}' {}. Give a Windows folder such as '{}'".format(text, problem, VM_PATH_EXAMPLE))
	folded = path.casefold().split(WINDOWS_SEPARATOR)
	for folder in REFUSED_VM_FOLDERS:
		folder_parts = folder.casefold().split(WINDOWS_SEPARATOR)
		if folded[:len(folder_parts)] == folder_parts:
			raise ShareError("wn does not share into {} or folders inside it. Pick another Windows folder".format(folder))
	return path


def check_mac_path(path, read_only):
	real = _real_folder(path)
	if not os.path.isdir(real):
		raise ShareError("{} is not a folder on the Mac".format(path))
	if real == MAC_SEPARATOR:
		raise ShareError("wn does not share the Mac root folder /. Pick a folder inside it")
	support = _real_folder(SUPPORT_DIRECTORY)
	if _contains_on_disk(support, real):
		raise ShareError("{} is inside wn's own folder {}. Pick another folder".format(path, support))
	if not read_only:
		for folder in map(_real_folder, WN_DIRECTORIES):
			if _contains_on_disk(real, folder):
				raise ShareError("{} contains wn's own folder {}, so Windows could change wn's settings or read its keys. Use --read-only or pick another folder".format(path, folder))
	return real


def check_new_share(share, existing):
	for other in existing:
		if share.name.casefold() == other.name.casefold():
			raise ShareError("a share named '{}' already exists. Pick another name with --name".format(other.name))
		if _mac_paths_overlap(share.mac_path, other.mac_path):
			raise ShareError("{} overlaps {}, the Mac folder of share '{}'. Shares must not contain each other".format(share.mac_path, other.mac_path, other.name))
		if share.vm == other.vm and _overlaps(share.vm_path, other.vm_path, WINDOWS_SEPARATOR):
			raise ShareError("{} overlaps {}, the Windows folder of share '{}'. Shares must not contain each other".format(share.vm_path, other.vm_path, other.name))


def pick_port(shares, can_bind):
	used = {share.port for share in shares}
	for port in range(FIRST_PORT, LAST_PORT + 1):
		if port not in used and can_bind(port):
			return port
	raise ShareError("no free port from {} to {} on {}. Remove a share or stop the program that uses these ports".format(FIRST_PORT, LAST_PORT, HOST_ADDRESS))
