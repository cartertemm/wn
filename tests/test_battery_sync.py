import io
import json
import os
import socket
import tempfile
import unittest
from unittest import mock

from fake_gdb_server import FakeGdbServer
from fake_qmp_server import TCPFakeQMPServer, reply
from wn.battery_sync import (
	BLOCK_SIZE,
	CANDIDATE_ADDRESSES,
	BatterySync,
	VmNotRunningError,
	battery_reading,
	decode_block,
	encode_block,
	run_sync_loop,
	sync_message,
)
from wn.cli import main

UPDATED = "2026-11-11T12:00:00Z"
UPDATED_UNIX = 1789000000
BASE = 0x50000000
CANDIDATE_SPACING = 0x100000
REGION_SIZE = 0x1000
MAGIC = b"WNBT"
DISCHARGING_STATUS = {"present": True, "percent": 83, "charging": False, "on_ac": False, "minutes_remaining": 252, "updated": UPDATED}
CHARGING_STATUS = {"present": True, "percent": 55, "charging": True, "on_ac": True, "minutes_remaining": 83, "updated": UPDATED}
CHARGED_STATUS = {"present": True, "percent": 100, "charging": False, "on_ac": True, "minutes_remaining": None, "updated": UPDATED}
DESKTOP_STATUS = {"present": False, "percent": None, "charging": False, "on_ac": True, "minutes_remaining": None, "updated": UPDATED}
DISCHARGING_BLOCK = bytes.fromhex(
	"574e4254" "01000000" "09000000" "53000000" "b8070000" "6c200000" "10270000" "882c0000" "40f9a16a00000000"
)
CHARGING_BLOCK = bytes.fromhex(
	"574e4254" "01000000" "07000000" "37000000" "b50c0000" "7c150000" "10270000" "882c0000" "40f9a16a00000000"
)


def all_candidate_regions():
	return {address: REGION_SIZE for address in CANDIDATE_ADDRESSES}


def status_handler(status):
	return {"query-status": lambda request: [reply(request, {"running": status == "running", "status": status, "singlestep": False})]}


def qmp_server(status="running"):
	return TCPFakeQMPServer(status_handler(status))


def unused_port():
	with socket.socket() as probe:
		probe.bind(("127.0.0.1", 0))
		return probe.getsockname()[1]


class StubQMPClient:
	"""Answers query-status without a socket, for tests that open many sessions."""

	def __init__(self, address, timeout=None):
		self.address = address

	def __enter__(self):
		return self

	def __exit__(self, *exc_info):
		pass

	def execute(self, command, arguments=None):
		return {"running": True, "status": "running"}


class EncodeTests(unittest.TestCase):
	def encode(self, status):
		return encode_block(battery_reading(status), UPDATED_UNIX)

	def test_discharging_exact_bytes(self):
		self.assertEqual(self.encode(DISCHARGING_STATUS), DISCHARGING_BLOCK)

	def test_charging_exact_bytes(self):
		self.assertEqual(self.encode(CHARGING_STATUS), CHARGING_BLOCK)

	def test_block_is_40_bytes(self):
		self.assertEqual(BLOCK_SIZE, 40)

	def test_charged_has_ac_and_no_rate(self):
		reading = battery_reading(CHARGED_STATUS)
		self.assertEqual((reading.flags, reading.percent, reading.rate_mw, reading.remaining_mwh), (0b0011, 100, 0, 10000))

	def test_desktop_has_no_battery(self):
		reading = battery_reading(DESKTOP_STATUS)
		self.assertEqual((reading.flags, reading.percent, reading.rate_mw, reading.remaining_mwh), (0b0010, 0, 0, 0))

	def test_no_estimate_gives_no_rate(self):
		reading = battery_reading(dict(DISCHARGING_STATUS, minutes_remaining=None))
		self.assertEqual((reading.flags, reading.rate_mw), (0b1001, 0))

	def test_zero_minutes_gives_no_rate(self):
		self.assertEqual(battery_reading(dict(CHARGING_STATUS, minutes_remaining=0)).rate_mw, 0)

	def test_ac_attached_not_charging_is_neither(self):
		reading = battery_reading(dict(CHARGED_STATUS, percent=80))
		self.assertEqual(reading.flags, 0b0011)

	def test_decode(self):
		self.assertEqual(decode_block(DISCHARGING_BLOCK), {
			"magic": "WNBT",
			"version": 1,
			"flags": 9,
			"present": True,
			"ac_online": False,
			"charging": False,
			"discharging": True,
			"percent": 83,
			"rate_mw": 1976,
			"remaining_mwh": 8300,
			"full_mwh": 10000,
			"design_mv": 11400,
			"updated_unix": UPDATED_UNIX,
		})


