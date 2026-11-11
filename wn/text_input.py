"""Turn text into key combos for a US keyboard layout and type it into the guest."""

import string

from wn.errors import WnError
from wn.keys import send_combos

DEFAULT_KEY_DELAY_MS = 20

UNSHIFTED_COMBOS = {
	" ": "spc",
	"\n": "ret",
	"\t": "tab",
	"-": "minus",
	"=": "equal",
	"[": "bracket_left",
	"]": "bracket_right",
	";": "semicolon",
	"'": "apostrophe",
	"`": "grave_accent",
	"\\": "backslash",
	",": "comma",
	".": "dot",
	"/": "slash",
}
UNSHIFTED_COMBOS.update({char: char for char in string.ascii_lowercase + string.digits})

SHIFTED_BASE_KEYS = {
	"!": "1",
	"@": "2",
	"#": "3",
	"$": "4",
	"%": "5",
	"^": "6",
	"&": "7",
	"*": "8",
	"(": "9",
	")": "0",
	"_": "minus",
	"+": "equal",
	"{": "bracket_left",
	"}": "bracket_right",
	":": "semicolon",
	"\"": "apostrophe",
	"~": "grave_accent",
	"|": "backslash",
	"<": "comma",
	">": "dot",
	"?": "slash",
}
SHIFTED_BASE_KEYS.update({char: char.lower() for char in string.ascii_uppercase})

CHAR_COMBOS = dict(UNSHIFTED_COMBOS)
CHAR_COMBOS.update({char: "shift+" + key for char, key in SHIFTED_BASE_KEYS.items()})


class UnsupportedCharacterError(WnError):
	"""Raised for characters that cannot be typed on a US keyboard layout."""


SUPPORTED_CHARACTERS_HINT = "only US-ASCII letters, digits, punctuation, space, tab and newline are supported"


def _unsupported_character_error(char, position=None):
	location = "" if position is None else " at position {}".format(position)
	return UnsupportedCharacterError(
		"cannot type {!r} (U+{:04X}){}: {}".format(char, ord(char), location, SUPPORTED_CHARACTERS_HINT)
	)


def char_to_combo(char):
	"""Return the combo that types ``char`` on a US layout."""
	try:
		return CHAR_COMBOS[char]
	except KeyError:
		raise _unsupported_character_error(char) from None


def text_to_combos(text):
	"""Return one combo per character of ``text``."""
	unsupported = [(position, char) for position, char in enumerate(text) if char not in CHAR_COMBOS]
	if unsupported:
		position, char = unsupported[0]
		raise _unsupported_character_error(char, position)
	return [CHAR_COMBOS[char] for char in text]


def send_text(client, text, delay_ms=DEFAULT_KEY_DELAY_MS, hold_ms=None, sleep=None):
	"""Type ``text`` into the guest through a QMP client."""
	send_combos(client, text_to_combos(text), delay_ms, hold_ms, sleep)
