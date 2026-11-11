import io
import json
import os
import signal
import tempfile
import time
import unittest
from unittest import mock

from fake_keygrab_helper import read_log, write_fake_helper
from fake_qmp_server import FakeQMPServer, reply
from fakes import RecordingQMPClient
from wn import keygrab
from wn.errors import WnError
from wn.keygrab import KeyEventTranslator, default_keycodes_path, load_keycode_table, run_grab
from wn.qmp import QMPClient

RELEASE_HOTKEY = "control,option,command"
KEYCODE_A = 0
KEYCODE_S = 1
KEYCODE_LEFT_SHIFT = 56
KEYCODE_RIGHT_SHIFT = 60
KEYCODE_LEFT_COMMAND = 55
KEYCODE_RIGHT_COMMAND = 54
KEYCODE_FN = 63
FAST_QUIT_TIMEOUT_SECONDS = 0.2
SLOW_TEST_SECONDS = 5

TABLE = {
	KEYCODE_A: "a",
	KEYCODE_S: "s",
	KEYCODE_LEFT_SHIFT: "shift",
	KEYCODE_RIGHT_SHIFT: "shift_r",
	KEYCODE_LEFT_COMMAND: "meta_l",
	KEYCODE_RIGHT_COMMAND: "meta_r",
}


def key_event(qcode, down):
	return {"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": qcode}}}


def send_arguments(*events):
	return {"events": list(events)}


def down(keycode, repeat=False):
	line = {"type": "down", "keycode": keycode, "flags": 0, "ts": 1.0}
	if repeat:
		line["repeat"] = True
	return line


def up(keycode):
	return {"type": "up", "keycode": keycode, "flags": 0, "ts": 1.0}


def grab_line(active):
	return {"type": "grab", "active": active}


TAP_RE_ENABLED = {"type": "tap", "state": "re-enabled", "reason": "timeout"}


class KeycodeTableTests(unittest.TestCase):
	def test_default_table_maps_letters_and_command_keys(self):
		table = load_keycode_table(default_keycodes_path())
		self.assertEqual(table[KEYCODE_A], "a")
		self.assertEqual(table[KEYCODE_LEFT_COMMAND], "meta_l")
		self.assertEqual(table[KEYCODE_RIGHT_COMMAND], "meta_r")
		self.assertNotIn(KEYCODE_FN, table)

	def test_missing_file(self):
		with self.assertRaises(WnError) as context:
			load_keycode_table("/no/such/keycodes.json")
		self.assertIn("/no/such/keycodes.json", str(context.exception))

	def test_file_without_keycodes_object(self):
		with tempfile.NamedTemporaryFile("w", suffix=".json") as table_file:
			json.dump({"_unmapped": []}, table_file)
			table_file.flush()
			with self.assertRaises(WnError):
				load_keycode_table(table_file.name)