class CandidateTests(unittest.TestCase):
	def test_sixteen_addresses_one_mib_apart(self):
		self.assertEqual(len(CANDIDATE_ADDRESSES), 16)
		self.assertEqual(CANDIDATE_ADDRESSES[0], BASE)
		self.assertEqual(CANDIDATE_ADDRESSES[-1], BASE + 15 * CANDIDATE_SPACING)


class BatterySyncTests(unittest.TestCase):
	def make_sync(self, server):
		return BatterySync(server.port, "tcp:127.0.0.1:1", qmp_factory=StubQMPClient, clock=lambda: UPDATED_UNIX)

	def test_writes_at_first_address(self):
		with FakeGdbServer({BASE: REGION_SIZE}) as server:
			server.write(BASE, MAGIC)
			address = self.make_sync(server).write(battery_reading(DISCHARGING_STATUS))
			self.assertEqual(address, BASE)
			self.assertEqual(server.read(BASE, BLOCK_SIZE), DISCHARGING_BLOCK)
			self.assertFalse(server.paused)

	def test_finds_magic_at_a_later_candidate(self):
		later = BASE + 3 * CANDIDATE_SPACING
		regions = all_candidate_regions()
		del regions[BASE]
		with FakeGdbServer(regions) as server:
			server.write(later, MAGIC)
			address = self.make_sync(server).write(battery_reading(CHARGING_STATUS))
			self.assertEqual(address, later)
			self.assertEqual(server.read(later, BLOCK_SIZE), CHARGING_BLOCK)
			self.assertEqual(server.read(BASE + CANDIDATE_SPACING, BLOCK_SIZE), bytes(BLOCK_SIZE))

	def test_no_magic_writes_nothing(self):
		with FakeGdbServer(all_candidate_regions()) as server:
			self.assertIsNone(self.make_sync(server).write(battery_reading(CHARGING_STATUS)))
			for address in CANDIDATE_ADDRESSES:
				self.assertEqual(server.read(address, BLOCK_SIZE), bytes(BLOCK_SIZE))
		self.assertFalse([packet for packet in server.packets if packet.startswith("M")])

	def test_keeps_the_found_address(self):
		later = BASE + 2 * CANDIDATE_SPACING
		with FakeGdbServer(all_candidate_regions()) as server:
			server.write(later, MAGIC)
			sync = self.make_sync(server)
			sync.write(battery_reading(CHARGING_STATUS))
			server.packets.clear()
			sync.write(battery_reading(DISCHARGING_STATUS))
		reads = [packet for packet in server.packets if packet.startswith("m")]
		self.assertEqual(reads, ["m{:x},4".format(later), "m{:x},28".format(later)])

	def test_finds_the_block_again_when_it_moves(self):
		first = BASE + CANDIDATE_SPACING
		second = BASE + 5 * CANDIDATE_SPACING
		with FakeGdbServer(all_candidate_regions()) as server:
			server.write(first, MAGIC)
			sync = self.make_sync(server)
			sync.write(battery_reading(CHARGING_STATUS))
			server.write(first, bytes(BLOCK_SIZE))
			server.write(second, MAGIC)
			self.assertEqual(sync.write(battery_reading(CHARGING_STATUS)), second)

	def test_read_back(self):
		with FakeGdbServer({BASE: REGION_SIZE}) as server:
			server.write(BASE, DISCHARGING_BLOCK)
			address, block = self.make_sync(server).read()
		self.assertEqual((address, block["percent"], block["discharging"]), (BASE, 83, True))

	def test_read_without_block(self):
		with FakeGdbServer({BASE: REGION_SIZE}) as server:
			self.assertEqual(self.make_sync(server).read(), (None, None))

	def test_messages(self):
		self.assertEqual(sync_message(0x50100000), "battery block found at 0x50100000 and written")
		self.assertEqual(sync_message(None), "no battery block (firmware driver not installed)")


