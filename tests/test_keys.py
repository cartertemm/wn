import unittest

from wn.keys import (
	DEFAULT_HOLD_MS,
	KeyNameError,
	combo_to_input_events,
	combos_to_input_steps,
	key_event,
	parse_combo,
	send_input_steps,
)


class ParseComboTests(unittest.TestCase):
	def test_single_letter(self):
		self.assertEqual(parse_combo("a"), ["a"])

	def test_ctrl_alt_del(self):
		self.assertEqual(parse_combo("ctrl+alt+del"), ["ctrl", "alt", "delete"])

	def test_windows_key_aliases_map_to_meta_l(self):
		for name in ("win", "meta", "cmd"):
			self.assertEqual(parse_combo(name + "+r"), ["meta_l", "r"])

	def test_altgr_maps_to_alt_r(self):
		self.assertEqual(parse_combo("altgr+e"), ["alt_r", "e"])

	def test_named_keys(self):
		expected = {
			"enter": "ret",
			"space": "spc",
			"tab": "tab",
			"esc": "esc",
			"up": "up",
			"pgdn": "pgdn",
			"f1": "f1",
			"f12": "f12",
			"backspace": "backspace",
			"kp_add": "kp_add",
			"caps_lock": "caps_lock",
			"volumeup": "volumeup",
		}
		for name, qcode in expected.items():
			self.assertEqual(parse_combo(name), [qcode], name)

	def test_punctuation_names(self):
		names = ["minus", "equal", "bracket_left", "semicolon", "apostrophe", "grave_accent", "backslash", "comma", "dot", "slash"]
		for name in names:
			self.assertEqual(parse_combo(name), [name])

	def test_digits(self):
		self.assertEqual(parse_combo("7"), ["7"])

	def test_case_insensitive_and_whitespace(self):
		self.assertEqual(parse_combo(" Shift + F10 "), ["shift", "f10"])

	def test_modifiers_move_to_front(self):
		self.assertEqual(parse_combo("a+ctrl+shift"), ["ctrl", "shift", "a"])

	def test_unknown_name_lists_close_matches(self):
		with self.assertRaises(KeyNameError) as context:
			parse_combo("ctrl+entr")
		message = str(context.exception)
		self.assertIn("entr", message)
		self.assertIn("enter", message)

	def test_empty_part_rejected(self):
		with self.assertRaises(KeyNameError):
			parse_combo("ctrl++a")

	def test_empty_combo_rejected(self):
		with self.assertRaises(KeyNameError):
			parse_combo("")

	def test_duplicate_key_rejected(self):
		with self.assertRaises(KeyNameError):
			parse_combo("ctrl+control+a")


def press(*qcodes):
	return {"events": [key_event(qcode, True) for qcode in qcodes]}


def release(*qcodes):
	return {"events": [key_event(qcode, False) for qcode in qcodes]}


class InputEventTests(unittest.TestCase):
	def test_key_event(self):
		self.assertEqual(key_event("ret", True), {"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": "ret"}}})

	def test_combo_presses_in_order_and_releases_in_reverse(self):
		self.assertEqual(combo_to_input_events("ctrl+alt+del"), (press("ctrl", "alt", "delete"), release("delete", "alt", "ctrl")))

	def test_steps_hold_then_delay(self):
		self.assertEqual(combos_to_input_steps(["enter"], delay_ms=20, hold_ms=150), [(press("ret"), 150), (release("ret"), 20)])

	def test_default_hold(self):
		self.assertEqual(combos_to_input_steps(["a"], delay_ms=20)[0], (press("a"), DEFAULT_HOLD_MS))

	def test_repeated_combo_gives_separate_presses(self):
		steps = combos_to_input_steps(["down", "down"], delay_ms=20, hold_ms=50)
		self.assertEqual([arguments for arguments, _ in steps], [press("down"), release("down"), press("down"), release("down")])


class RecordingClient:
	def __init__(self):
		self.commands = []

	def execute(self, command, arguments=None):
		self.commands.append((command, arguments))
		return {}


class SendInputStepsTests(unittest.TestCase):
	def test_sends_input_events_and_sleeps_for_hold_and_delay(self):
		client = RecordingClient()
		sleeps = []
		send_input_steps(client, combos_to_input_steps(["a", "shift+b"], delay_ms=30, hold_ms=40), sleep=sleeps.append)
		self.assertEqual(client.commands, [
			("input-send-event", press("a")),
			("input-send-event", release("a")),
			("input-send-event", press("shift", "b")),
			("input-send-event", release("b", "shift")),
		])
		self.assertEqual(sleeps, [0.04, 0.03, 0.04, 0.03])

	def test_validates_all_combos_before_any_step(self):
		with self.assertRaises(KeyNameError):
			combos_to_input_steps(["a", "bogus"], delay_ms=0)


if __name__ == "__main__":
	unittest.main()
