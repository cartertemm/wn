import io
import json
import os
import tempfile
import unittest
from unittest import mock

from wn.battery_sync import (
	BLOCK_SIZE,
	BatteryBlockError,
	battery_reading,
	decode_block,
	encode_block,
	run_sync_loop,
)
from wn.cli import main

UPDATED = "2026-11-11T12:00:00Z"
UPDATED_UNIX = 1789000000
BATTERY_PATH = "/tmp/wn-battery.bin"
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


class FakeSync:
	def __init__(self, results):
		self.results = list(results)
		self.readings = []

	def write(self, reading):
		self.readings.append(reading)
		result = self.results.pop(0) if self.results else BATTERY_PATH
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
		sync, log = self.run_loop([DISCHARGING_STATUS] * 7, results=[BatteryBlockError("cannot write"), BATTERY_PATH])
		self.assertEqual(len(sync.readings), 2)
		self.assertIn("battery sync: cannot write", log)
		self.assertIn("battery sync: battery file {} written".format(BATTERY_PATH), log)

	def test_logs_a_repeated_message_once(self):
		_, log = self.run_loop([DISCHARGING_STATUS] * 13)
		self.assertEqual(log.count("battery file"), 1)

	def test_survives_unexpected_errors(self):
		sync, log = self.run_loop([RuntimeError("boom"), DISCHARGING_STATUS])
		self.assertEqual(len(sync.readings), 1)
		self.assertIn("boom", log)


class FileBatteryCommandTests(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		self.addCleanup(self.home.cleanup)
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.path = os.path.join(self.home.name, "wn-battery.bin")
		os.makedirs(os.path.join(self.home.name, ".config", "wn"))
		with open(os.path.join(self.home.name, ".config", "wn", "config.json"), "w") as config_file:
			json.dump({"battery_file": self.path}, config_file)

	def run_cli(self, *argv):
		stdout = io.StringIO()
		stderr = io.StringIO()
		exit_code = main(["battery"] + list(argv), stdout=stdout, stderr=stderr)
		return exit_code, stdout.getvalue(), stderr.getvalue()

	def test_set_then_status(self):
		exit_code, stdout, stderr = self.run_cli("set", "--percent", "42", "--charging")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(stdout, "battery file {} written\n".format(self.path))
		exit_code, stdout, _ = self.run_cli("status")
		self.assertEqual(exit_code, 0)
		self.assertIn("Battery block at {}".format(self.path), stdout)
		self.assertIn("flags: 0x7 (present, ac online, charging)", stdout)
		self.assertIn("percent: 42", stdout)

	def test_status_without_file(self):
		exit_code, _, stderr = self.run_cli("status")
		self.assertEqual(exit_code, 1)
		self.assertIn(self.path, stderr)

	def test_status_without_block(self):
		open(self.path, "wb").close()
		exit_code, _, stderr = self.run_cli("status")
		self.assertEqual(exit_code, 1)
		self.assertIn("no battery block (firmware driver not installed)", stderr)

	def read_block(self):
		with open(self.path, "rb") as data_file:
			return decode_block(data_file.read(BLOCK_SIZE))

	def test_set_ac(self):
		self.run_cli("set", "--percent", "70", "--ac")
		self.assertEqual(self.read_block()["flags"], 0b0011)

	def test_set_defaults_to_discharging(self):
		self.run_cli("set", "--percent", "70")
		self.assertEqual(self.read_block()["flags"], 0b1001)

	def test_set_rejects_bad_percent(self):
		exit_code, _, stderr = self.run_cli("set", "--percent", "101")
		self.assertEqual(exit_code, 1)
		self.assertIn("0 to 100", stderr)

	def test_sync_uses_pmset(self):
		with mock.patch("wn.cli.agent.battery_status", return_value=DISCHARGING_STATUS):
			exit_code, stdout, _ = self.run_cli("sync")
		self.assertEqual(exit_code, 0)
		self.assertEqual(stdout, "battery file {} written\n".format(self.path))
		self.assertEqual(self.read_block()["percent"], 83)


if __name__ == "__main__":
	unittest.main()