class VmStateTests(unittest.TestCase):
	def test_writes_when_qmp_says_running(self):
		with FakeGdbServer({BASE: REGION_SIZE}) as gdb, qmp_server("running") as qmp:
			gdb.write(BASE, MAGIC)
			address = BatterySync(gdb.port, qmp.address, clock=lambda: UPDATED_UNIX).write(battery_reading(DISCHARGING_STATUS))
			self.assertEqual(address, BASE)
			self.assertEqual(gdb.read(BASE, BLOCK_SIZE), DISCHARGING_BLOCK)
		self.assertEqual([request["execute"] for request in qmp.received], ["qmp_capabilities", "query-status"])

	def test_skips_a_paused_vm_without_touching_the_stub(self):
		for status in ("paused", "prelaunch", "suspended"):
			with self.subTest(status=status):
				with FakeGdbServer({BASE: REGION_SIZE}) as gdb, qmp_server(status) as qmp:
					gdb.write(BASE, MAGIC)
					with self.assertRaises(VmNotRunningError) as caught:
						BatterySync(gdb.port, qmp.address).write(battery_reading(DISCHARGING_STATUS))
				self.assertIn("the VM is {}".format(status), str(caught.exception))
				self.assertEqual(gdb.connections, 0)

	def test_skips_when_qmp_is_unreachable(self):
		with FakeGdbServer({BASE: REGION_SIZE}) as gdb:
			gdb.write(BASE, MAGIC)
			with self.assertRaises(VmNotRunningError) as caught:
				BatterySync(gdb.port, "tcp:127.0.0.1:{}".format(unused_port())).write(battery_reading(DISCHARGING_STATUS))
		self.assertIn("cannot check the VM state over QMP", str(caught.exception))
		self.assertEqual(gdb.connections, 0)

	def test_skips_when_query_status_fails(self):
		with FakeGdbServer({BASE: REGION_SIZE}) as gdb, TCPFakeQMPServer() as qmp:
			with self.assertRaises(VmNotRunningError):
				BatterySync(gdb.port, qmp.address).read()
		self.assertEqual(gdb.connections, 0)


class FakeSync:
	def __init__(self, results):
		self.results = list(results)
		self.readings = []

	def write(self, reading):
		self.readings.append(reading)
		result = self.results.pop(0) if self.results else 0x50000000
		if isinstance(result, Exception):
			raise result
		return result


class LoopTests(unittest.TestCase):
	def run_loop(self, statuses, results=(), seconds_per_poll=5):
		clock = {"now": 0.0}
		polls = {"left": len(statuses)}
		status_list = list(statuses)
		log = io.StringIO()
		sync = FakeSync(results)

		def read_status():
			status = status_list.pop(0)
			if isinstance(status, Exception):
				raise status
			return status

		def wait(seconds):
			self.assertEqual(seconds, 5)
			clock["now"] += seconds_per_poll
			polls["left"] -= 1
			return polls["left"] == 0

		run_sync_loop(sync, read_status, log, wait, monotonic=lambda: clock["now"])
		return sync, log.getvalue()

	def test_writes_first_reading_and_only_changes_within_30_seconds(self):
		statuses = [DISCHARGING_STATUS, DISCHARGING_STATUS, CHARGING_STATUS, CHARGING_STATUS]
		sync, _ = self.run_loop(statuses)
		self.assertEqual(sync.readings, [battery_reading(DISCHARGING_STATUS), battery_reading(CHARGING_STATUS)])

	def test_rewrites_every_30_seconds(self):
		sync, _ = self.run_loop([DISCHARGING_STATUS] * 13)
		self.assertEqual(len(sync.readings), 3)

	def test_logs_errors_and_keeps_going(self):
		from wn.gdbstub import GdbStubError
		sync, log = self.run_loop([DISCHARGING_STATUS] * 7, results=[GdbStubError("cannot connect"), 0x50000000])
		self.assertEqual(len(sync.readings), 2)
		self.assertIn("battery sync: cannot connect", log)
		self.assertIn("battery sync: battery block found at 0x50000000 and written", log)

	def test_logs_a_repeated_message_once(self):
		_, log = self.run_loop([DISCHARGING_STATUS] * 13, results=[None, None, None])
		self.assertEqual(log.count("no battery block"), 1)

	def test_survives_unexpected_errors(self):
		sync, log = self.run_loop([RuntimeError("boom"), DISCHARGING_STATUS])
		self.assertEqual(len(sync.readings), 1)
		self.assertIn("boom", log)


