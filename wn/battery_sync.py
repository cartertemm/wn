"""Write the Mac's battery status into a data block in guest memory, where the firmware battery driver reads it.

The block is one page that the guest firmware reserves and marks with the magic "WNBT". The host finds it
by probing a fixed list of guest physical addresses through QEMU's GDB stub.
"""

import collections
import datetime
import struct
import time

from wn.errors import WnError
from wn.gdbstub import GdbStubClient, GdbStubError
from wn.qmp import qmp_client_for

DEFAULT_GDB_PORT = 4446
BLOCK_BASE_ADDRESS = 0x50000000
CANDIDATE_SPACING = 0x100000
CANDIDATE_COUNT = 16
CANDIDATE_ADDRESSES = tuple(BLOCK_BASE_ADDRESS + index * CANDIDATE_SPACING for index in range(CANDIDATE_COUNT))
BLOCK_MAGIC = b"WNBT"
BLOCK_VERSION = 1
BLOCK_LAYOUT = struct.Struct("<4sIIIIIIIQ")
BLOCK_SIZE = BLOCK_LAYOUT.size
FLAG_PRESENT = 1 << 0
FLAG_AC_ONLINE = 1 << 1
FLAG_CHARGING = 1 << 2
FLAG_DISCHARGING = 1 << 3
FLAG_NAMES = ((FLAG_PRESENT, "present"), (FLAG_AC_ONLINE, "ac online"), (FLAG_CHARGING, "charging"), (FLAG_DISCHARGING, "discharging"))
FULL_CAPACITY_MWH = 10000
DESIGN_VOLTAGE_MV = 11400
MWH_PER_PERCENT = 100
MINUTES_PER_HOUR = 60
MAX_PERCENT = 100
POLL_SECONDS = 5
WRITE_INTERVAL_SECONDS = 30
RUNNING_STATUS = "running"
LOG_PREFIX = "battery sync: "
NO_BLOCK_MESSAGE = "no battery block (firmware driver not installed)"

BatteryReading = collections.namedtuple("BatteryReading", "flags percent rate_mw remaining_mwh")


class BatteryBlockError(WnError):
	"""Raised when the guest has no battery block or a write does not stick."""


class VmNotRunningError(WnError):
	"""Raised instead of opening the GDB stub, because detaching from it would resume a paused VM."""


def _rate_mw(status, remaining_mwh, charging, discharging):
	minutes = status.get("minutes_remaining")
	if not minutes:
		return 0
	if discharging:
		return remaining_mwh * MINUTES_PER_HOUR // minutes
	if charging:
		return (FULL_CAPACITY_MWH - remaining_mwh) * MINUTES_PER_HOUR // minutes
	return 0


def battery_reading(status):
	"""Turn the agent's pmset status into the values the block carries."""
	present = bool(status.get("present"))
	on_ac = bool(status.get("on_ac"))
	charging = present and bool(status.get("charging"))
	discharging = present and not on_ac and not charging
	percent = (status.get("percent") or 0) if present else 0
	remaining_mwh = percent * MWH_PER_PERCENT
	flags = 0
	for flag, is_set in ((FLAG_PRESENT, present), (FLAG_AC_ONLINE, on_ac), (FLAG_CHARGING, charging), (FLAG_DISCHARGING, discharging)):
		if is_set:
			flags |= flag
	return BatteryReading(flags, percent, _rate_mw(status, remaining_mwh, charging, discharging), remaining_mwh)


def encode_block(reading, updated_unix):
	return BLOCK_LAYOUT.pack(
		BLOCK_MAGIC,
		BLOCK_VERSION,
		reading.flags,
		reading.percent,
		reading.rate_mw,
		reading.remaining_mwh,
		FULL_CAPACITY_MWH,
		DESIGN_VOLTAGE_MV,
		updated_unix,
	)


def decode_block(data):
	magic, version, flags, percent, rate_mw, remaining_mwh, full_mwh, design_mv, updated_unix = BLOCK_LAYOUT.unpack(data[:BLOCK_SIZE])
	return {
		"magic": magic.decode("ascii", "replace"),
		"version": version,
		"flags": flags,
		"present": bool(flags & FLAG_PRESENT),
		"ac_online": bool(flags & FLAG_AC_ONLINE),
		"charging": bool(flags & FLAG_CHARGING),
		"discharging": bool(flags & FLAG_DISCHARGING),
		"percent": percent,
		"rate_mw": rate_mw,
		"remaining_mwh": remaining_mwh,
		"full_mwh": full_mwh,
		"design_mv": design_mv,
		"updated_unix": updated_unix,
	}


def sync_message(address):
	if address is None:
		return NO_BLOCK_MESSAGE
	return "battery block found at 0x{:x} and written".format(address)


