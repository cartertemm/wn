"""Build an SSDT with an ACPI battery (BAT0) and AC adapter (ACAD) whose state lives in guest memory.

The table reads a shared data block through a SystemMemory OperationRegion. Its base address is the
QWord ``PLACEHOLDER_ADDRESS``, which the wn-acpi UEFI driver replaces with the address it allocated.
The AML encoding follows the ACPI 6.5 specification, chapter 20.
"""

import struct
import sys

from wn.battery_sync import DESIGN_VOLTAGE_MV, FLAG_CHARGING, FLAG_DISCHARGING, FULL_CAPACITY_MWH

HEADER_LENGTH = 36
SIGNATURE = b"SSDT"
REVISION = 2
OEM_ID = b"WN    "
OEM_TABLE_ID = b"WNBATT  "
OEM_REVISION = 1
CREATOR_ID = b"WN  "
CREATOR_REVISION = 1
PLACEHOLDER_ADDRESS = 0x5151515151515151
REGION_LENGTH = 64

ZERO_OP = 0x00
ONE_OP = 0x01
NAME_OP = 0x08
BYTE_PREFIX = 0x0A
WORD_PREFIX = 0x0B
DWORD_PREFIX = 0x0C
STRING_PREFIX = 0x0D
QWORD_PREFIX = 0x0E
SCOPE_OP = 0x10
PACKAGE_OP = 0x12
METHOD_OP = 0x14
ROOT_CHAR = b"\\"
NULL_NAME = b"\x00"
LOCAL0_OP = 0x60
STORE_OP = 0x70
SHIFT_RIGHT_OP = 0x7A
AND_OP = 0x7B
OR_OP = 0x7D
INDEX_OP = 0x88
RETURN_OP = 0xA4
EXT_OP_PREFIX = 0x5B
OP_REGION_OP = 0x80
FIELD_OP = 0x81
DEVICE_OP = 0x82

SYSTEM_MEMORY = 0x00
DWORD_ACC_NO_LOCK_PRESERVE = 0x03
SERIALIZED_FLAG = 0x08
NAME_SEG_LENGTH = 4
DWORD_BITS = 32

PKG_LENGTH_ONE_BYTE_MAX = 0x3F
PKG_LENGTH_MAX_FOLLOW_BYTES = 3

BATTERY_STA = 0x1F
AC_STA = 0x0F
WARNING_CAPACITY_MWH = 500
LOW_CAPACITY_MWH = 200
GRANULARITY_MWH = 100
POWER_UNIT_MWH = 0
RECHARGEABLE = 1

CHARGING_SHIFT = 1
DISCHARGING_SHIFT = 3

DATA_FIELDS = ("MAGC", "VERS", "FLGS", "PCNT", "RATE", "REMN", "FULL", "DMV_")
BST_INDEX_STATE = 0
BST_INDEX_RATE = 1
BST_INDEX_REMAINING = 2
BST_INDEX_VOLTAGE = 3


def _encode_length(value, follow):
	if follow == 0:
		return bytes([value])
	encoded = [(follow << 6) | (value & 0x0F)]
	encoded += [(value >> (4 + 8 * position)) & 0xFF for position in range(follow)]
	return bytes(encoded)


def _follow_bytes(value):
	if value <= PKG_LENGTH_ONE_BYTE_MAX:
		return 0
	for follow in range(1, PKG_LENGTH_MAX_FOLLOW_BYTES + 1):
		if value < 1 << (4 + 8 * follow):
			return follow
	raise ValueError("length {} is too long for a PkgLength".format(value))


def pkg_length(content_length):
	"""Encode a PkgLength for ``content_length`` bytes that follow it. The value counts itself."""
	for follow in range(PKG_LENGTH_MAX_FOLLOW_BYTES + 1):
		total = content_length + follow + 1
		if _follow_bytes(total) == follow:
			return _encode_length(total, follow)
	raise ValueError("package of {} bytes is too long for a PkgLength".format(content_length))


def field_length(bits):
	"""Encode the width of a field unit. It uses the PkgLength form but does not count itself."""
	return _encode_length(bits, _follow_bytes(bits))


def name_seg(name):
	"""Return a 4 byte NameSeg, padded with underscores."""
	if not 1 <= len(name) <= NAME_SEG_LENGTH:
		raise ValueError("NameSeg '{}' must have 1 to 4 characters".format(name))
	return name.ljust(NAME_SEG_LENGTH, "_").encode("ascii")


def name_string(path):
	"""Encode a name such as ``BAT0`` or ``\\_SB`` with one segment."""
	if path.startswith("\\"):
		return ROOT_CHAR + name_seg(path[1:])
	return name_seg(path)


def eisa_id(text):
	"""Compress an EISA ID such as ``PNP0C0A`` into its 32 bit integer form."""
	vendor = 0
	for letter in text[:3]:
		vendor = (vendor << 5) | (ord(letter) - ord("@"))
	product = int(text[3:], 16)
	value = (vendor << 16) | product
	return struct.unpack("<I", struct.pack(">I", value))[0]


def integer(value):
	"""Encode an integer with the shortest constant form."""
	if value == 0:
		return bytes([ZERO_OP])
	if value == 1:
		return bytes([ONE_OP])
	for prefix, size, code in ((BYTE_PREFIX, 1, "<B"), (WORD_PREFIX, 2, "<H"), (DWORD_PREFIX, 4, "<I"), (QWORD_PREFIX, 8, "<Q")):
		if value < 1 << (8 * size):
			return bytes([prefix]) + struct.pack(code, value)
	raise ValueError("integer {} is wider than 64 bits".format(value))


