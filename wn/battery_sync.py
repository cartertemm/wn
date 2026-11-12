"""Encode the Mac's battery status into the data block that the firmware battery driver reads.

The block starts with the magic "WNBT". wn.battery_file writes it into the file that QEMU maps into the guest.
"""

import collections
import datetime
import struct
import time

from wn.errors import WnError

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
LOG_PREFIX = "battery sync: "
NO_BLOCK_MESSAGE = "no battery block (firmware driver not installed)"

BatteryReading = collections.namedtuple("BatteryReading", "flags percent rate_mw remaining_mwh")


class BatteryBlockError(WnError):
	"""Raised when the battery block cannot be read or written, or a value is out of range."""


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


def sync_message(path):
	return "battery file {} written".format(path)


def run_sync_loop(sync, read_status, log, wait, poll_seconds=POLL_SECONDS, monotonic=time.monotonic):
	"""Poll pmset every ``poll_seconds``. Write at once when the reading changes, and every 30 s otherwise.

	Any error is logged and the loop goes on, so a failed write can never stop the agent. ``wait`` sleeps
	for up to the given seconds, can return early to poll at once, and returns True when the loop must end.
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
		if wait(poll_seconds):
			return


def _describe_flags(flags):
	names = [name for flag, name in FLAG_NAMES if flags & flag]
	return "0x{:x} ({})".format(flags, ", ".join(names) or "none")


def _format_block(location, block):
	updated = datetime.datetime.fromtimestamp(block["updated_unix"], datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
	lines = [
		"Battery block at {}".format(location),
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
	output.write(sync_message(sync.write(battery_reading(status))) + "\n")


def run_battery_command(arguments, sync, output, read_status):
	"""Run ``wn battery``. ``sync`` is a wn.battery_file.BatteryFile."""
	if arguments.battery_command == "status":
		address, block = sync.read()
		if address is None:
			raise BatteryBlockError(NO_BLOCK_MESSAGE)
		output.write(_format_block(address, block))
	elif arguments.battery_command == "set":
		_write_or_fail(sync, _fake_status(arguments), output)
	else:
		_write_or_fail(sync, read_status(), output)