def _has_magic(client, address):
	try:
		return client.read_memory(address, len(BLOCK_MAGIC)) == BLOCK_MAGIC
	except GdbStubError:
		return False


class BatterySync:
	"""Find the block in guest memory once, then keep writing to that address.

	Every session first asks QMP for the VM state, and goes on only while the VM runs. Detaching from
	the GDB stub always resumes the VM, so a session on a paused VM would unpause it.
	"""

	def __init__(self, port, qmp_address, client_factory=GdbStubClient, qmp_factory=qmp_client_for, clock=time.time):
		self.port = port
		self.qmp_address = qmp_address
		self.address = None
		self._client_factory = client_factory
		self._qmp_factory = qmp_factory
		self._clock = clock

	def _require_running(self):
		try:
			with self._qmp_factory(self.qmp_address) as qmp:
				status = qmp.execute("query-status")["status"]
		except (WnError, OSError) as error:
			raise VmNotRunningError("cannot check the VM state over QMP, sync skipped: {}".format(error)) from None
		if status != RUNNING_STATUS:
			raise VmNotRunningError("the VM is {}, sync skipped".format(status))

	def _session(self):
		self._require_running()
		return self._client_factory(self.port)

	def _locate(self, client):
		if self.address is not None and _has_magic(client, self.address):
			return self.address
		self.address = next((address for address in CANDIDATE_ADDRESSES if _has_magic(client, address)), None)
		return self.address

	def write(self, reading):
		"""Write the reading and read it back. Return the block address, or None if the guest has no block."""
		with self._session() as client:
			address = self._locate(client)
			if address is None:
				return None
			data = encode_block(reading, int(self._clock()))
			client.write_memory(address, data)
			if client.read_memory(address, len(data)) != data:
				raise BatteryBlockError("the battery block at 0x{:x} did not keep the written data".format(address))
			return address

	def read(self):
		"""Return the block address and its decoded fields, or (None, None) if the guest has no block."""
		with self._session() as client:
			address = self._locate(client)
			if address is None:
				return None, None
			return address, decode_block(client.read_memory(address, BLOCK_SIZE))


def run_sync_loop(sync, read_status, log, wait, monotonic=time.monotonic):
	"""Poll pmset every 5 s. Write at once when the reading changes, and every 30 s otherwise.

	Any error is logged and the loop goes on, so the GDB side can never stop the agent. ``wait`` sleeps
	for the given seconds and returns True when the loop must end.
	"""
	last_reading = None
	next_write_time = 0
	last_message = None
	while True:
		message = None
		try:
			reading = battery_reading(read_status())
			now = monotonic()
			if reading != last_reading or now >= next_write_time:
				last_reading = reading
				next_write_time = now + WRITE_INTERVAL_SECONDS
				message = sync_message(sync.write(reading))
		except Exception as error:
			message = str(error)
		if message is not None and message != last_message:
			log.write(LOG_PREFIX + message + "\n")
			log.flush()
			last_message = message
		if wait(POLL_SECONDS):
			return


def _describe_flags(flags):
	names = [name for flag, name in FLAG_NAMES if flags & flag]
	return "0x{:x} ({})".format(flags, ", ".join(names) or "none")


def _format_block(address, block):
	updated = datetime.datetime.fromtimestamp(block["updated_unix"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
	lines = [
		"Battery block at 0x{:x}".format(address),
		"magic: {}".format(block["magic"]),
		"version: {}".format(block["version"]),
		"flags: {}".format(_describe_flags(block["flags"])),
	]
	lines += ["{}: {}".format(name, block[name]) for name in ("percent", "rate_mw", "remaining_mwh", "full_mwh", "design_mv")]
	lines.append("updated_unix: {} ({})".format(block["updated_unix"], updated))
	return "\n".join(lines) + "\n"


def _fake_status(arguments):
	if not 0 <= arguments.percent <= MAX_PERCENT:
		raise BatteryBlockError("--percent must be 0 to 100")
	on_ac = arguments.charging or arguments.ac
	return {"present": True, "percent": arguments.percent, "charging": arguments.charging, "on_ac": on_ac, "minutes_remaining": None}


def _write_or_fail(sync, status, output):
	address = sync.write(battery_reading(status))
	if address is None:
		raise BatteryBlockError(NO_BLOCK_MESSAGE)
	output.write(sync_message(address) + "\n")


def run_battery_command(arguments, config, qmp_address, output, read_status):
	sync = BatterySync(config.gdb_port, qmp_address)
	if arguments.battery_command == "status":
		address, block = sync.read()
		if address is None:
			raise BatteryBlockError(NO_BLOCK_MESSAGE)
		output.write(_format_block(address, block))
	elif arguments.battery_command == "set":
		_write_or_fail(sync, _fake_status(arguments), output)
	else:
		_write_or_fail(sync, read_status(), output)