def qword(value):
	return bytes([QWORD_PREFIX]) + struct.pack("<Q", value)


def string(text):
	return bytes([STRING_PREFIX]) + text.encode("ascii") + b"\x00"


def _with_pkg_length(opcode, body):
	return opcode + pkg_length(len(body)) + body


def scope(path, *terms):
	return _with_pkg_length(bytes([SCOPE_OP]), name_string(path) + b"".join(terms))


def device(name, *terms):
	return _with_pkg_length(bytes([EXT_OP_PREFIX, DEVICE_OP]), name_string(name) + b"".join(terms))


def name(path, value):
	return bytes([NAME_OP]) + name_string(path) + value


def method(path, argument_count, *terms):
	flags = argument_count | SERIALIZED_FLAG
	return _with_pkg_length(bytes([METHOD_OP]), name_string(path) + bytes([flags]) + b"".join(terms))


def package(*elements):
	return _with_pkg_length(bytes([PACKAGE_OP]), bytes([len(elements)]) + b"".join(elements))


def operation_region(path, space, offset, length):
	return bytes([EXT_OP_PREFIX, OP_REGION_OP]) + name_string(path) + bytes([space]) + offset + length


def field(region, flags, units):
	body = name_string(region) + bytes([flags])
	for unit_name, bits in units:
		body += name_seg(unit_name) + field_length(bits)
	return _with_pkg_length(bytes([EXT_OP_PREFIX, FIELD_OP]), body)


def return_(value):
	return bytes([RETURN_OP]) + value


def store(source, target):
	return bytes([STORE_OP]) + source + target


def and_(left, right):
	return bytes([AND_OP]) + left + right + NULL_NAME


def or_(left, right):
	return bytes([OR_OP]) + left + right + NULL_NAME


def shift_right(value, count):
	return bytes([SHIFT_RIGHT_OP]) + value + count + NULL_NAME


def index(source, position):
	return bytes([INDEX_OP]) + source + integer(position) + NULL_NAME


def _flag_bit(flag, shift):
	return shift_right(and_(name_string("FLGS"), integer(flag)), integer(shift))


def _battery():
	bif = package(
		integer(POWER_UNIT_MWH),
		integer(FULL_CAPACITY_MWH),
		integer(FULL_CAPACITY_MWH),
		integer(RECHARGEABLE),
		integer(DESIGN_VOLTAGE_MV),
		integer(WARNING_CAPACITY_MWH),
		integer(LOW_CAPACITY_MWH),
		integer(GRANULARITY_MWH),
		integer(GRANULARITY_MWH),
		string("wn"),
		string("1"),
		string("LION"),
		string("wn"),
	)
	state = or_(_flag_bit(FLAG_DISCHARGING, DISCHARGING_SHIFT), _flag_bit(FLAG_CHARGING, CHARGING_SHIFT))
	pbst = name_string("PBST")
	return device(
		"BAT0",
		name("_HID", integer(eisa_id("PNP0C0A"))),
		name("_UID", integer(1)),
		method("_STA", 0, return_(integer(BATTERY_STA))),
		name("_BIF", bif),
		name("PBST", package(integer(0), integer(0), integer(0), integer(0))),
		method(
			"_BST",
			0,
			store(state, index(pbst, BST_INDEX_STATE)),
			store(name_string("RATE"), index(pbst, BST_INDEX_RATE)),
			store(name_string("REMN"), index(pbst, BST_INDEX_REMAINING)),
			store(name_string("DMV"), index(pbst, BST_INDEX_VOLTAGE)),
			return_(pbst),
		),
		method("_BTP", 1, return_(integer(0))),
	)


def _ac_adapter():
	return device(
		"ACAD",
		name("_HID", string("ACPI0003")),
		method("_STA", 0, return_(integer(AC_STA))),
		method("_PSR", 0, return_(and_(shift_right(name_string("FLGS"), integer(1)), integer(1)))),
		name("_PCL", package(name_string("\\_SB"))),
	)


def _data_block():
	return (
		operation_region("WNBT", SYSTEM_MEMORY, qword(PLACEHOLDER_ADDRESS), integer(REGION_LENGTH))
		+ field("WNBT", DWORD_ACC_NO_LOCK_PRESERVE, [(unit, DWORD_BITS) for unit in DATA_FIELDS])
	)


def _header(body_length):
	return struct.pack(
		"<4sIBB6s8sI4sI",
		SIGNATURE,
		HEADER_LENGTH + body_length,
		REVISION,
		0,
		OEM_ID,
		OEM_TABLE_ID,
		OEM_REVISION,
		CREATOR_ID,
		CREATOR_REVISION,
	)


def build_ssdt():
	"""Return the complete SSDT as bytes, with a valid checksum."""
	body = scope("\\_SB", _data_block(), _battery(), _ac_adapter())
	table = bytearray(_header(len(body)) + body)
	table[9] = (-sum(table)) & 0xFF
	return bytes(table)


def main(argv):
	if len(argv) != 2:
		sys.stderr.write("usage: python3 -m wn.acpi_battery OUTPUT.aml\n")
		return 2
	with open(argv[1], "wb") as output:
		output.write(build_ssdt())
	return 0


if __name__ == "__main__":
	sys.exit(main(sys.argv))
