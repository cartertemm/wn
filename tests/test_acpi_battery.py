import os
import struct
import subprocess
import sys
import tempfile
import unittest

from wn import acpi_battery
from wn.acpi_battery import (
	HEADER_LENGTH,
	PLACEHOLDER_ADDRESS,
	WMI_BLOCK_GUID,
	build_ssdt,
	eisa_id,
	field_length,
	integer,
	name_seg,
	pkg_length,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOPE_OP = 0x10
DEVICE_OP = b"\x5b\x82"
QWORD_PREFIX = 0x0E


def decode_pkg_length(data, offset):
	"""Return (package length, number of PkgLength bytes) at ``offset``."""
	lead = data[offset]
	follow = lead >> 6
	if follow == 0:
		return lead & 0x3F, 1
	value = lead & 0x0F
	for index in range(follow):
		value |= data[offset + 1 + index] << (4 + 8 * index)
	return value, follow + 1


class PkgLengthTests(unittest.TestCase):
	def test_one_byte(self):
		self.assertEqual(pkg_length(0), b"\x01")
		self.assertEqual(pkg_length(62), b"\x3f")

	def test_two_bytes(self):
		encoded = pkg_length(63)
		self.assertEqual(len(encoded), 2)
		self.assertEqual(decode_pkg_length(encoded, 0), (65, 2))

	def test_three_bytes(self):
		encoded = pkg_length(5000)
		self.assertEqual(len(encoded), 3)
		self.assertEqual(decode_pkg_length(encoded, 0), (5003, 3))

	def test_length_includes_itself(self):
		for size in (0, 1, 61, 62, 63, 64, 4000, 4093, 4094, 70000):
			encoded = pkg_length(size)
			self.assertEqual(decode_pkg_length(encoded, 0), (size + len(encoded), len(encoded)))


class EncodingTests(unittest.TestCase):
	def test_field_length_does_not_count_itself(self):
		self.assertEqual(field_length(32), b"\x20")
		self.assertEqual(decode_pkg_length(field_length(512), 0), (512, 2))

	def test_name_seg_pads(self):
		self.assertEqual(name_seg("_SB"), b"_SB_")
		self.assertEqual(name_seg("BAT0"), b"BAT0")

	def test_eisa_id(self):
		self.assertEqual(eisa_id("PNP0C0A"), 0x0A0CD041)
		self.assertEqual(eisa_id("PNP0C0A").to_bytes(4, "little"), b"\x41\xd0\x0c\x0a")

	def test_integers(self):
		self.assertEqual(integer(0), b"\x00")
		self.assertEqual(integer(1), b"\x01")
		self.assertEqual(integer(0x1F), b"\x0a\x1f")
		self.assertEqual(integer(10000), b"\x0b\x10\x27")
		self.assertEqual(integer(0x0A0CD041), b"\x0c\x41\xd0\x0c\x0a")
		self.assertEqual(integer(PLACEHOLDER_ADDRESS)[0], QWORD_PREFIX)


class SsdtTests(unittest.TestCase):
	def setUp(self):
		self.table = build_ssdt()

	def test_header(self):
		self.assertEqual(self.table[:4], b"SSDT")
		length, = struct.unpack_from("<I", self.table, 4)
		self.assertEqual(length, len(self.table))
		self.assertEqual(self.table[8], 2)
		self.assertEqual(self.table[10:16], b"WN    ")
		self.assertEqual(self.table[16:24], b"WNBATT  ")

	def test_checksum_is_zero_sum(self):
		self.assertEqual(sum(self.table) % 256, 0)

	def test_placeholder_appears_once_as_qword(self):
		placeholder = struct.pack("<Q", PLACEHOLDER_ADDRESS)
		self.assertEqual(self.table.count(placeholder), 1)
		self.assertEqual(self.table[self.table.index(placeholder) - 1], QWORD_PREFIX)

	def test_scope_spans_whole_body(self):
		self.assertEqual(self.table[HEADER_LENGTH], SCOPE_OP)
		length, size = decode_pkg_length(self.table, HEADER_LENGTH + 1)
		self.assertEqual(HEADER_LENGTH + 1 + length, len(self.table))
		self.assertEqual(self.table[HEADER_LENGTH + 1 + size:HEADER_LENGTH + 1 + size + 5], b"\\_SB_")

	def test_devices_fit_inside_scope(self):
		found = []
		offset = self.table.find(DEVICE_OP)
		while offset != -1:
			length, size = decode_pkg_length(self.table, offset + 2)
			found.append(self.table[offset + 2 + size:offset + 2 + size + 4])
			self.assertLessEqual(offset + 2 + length, len(self.table))
			offset = self.table.find(DEVICE_OP, offset + 2)
		self.assertEqual(found, [b"BAT0", b"ACAD", b"WNWM"])

	def test_hids(self):
		self.assertIn(b"\x08_HID\x0c\x41\xd0\x0c\x0a", self.table)
		self.assertIn(b"\x08_HID\x0dACPI0003\x00", self.table)

	def test_methods_present(self):
		for name in (b"_STA", b"_BST", b"_BTP", b"_PSR"):
			self.assertIn(name, self.table)
		self.assertIn(b"_BIF", self.table)
		self.assertIn(b"_PCL", self.table)

	def test_wmi_data_block(self):
		self.assertIn(b"\x08_HID\x0c\x41\xd0\x0c\x14", self.table)
		guid_entry = WMI_BLOCK_GUID.bytes_le + b"AA\x01\x00"
		self.assertIn(b"\x08_WDG\x11\x17\x0a\x14" + guid_entry, self.table)
		self.assertIn(b"\x14\x45\x06WQAA\x09", self.table)

	def test_wmi_query_notifies_on_change(self):
		self.assertIn(b"\xa0\x1e\x92\x93FLGSLFLG", self.table)
		self.assertIn(b"\x86ACAD\x0a\x80", self.table)
		self.assertIn(b"\x92\x93RATELRAT", self.table)
		self.assertIn(b"\x92\x93REMNLREM", self.table)
		self.assertIn(b"\xa0\x09\x60\x86BAT0\x0a\x80\xa4\x60", self.table)


class MainTests(unittest.TestCase):
	def test_writes_file(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "ssdt.aml")
			subprocess.run([sys.executable, "-m", "wn.acpi_battery", path], check=True, cwd=PROJECT_ROOT)
			with open(path, "rb") as table_file:
				self.assertEqual(table_file.read(), acpi_battery.build_ssdt())


if __name__ == "__main__":
	unittest.main()