class KeyEventTranslatorTests(unittest.TestCase):
	def setUp(self):
		self.translator = KeyEventTranslator(TABLE)

	def test_down_and_up(self):
		self.assertEqual(self.translator.translate(down(KEYCODE_A)), [send_arguments(key_event("a", True))])
		self.assertEqual(self.translator.translate(up(KEYCODE_A)), [send_arguments(key_event("a", False))])

	def test_left_and_right_modifiers_differ(self):
		self.assertEqual(self.translator.translate(down(KEYCODE_LEFT_SHIFT)), [send_arguments(key_event("shift", True))])
		self.assertEqual(self.translator.translate(down(KEYCODE_RIGHT_SHIFT)), [send_arguments(key_event("shift_r", True))])
		self.assertEqual(self.translator.translate(down(KEYCODE_LEFT_COMMAND)), [send_arguments(key_event("meta_l", True))])
		self.assertEqual(self.translator.translate(down(KEYCODE_RIGHT_COMMAND)), [send_arguments(key_event("meta_r", True))])

	def test_unmapped_keycode_is_dropped_and_counted(self):
		self.assertEqual(self.translator.translate(down(KEYCODE_FN)), [])
		self.assertEqual(self.translator.translate(up(KEYCODE_FN)), [])
		self.assertEqual(self.translator.dropped_count, 2)
		self.assertEqual(self.translator.release_all(), [])

	def test_up_for_key_not_held_is_dropped(self):
		self.assertEqual(self.translator.translate(up(KEYCODE_A)), [])

	def test_repeat_is_sent_by_default(self):
		self.translator.translate(down(KEYCODE_A))
		self.assertEqual(self.translator.translate(down(KEYCODE_A, repeat=True)), [send_arguments(key_event("a", True))])

	def test_repeat_is_dropped_when_disabled(self):
		translator = KeyEventTranslator(TABLE, send_repeats=False)
		translator.translate(down(KEYCODE_A))
		self.assertEqual(translator.translate(down(KEYCODE_A, repeat=True)), [])
		self.assertEqual(translator.translate(up(KEYCODE_A)), [send_arguments(key_event("a", False))])

	def test_grab_off_releases_held_keys_in_reverse_order(self):
		self.translator.translate(down(KEYCODE_LEFT_SHIFT))
		self.translator.translate(down(KEYCODE_A))
		self.assertEqual(self.translator.translate(grab_line(False)), [send_arguments(key_event("a", False), key_event("shift", False))])

	def test_release_is_idempotent(self):
		self.translator.translate(down(KEYCODE_A))
		self.translator.translate(grab_line(False))
		self.assertEqual(self.translator.translate(up(KEYCODE_A)), [])
		self.assertEqual(self.translator.release_all(), [])

	def test_tap_re_enabled_releases_held_keys(self):
		self.translator.translate(down(KEYCODE_S))
		self.assertEqual(self.translator.translate(TAP_RE_ENABLED), [send_arguments(key_event("s", False))])

	def test_grab_on_and_unknown_lines_send_nothing(self):
		self.translator.translate(down(KEYCODE_A))
		self.assertEqual(self.translator.translate(grab_line(True)), [])
		self.assertEqual(self.translator.translate({"type": "future", "keycode": KEYCODE_A}), [])
		self.assertEqual(self.translator.release_all(), [send_arguments(key_event("a", False))])


