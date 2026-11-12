"""Write the battery block into the file that QEMU maps into the guest as an NVDIMM.

QEMU maps the file with ``share=on``, so the guest sees each write at once. The firmware driver finds the
NVDIMM through the ACPI NFIT table and points the battery SSDT at it. See docs/research/18-nvdimm-channel.md.
"""

import os
import time

from wn.battery_sync import (
	BLOCK_MAGIC,
	BLOCK_SIZE,
	FLAG_AC_ONLINE,
	FLAG_PRESENT,
	FULL_CAPACITY_MWH,
	MAX_PERCENT,
	BatteryBlockError,
	BatteryReading,
	decode_block,
	encode_block,
)
from wn.utm_config import BATTERY_FILE_SIZE_MIB, default_battery_file

FILE_SIZE = BATTERY_FILE_SIZE_MIB << 20
UPDATED_OFFSET = 32
FILE_MODE = 0o644
DEFAULT_READING = BatteryReading(FLAG_PRESENT | FLAG_AC_ONLINE, MAX_PERCENT, 0, FULL_CAPACITY_MWH)


def battery_file_path(config):
	"""Return ``battery_file`` from the config, or wn-battery-<vm_name>.bin in the UTM group container."""
	return config.battery_file or default_battery_file(config.vm_name)


def battery_writer(config):
	return BatteryFile(battery_file_path(config))


class BatteryFile:
	"""Keep the block at offset 0 of the file. The file is changed in place and never shrinks.

	QEMU has the file mapped while the VM runs, so replacing or truncating it would break the mapping.
	The fields go first and ``updated_unix`` last, so a reader that sees a new timestamp sees the new fields.
	"""

	def __init__(self, path, clock=time.time):
		self.path = path
		self._clock = clock

	def write(self, reading):
		"""Write the reading and return the file path."""
		data = encode_block(reading, int(self._clock()))
		try:
			descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, FILE_MODE)
		except OSError as error:
			raise BatteryBlockError("cannot write the battery file {}: {}".format(self.path, error)) from None
		try:
			if os.fstat(descriptor).st_size < FILE_SIZE:
				os.ftruncate(descriptor, FILE_SIZE)
			os.pwrite(descriptor, data[:UPDATED_OFFSET], 0)
			os.pwrite(descriptor, data[UPDATED_OFFSET:], UPDATED_OFFSET)
		except OSError as error:
			raise BatteryBlockError("cannot write the battery file {}: {}".format(self.path, error)) from None
		finally:
			os.close(descriptor)
		return self.path

	def read(self):
		"""Return the path and the decoded block, or (None, None) if the file has no block yet."""
		try:
			with open(self.path, "rb") as data_file:
				data = data_file.read(BLOCK_SIZE)
		except OSError as error:
			raise BatteryBlockError("cannot read the battery file {}: {}".format(self.path, error)) from None
		if len(data) < BLOCK_SIZE or not data.startswith(BLOCK_MAGIC):
			return None, None
		return self.path, decode_block(data)


def prepare_battery_file(path, clock=time.time):
	"""Create the file with a full battery on AC power, so the guest has a valid block before the agent runs."""
	if not os.path.exists(path):
		BatteryFile(path, clock).write(DEFAULT_READING)
