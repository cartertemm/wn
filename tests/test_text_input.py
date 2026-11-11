import unittest

from wn.keys import DEFAULT_HOLD_MS, combo_to_input_events
from wn.text_input import UnsupportedCharacterError, char_to_combo, send_text, text_to_combos


class CharToComboTests(unittest.TestCase):
	def test_lowercase_letter(self):
		self.assertEqual(char_to_combo("q"), "q")

	def test_uppercase_letter(self):
		self.assertEqual(char_to_combo("A"), "shift+a")

	def test_digit(self):
		self.assertEqual(char_to_combo("5"), "5")

	def test_whitespace(self):
		self.assertEqual(char_to_combo(" "), "spc")
		self.assertEqual(char_to_combo("\n"), "ret")
		self.assertEqual(char_to_combo("\t"), "tab")

	def test_unshifted_punctuation(self):
		expected = {
			"-": "minus", "=": "equal", "[": "bracket_left", "]": "bracket_right",
			";": "semicolon", "'": "apostrophe", "`": "grave_accent", "\\": "backslash",
			",": "comma", ".": "dot", "/": "slash",
		}
		for char, combo in expected.items():
			self.assertEqual(char_to_combo(char), combo, char)

	def test_shifted_punctuation(self):
		expected = {
			"!": "shift+1", "@": "shift+2", "#": "shift+3", "$": "shift+4", "%": "shift+5",
			"^": "shift+6", "&": "shift+7", "*": "shift+8", "(": "shift+9", ")": "shift+0",
			"_": "shift+minus", "+": "shift+equal", "{": "shift+bracket_left", "}": "shift+bracket_right",
			":": "shift+semicolon", "\"": "shift+apostrophe", "~": "shift+grave_accent", "|": "shift+backslash",
			"<": "shift+comma", ">": "shift+dot", "?": "shift+slash",
		}
		for char, combo in expected.items():
			self.assertEqual(char_to_combo(char), combo, char)

	def test_every_printable_ascii_char_is_supported(self):
		for code in range(0x20, 0x7f):
			combo_to_input_events(char_to_combo(chr(code)))

	def test_non_ascii_rejected(self):
		with self.assertRaises(UnsupportedCharacterError) as context:
			char_to_combo("é")
		self.assertIn("U+00E9", str(context.exception))


class TextToCombosTests(unittest.TestCase):
	def test_sentence(self):
		self.assertEqual(text_to_combos("Hi!\n"), ["shift+h", "i", "shift+1", "ret"])

	def test_error_reports_position(self):
		with self.assertRaises(UnsupportedCharacterError) as context:
			text_to_combos("ab€c")
		self.assertIn("position 2", str(context.exception))


class RecordingClient:
	def __init__(self):
		self.commands = []

	def execute(self, command, arguments=None):
		self.commands.append((command, arguments))
		return {}


class SendTextTests(unittest.TestCase):
	def test_sends_press_and_release_per_char_with_default_delay(self):
		client = RecordingClient()
		sleeps = []
		send_text(client, "aB", sleep=sleeps.append)
		a_press, a_release = combo_to_input_events("a")
		b_press, b_release = combo_to_input_events("shift+b")
		self.assertEqual(client.commands, [
			("input-send-event", a_press),
			("input-send-event", a_release),
			("input-send-event", b_press),
			("input-send-event", b_release),
		])
		hold_seconds = DEFAULT_HOLD_MS / 1000
		self.assertEqual(sleeps, [hold_seconds, 0.02, hold_seconds, 0.02])

	def test_repeated_letters_are_pressed_twice(self):
		client = RecordingClient()
		send_text(client, "hello", sleep=lambda seconds: None)
		l_press, l_release = combo_to_input_events("l")
		arguments = [arguments for _, arguments in client.commands]
		self.assertEqual(arguments[4:8], [l_press, l_release, l_press, l_release])
		self.assertEqual(arguments.count(l_press), 2)

	def test_hold_time_and_delay_are_passed_through(self):
		client = RecordingClient()
		sleeps = []
		send_text(client, "x", delay_ms=50, hold_ms=10, sleep=sleeps.append)
		self.assertEqual(sleeps, [0.01, 0.05])

	def test_nothing_sent_when_text_has_unsupported_char(self):
		client = RecordingClient()
		with self.assertRaises(UnsupportedCharacterError):
			send_text(client, "okü", sleep=lambda seconds: None)
		self.assertEqual(client.commands, [])


if __name__ == "__main__":
	unittest.main()
