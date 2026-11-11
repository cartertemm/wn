import unittest

from wn.keys import KeyNameError, combo_to_send_key_arguments, parse_combo, send_combos


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


class SendKeyArgumentsTests(unittest.TestCase):
	def test_arguments_without_hold_time(self):
		self.assertEqual(
			combo_to_send_key_arguments("ctrl+alt+del"),
			{"keys": [
				{"type": "qcode", "data": "ctrl"},
				{"type": "qcode", "data": "alt"},
				{"type": "qcode", "data": "delete"},
			]},
		)

	def test_arguments_with_hold_time(self):
		self.assertEqual(
			combo_to_send_key_arguments("enter", hold_ms=150),
			{"keys": [{"type": "qcode", "data": "ret"}], "hold-time": 150},
		)


class RecordingClient:
	def __init__(self):
		self.commands = []

	def execute(self, command, arguments=None):
		self.commands.append((command, arguments))
		return {}


class SendCombosTests(unittest.TestCase):
	def test_sends_each_combo_and_sleeps_between(self):
		client = RecordingClient()
		sleeps = []
		send_combos(client, ["a", "shift+b"], delay_ms=30, hold_ms=None, sleep=sleeps.append)
		self.assertEqual([command for command, _ in client.commands], ["send-key", "send-key"])
		self.assertEqual(client.commands[1][1], combo_to_send_key_arguments("shift+b"))
		self.assertEqual(sleeps, [0.03, 0.03])

	def test_validates_all_combos_before_sending(self):
		client = RecordingClient()
		with self.assertRaises(KeyNameError):
			send_combos(client, ["a", "bogus"], delay_ms=0, hold_ms=None, sleep=lambda seconds: None)
		self.assertEqual(client.commands, [])


if __name__ == "__main__":
	unittest.main()
