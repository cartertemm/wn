import os
import tempfile
import unittest
from unittest import mock

from wn.battery_file import FILE_SIZE, BatteryFile, battery_file_path, battery_writer, prepare_battery_file
from wn.battery_sync import BLOCK_SIZE, BatteryBlockError, BatterySync, battery_reading, decode_block, encode_block, sync_message
from wn.config import Config

UPDATED_UNIX = 1789000000
UPDATED_OFFSET = 32
DISCHARGING_STATUS = {"present": True, "percent": 83, "charging": False, "on_ac": False, "minutes_remaining": 252}
CHARGING_STATUS = {"present": True, "percent": 55, "charging": True, "on_ac": True, "minutes_remaining": 83}
GUEST_BYTES = b"guest owns the rest"
GUEST_OFFSET = 0x38


class BatteryFileTests(unittest.TestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.directory = directory.name
		self.path = os.path.join(self.directory, "wn-battery.bin")
		self.battery_file = BatteryFile(self.path, clock=lambda: UPDATED_UNIX)

	def read_file(self):
		with open(self.path, "rb") as data_file:
			return data_file.read()

	def test_write_creates_a_one_mib_file_with_the_block_first(self):
		self.assertEqual(self.battery_file.write(battery_reading(DISCHARGING_STATUS)), self.path)
		data = self.read_file()
		self.assertEqual(len(data), FILE_SIZE)
		self.assertEqual(FILE_SIZE, 1 << 20)
		self.assertEqual(data[:BLOCK_SIZE], encode_block(battery_reading(DISCHARGING_STATUS), UPDATED_UNIX))
		self.assertEqual(data[BLOCK_SIZE:], bytes(FILE_SIZE - BLOCK_SIZE))

	def test_write_updates_in_place_and_keeps_other_bytes(self):
		self.battery_file.write(battery_reading(DISCHARGING_STATUS))
		with open(self.path, "r+b") as data_file:
			data_file.seek(GUEST_OFFSET)
			data_file.write(GUEST_BYTES)
		inode = os.stat(self.path).st_ino
		self.battery_file.write(battery_reading(CHARGING_STATUS))
		data = self.read_file()
		self.assertEqual(os.stat(self.path).st_ino, inode)
		self.assertEqual(decode_block(data)["percent"], 55)
		self.assertEqual(data[GUEST_OFFSET:GUEST_OFFSET + len(GUEST_BYTES)], GUEST_BYTES)

	def test_write_never_shrinks_a_larger_file(self):
		with open(self.path, "wb") as data_file:
			data_file.truncate(2 * FILE_SIZE)
		self.battery_file.write(battery_reading(DISCHARGING_STATUS))
		self.assertEqual(os.path.getsize(self.path), 2 * FILE_SIZE)

	def test_write_grows_a_short_file(self):
		with open(self.path, "wb") as data_file:
			data_file.write(b"x")
		self.battery_file.write(battery_reading(DISCHARGING_STATUS))
		self.assertEqual(os.path.getsize(self.path), FILE_SIZE)

	def test_updated_unix_is_written_last(self):
		real_pwrite = os.pwrite
		offsets = []

		def recording_pwrite(descriptor, data, offset):
			offsets.append((offset, len(data)))
			return real_pwrite(descriptor, data, offset)

		with mock.patch("wn.battery_file.os.pwrite", side_effect=recording_pwrite):
			self.battery_file.write(battery_reading(DISCHARGING_STATUS))
		self.assertEqual(offsets, [(0, UPDATED_OFFSET), (UPDATED_OFFSET, BLOCK_SIZE - UPDATED_OFFSET)])

	def test_read_back(self):
		self.battery_file.write(battery_reading(DISCHARGING_STATUS))
		location, block = self.battery_file.read()
		self.assertEqual((location, block["percent"], block["discharging"], block["updated_unix"]), (self.path, 83, True, UPDATED_UNIX))

	def test_read_without_magic(self):
		with open(self.path, "wb") as data_file:
			data_file.truncate(FILE_SIZE)
		self.assertEqual(self.battery_file.read(), (None, None))

	def test_read_missing_file(self):
		with self.assertRaises(BatteryBlockError) as caught:
			self.battery_file.read()
		self.assertIn(self.path, str(caught.exception))

	def test_write_into_a_missing_directory(self):
		missing = BatteryFile(os.path.join(self.directory, "missing", "wn-battery.bin"))
		with self.assertRaises(BatteryBlockError) as caught:
			missing.write(battery_reading(DISCHARGING_STATUS))
		self.assertIn("cannot write the battery file", str(caught.exception))

	def test_prepare_writes_a_default_block_only_once(self):
		prepare_battery_file(self.path, clock=lambda: UPDATED_UNIX)
		block = decode_block(self.read_file())
		self.assertEqual((block["magic"], block["flags"], block["percent"], block["remaining_mwh"]), ("WNBT", 0b0011, 100, 10000))
		self.battery_file.write(battery_reading(DISCHARGING_STATUS))
		prepare_battery_file(self.path)
		self.assertEqual(decode_block(self.read_file())["percent"], 83)

	def test_message(self):
		self.assertEqual(sync_message(self.path), "battery file {} written".format(self.path))


class ChannelTests(unittest.TestCase):
	def test_default_path_is_in_the_vm_bundle(self):
		with mock.patch.dict(os.environ, {"HOME": "/home/me"}):
			path = battery_file_path(Config())
		self.assertEqual(path, "/home/me/Library/Containers/com.utmapp.UTM/Data/Documents/Windows.utm/Data/wn-battery.bin")

	def test_default_path_follows_vm_name(self):
		with mock.patch.dict(os.environ, {"HOME": "/home/me"}):
			path = battery_file_path(Config(vm_name="Win11"))
		self.assertEqual(path, "/home/me/Library/Containers/com.utmapp.UTM/Data/Documents/Win11.utm/Data/wn-battery.bin")

	def test_configured_path(self):
		self.assertEqual(battery_file_path(Config(battery_file="/tmp/b.bin")), "/tmp/b.bin")

	def test_file_channel_is_the_default(self):
		writer = battery_writer(Config(battery_file="/tmp/b.bin"), lambda: self.fail("the file channel needs no QMP"))
		self.assertIsInstance(writer, BatteryFile)
		self.assertEqual(writer.path, "/tmp/b.bin")

	def test_gdb_channel(self):
		writer = battery_writer(Config(battery_channel="gdb", gdb_port=4500), lambda: "tcp:127.0.0.1:4444")
		self.assertIsInstance(writer, BatterySync)
		self.assertEqual((writer.port, writer.qmp_address), (4500, "tcp:127.0.0.1:4444"))


if __name__ == "__main__":
	unittest.main()