class BatteryCommandTests(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		self.addCleanup(self.home.cleanup)
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.server = FakeGdbServer({BASE: REGION_SIZE})
		self.server.__enter__()
		self.addCleanup(self.server.__exit__, None, None, None)
		self.qmp = qmp_server(self.vm_status)
		self.qmp.__enter__()
		self.addCleanup(self.qmp.__exit__, None, None, None)
		os.makedirs(os.path.join(self.home.name, ".config", "wn"))
		with open(os.path.join(self.home.name, ".config", "wn", "config.json"), "w") as config_file:
			json.dump({"gdb_port": self.server.port, "qmp_socket": self.qmp.address}, config_file)

	vm_status = "running"

	def run_cli(self, *argv):
		stdout = io.StringIO()
		stderr = io.StringIO()
		exit_code = main(["battery"] + list(argv), stdout=stdout, stderr=stderr)
		return exit_code, stdout.getvalue(), stderr.getvalue()

	def test_status(self):
		self.server.write(BASE, DISCHARGING_BLOCK)
		exit_code, stdout, _ = self.run_cli("status")
		self.assertEqual(exit_code, 0)
		self.assertIn("Battery block at 0x50000000", stdout)
		self.assertIn("flags: 0x9 (present, discharging)", stdout)
		self.assertIn("percent: 83", stdout)
		self.assertIn("rate_mw: 1976", stdout)

	def test_status_without_block(self):
		exit_code, _, stderr = self.run_cli("status")
		self.assertEqual(exit_code, 1)
		self.assertIn("no battery block (firmware driver not installed)", stderr)

	def test_set_charging(self):
		self.server.write(BASE, MAGIC)
		exit_code, stdout, _ = self.run_cli("set", "--percent", "42", "--charging")
		self.assertEqual(exit_code, 0)
		block = decode_block(self.server.read(BASE, BLOCK_SIZE))
		self.assertEqual((block["percent"], block["flags"], block["remaining_mwh"]), (42, 0b0111, 4200))
		self.assertIn("0x50000000", stdout)

	def test_set_ac(self):
		self.server.write(BASE, MAGIC)
		self.run_cli("set", "--percent", "70", "--ac")
		self.assertEqual(decode_block(self.server.read(BASE, BLOCK_SIZE))["flags"], 0b0011)

	def test_set_defaults_to_discharging(self):
		self.server.write(BASE, MAGIC)
		self.run_cli("set", "--percent", "70")
		self.assertEqual(decode_block(self.server.read(BASE, BLOCK_SIZE))["flags"], 0b1001)

	def test_set_rejects_bad_percent(self):
		exit_code, _, stderr = self.run_cli("set", "--percent", "101")
		self.assertEqual(exit_code, 1)
		self.assertIn("0 to 100", stderr)

	def test_sync_uses_pmset(self):
		self.server.write(BASE, MAGIC)
		with mock.patch("wn.cli.agent.battery_status", return_value=DISCHARGING_STATUS):
			exit_code, stdout, _ = self.run_cli("sync")
		self.assertEqual(exit_code, 0)
		self.assertIn("battery block found at 0x50000000 and written", stdout)
		self.assertEqual(decode_block(self.server.read(BASE, BLOCK_SIZE))["percent"], 83)

	def test_sync_without_block_fails(self):
		with mock.patch("wn.cli.agent.battery_status", return_value=DISCHARGING_STATUS):
			exit_code, _, stderr = self.run_cli("sync")
		self.assertEqual(exit_code, 1)
		self.assertIn("no battery block", stderr)



class PausedBatteryCommandTests(unittest.TestCase):
	def test_sync_skips_a_paused_vm(self):
		home = tempfile.TemporaryDirectory()
		self.addCleanup(home.cleanup)
		with mock.patch.dict(os.environ, {"HOME": home.name}), FakeGdbServer({BASE: REGION_SIZE}) as gdb, qmp_server("paused") as qmp:
			gdb.write(BASE, MAGIC)
			os.makedirs(os.path.join(home.name, ".config", "wn"))
			with open(os.path.join(home.name, ".config", "wn", "config.json"), "w") as config_file:
				json.dump({"gdb_port": gdb.port, "qmp_socket": qmp.address}, config_file)
			stderr = io.StringIO()
			with mock.patch("wn.cli.agent.battery_status", return_value=DISCHARGING_STATUS):
				exit_code = main(["battery", "sync"], stdout=io.StringIO(), stderr=stderr)
		self.assertEqual(exit_code, 1)
		self.assertIn("the VM is paused", stderr.getvalue())
		self.assertEqual(gdb.connections, 0)


if __name__ == "__main__":
	unittest.main()
