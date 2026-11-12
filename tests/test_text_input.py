import unittest

from wn.keys import combo_to_input_events
from wn.text_input import UnsupportedCharacterError, text_to_combos


class CharacterComboTests(unittest.TestCase):
	def test_lowercase_letter(self):
		self.assertEqual(text_to_combos("q")[0], "q")

	def test_uppercase_letter(self):
		self.assertEqual(text_to_combos("A")[0], "shift+a")

	def test_digit(self):
		self.assertEqual(text_to_combos("5")[0], "5")

	def test_whitespace(self):
		self.assertEqual(text_to_combos(" ")[0], "spc")
		self.assertEqual(text_to_combos("\n")[0], "ret")
		self.assertEqual(text_to_combos("\t")[0], "tab")

	def test_unshifted_punctuation(self):
		expected = {
			"-": "minus", "=": "equal", "[": "bracket_left", "]": "bracket_right",
			";": "semicolon", "'": "apostrophe", "`": "grave_accent", "\\": "backslash",
			",": "comma", ".": "dot", "/": "slash",
		}
		for char, combo in expected.items():
			self.assertEqual(text_to_combos(char)[0], combo, char)

	def test_shifted_punctuation(self):
		expected = {
			"!": "shift+1", "@": "shift+2", "#": "shift+3", "$": "shift+4", "%": "shift+5",
			"^": "shift+6", "&": "shift+7", "*": "shift+8", "(": "shift+9", ")": "shift+0",
			"_": "shift+minus", "+": "shift+equal", "{": "shift+bracket_left", "}": "shift+bracket_right",
			":": "shift+semicolon", "\"": "shift+apostrophe", "~": "shift+grave_accent", "|": "shift+backslash",
			"<": "shift+comma", ">": "shift+dot", "?": "shift+slash",
		}
		for char, combo in expected.items():
			self.assertEqual(text_to_combos(char)[0], combo, char)

	def test_every_printable_ascii_char_is_supported(self):
		for code in range(0x20, 0x7f):
			combo_to_input_events(text_to_combos(chr(code))[0])

	def test_non_ascii_rejected(self):
		with self.assertRaises(UnsupportedCharacterError) as context:
			text_to_combos("é")[0]
		self.assertIn("U+00E9", str(context.exception))


class TextToCombosTests(unittest.TestCase):
	def test_sentence(self):
		self.assertEqual(text_to_combos("Hi!\n"), ["shift+h", "i", "shift+1", "ret"])

	def test_error_reports_position(self):
		with self.assertRaises(UnsupportedCharacterError) as context:
			text_to_combos("ab€c")
		self.assertIn("position 2", str(context.exception))


if __name__ == "__main__":
	unittest.main()