class RunGrabTests(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.stdout = io.StringIO()

	def make_helper(self, lines, **options):
		return write_fake_helper(self.directory.name, lines, **options)

	def run_against_fake_server(self, helper_path, on_send=None, **options):
		def handle_send(request):
			if on_send:
				on_send()
			return [reply(request, {})]

		with FakeQMPServer({"input-send-event": handle_send}) as server:
			with QMPClient(server.socket_path) as client:
				run_grab(client, helper_path, TABLE, RELEASE_HOTKEY, False, self.stdout, **options)
		return [request["arguments"] for request in server.received if request["execute"] == "input-send-event"]

	def test_forwards_keys_and_releases_on_grab_off(self):
		helper_path, log_path = self.make_helper([
			grab_line(True),
			down(KEYCODE_LEFT_SHIFT),
			down(KEYCODE_A),
			up(KEYCODE_A),
			down(KEYCODE_A, repeat=True),
			grab_line(False),
			up(KEYCODE_A),
			up(KEYCODE_LEFT_SHIFT),
			grab_line(True),
			down(KEYCODE_FN),
			"not json",
		])
		sent = self.run_against_fake_server(helper_path)
		self.assertEqual(sent, [
			send_arguments(key_event("shift", True)),
			send_arguments(key_event("a", True)),
			send_arguments(key_event("a", False)),
			send_arguments(key_event("a", True)),
			send_arguments(key_event("a", False), key_event("shift", False)),
		])
		output = self.stdout.getvalue()
		self.assertIn("grab on", output)
		self.assertIn("grab off", output)
		self.assertIn(RELEASE_HOTKEY.replace(",", "+"), output)
		self.assertIn("dropped 1", output)
		self.assertEqual(read_log(log_path)[0], ["--release-hotkey", RELEASE_HOTKEY])

	def test_releases_held_keys_when_helper_exits(self):
		helper_path, _ = self.make_helper([down(KEYCODE_A)])
		sent = self.run_against_fake_server(helper_path)
		self.assertEqual(sent, [send_arguments(key_event("a", True)), send_arguments(key_event("a", False))])

	def test_tap_re_enabled_warns_and_releases(self):
		helper_path, _ = self.make_helper([down(KEYCODE_S), TAP_RE_ENABLED])
		sent = self.run_against_fake_server(helper_path)
		self.assertEqual(sent[-1], send_arguments(key_event("s", False)))
		self.assertIn("lost", self.stdout.getvalue())

	def test_send_repeats_option(self):
		helper_path, _ = self.make_helper([down(KEYCODE_A), down(KEYCODE_A, repeat=True), up(KEYCODE_A)])
		sent = self.run_against_fake_server(helper_path, send_repeats=False)
		self.assertEqual(sent, [send_arguments(key_event("a", True)), send_arguments(key_event("a", False))])

	def test_start_released_is_passed_to_helper(self):
		helper_path, log_path = self.make_helper([grab_line(False)])
		run_grab(RecordingQMPClient([]), helper_path, TABLE, "control,shift", True, self.stdout)
		self.assertEqual(read_log(log_path)[0], ["--release-hotkey", "control,shift", "--start-released"])

	def assert_signal_stops_helper(self, signal_number):
		helper_path, log_path = self.make_helper([grab_line(True), down(KEYCODE_A)], wait_for_commands=True)
		signals_left = [signal_number]

		def signal_on_first_send():
			if signals_left:
				os.kill(os.getpid(), signals_left.pop())

		sent = self.run_against_fake_server(helper_path, on_send=signal_on_first_send)
		self.assertEqual(sent, [send_arguments(key_event("a", True)), send_arguments(key_event("a", False))])
		self.assertEqual(read_log(log_path)[1], ["quit"])

	def test_sigterm_sends_quit_and_releases_keys(self):
		self.assert_signal_stops_helper(signal.SIGTERM)

	def test_keyboard_interrupt_sends_quit_and_releases_keys(self):
		self.assert_signal_stops_helper(signal.SIGINT)

	def test_sigterm_handler_is_restored(self):
		previous_handler = signal.getsignal(signal.SIGTERM)
		helper_path, _ = self.make_helper([])
		run_grab(RecordingQMPClient([]), helper_path, TABLE, RELEASE_HOTKEY, False, self.stdout)
		self.assertIs(signal.getsignal(signal.SIGTERM), previous_handler)

	def test_helper_that_ignores_quit_is_terminated(self):
		helper_path, log_path = self.make_helper([grab_line(True)], wait_for_commands=True, ignore_quit=True)
		stdout = InterruptingOutput("grab on")
		started_at = time.monotonic()
		with mock.patch.object(keygrab, "QUIT_TIMEOUT_SECONDS", FAST_QUIT_TIMEOUT_SECONDS):
			run_grab(RecordingQMPClient([]), helper_path, TABLE, RELEASE_HOTKEY, False, stdout)
		self.assertLess(time.monotonic() - started_at, SLOW_TEST_SECONDS)
		self.assertEqual(read_log(log_path)[1], ["quit"])

	def test_permission_error(self):
		helper_path, _ = self.make_helper([{"type": "error", "reason": "accessibility-permission"}], exit_code=keygrab.EXIT_NO_PERMISSION)
		with self.assertRaises(WnError) as context:
			run_grab(RecordingQMPClient([]), helper_path, TABLE, RELEASE_HOTKEY, False, self.stdout)
		self.assertIn("Privacy & Security > Accessibility", str(context.exception))
		self.assertIn("permission needed", self.stdout.getvalue())

	def test_other_helper_failure(self):
		helper_path, _ = self.make_helper([], exit_code=keygrab.EXIT_USAGE)
		with self.assertRaises(WnError) as context:
			run_grab(RecordingQMPClient([]), helper_path, TABLE, "control,banana", False, self.stdout)
		self.assertIn("control,banana", str(context.exception))

	def test_helper_killed_by_signal(self):
		helper_path, _ = self.make_helper([])
		with open(helper_path, "w") as helper_file:
			helper_file.write("#!/bin/sh\nkill -KILL $$\n")
		with self.assertRaises(WnError) as context:
			run_grab(RecordingQMPClient([]), helper_path, TABLE, RELEASE_HOTKEY, False, self.stdout)
		self.assertIn("signal", str(context.exception))

	def test_missing_helper(self):
		with self.assertRaises(WnError) as context:
			run_grab(RecordingQMPClient([]), "/no/such/wn-keygrab", TABLE, RELEASE_HOTKEY, False, self.stdout)
		self.assertIn("build.sh", str(context.exception))


class InterruptingOutput(io.StringIO):
	"""Raises KeyboardInterrupt the first time ``trigger`` is written."""

	def __init__(self, trigger):
		super().__init__()
		self.trigger = trigger
		self.triggered = False

	def write(self, text):
		super().write(text)
		if self.trigger in text and not self.triggered:
			self.triggered = True
			raise KeyboardInterrupt
		return len(text)


if __name__ == "__main__":
	unittest.main()
