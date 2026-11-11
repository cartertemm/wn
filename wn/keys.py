"""Parse key combos such as ``ctrl+alt+del`` into QEMU qcode names."""

import difflib
import time

from wn.errors import WnError

COMBO_SEPARATOR = "+"
MAX_CLOSE_MATCHES = 3
MILLISECONDS_PER_SECOND = 1000
DEFAULT_HOLD_MS = 50
INPUT_SEND_EVENT = "input-send-event"

MODIFIER_QCODES = ("ctrl", "ctrl_r", "shift", "shift_r", "alt", "alt_r", "meta_l", "meta_r")

PLAIN_QCODES = (
	"delete", "ret", "esc", "spc", "backspace", "tab",
	"pgup", "pgdn", "home", "end", "insert",
	"up", "down", "left", "right",
	"minus", "equal", "bracket_left", "bracket_right", "semicolon", "apostrophe",
	"grave_accent", "backslash", "comma", "dot", "slash",
	"caps_lock", "print", "scroll_lock", "pause", "menu",
	"audiomute", "volumeup", "volumedown",
	"kp_divide", "kp_multiply", "kp_subtract", "kp_add", "kp_enter", "kp_decimal", "kp_equals", "kp_comma",
)
FUNCTION_KEY_COUNT = 12
FUNCTION_QCODES = tuple("f{}".format(number) for number in range(1, FUNCTION_KEY_COUNT + 1))
KEYPAD_DIGIT_QCODES = tuple("kp_{}".format(digit) for digit in range(10))
DIGIT_QCODES = tuple(str(digit) for digit in range(10))
LETTER_QCODES = tuple("abcdefghijklmnopqrstuvwxyz")

ALL_QCODES = MODIFIER_QCODES + PLAIN_QCODES + FUNCTION_QCODES + KEYPAD_DIGIT_QCODES + DIGIT_QCODES + LETTER_QCODES

ALIASES = {
	"control": "ctrl",
	"lctrl": "ctrl",
	"rctrl": "ctrl_r",
	"lshift": "shift",
	"rshift": "shift_r",
	"option": "alt",
	"lalt": "alt",
	"ralt": "alt_r",
	"altgr": "alt_r",
	"win": "meta_l",
	"meta": "meta_l",
	"cmd": "meta_l",
	"super": "meta_l",
	"rwin": "meta_r",
	"del": "delete",
	"enter": "ret",
	"return": "ret",
	"escape": "esc",
	"space": "spc",
	"bksp": "backspace",
	"pageup": "pgup",
	"pagedown": "pgdn",
	"ins": "insert",
	"capslock": "caps_lock",
	"printscreen": "print",
	"prtsc": "print",
	"scrolllock": "scroll_lock",
	"apps": "menu",
	"mute": "audiomute",
	"period": "dot",
	"quote": "apostrophe",
	"grave": "grave_accent",
	"backtick": "grave_accent",
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

KNOWN_NAMES = {qcode: qcode for qcode in ALL_QCODES}
KNOWN_NAMES.update(ALIASES)


class KeyNameError(WnError):
	"""Raised when a combo contains a key name that wn does not know."""


def _unknown_name_message(name):
	matches = difflib.get_close_matches(name, KNOWN_NAMES, n=MAX_CLOSE_MATCHES)
	message = "unknown key name '{}'".format(name)
	if matches:
		message += ". Did you mean: {}?".format(", ".join(matches))
	return message


def _name_to_qcode(name):
	if not name:
		raise KeyNameError("empty key name in combo")
	try:
		return KNOWN_NAMES[name]
	except KeyError:
		raise KeyNameError(_unknown_name_message(name)) from None


def parse_combo(combo):
	"""Return the qcodes in ``combo``, modifiers first, other keys in the given order."""
	names = [part.strip().lower() for part in combo.split(COMBO_SEPARATOR)]
	qcodes = [_name_to_qcode(name) for name in names]
	if len(set(qcodes)) != len(qcodes):
		raise KeyNameError("combo '{}' names the same key twice".format(combo))
	modifiers = [qcode for qcode in qcodes if qcode in MODIFIER_QCODES]
	others = [qcode for qcode in qcodes if qcode not in MODIFIER_QCODES]
	return modifiers + others


def key_event(qcode, down):
	"""Return one ``input-send-event`` key event."""
	return {"type": "key", "data": {"down": down, "key": {"type": "qcode", "data": qcode}}}


def combo_to_input_events(combo):
	"""Return the ``input-send-event`` arguments that press ``combo`` and then release it, last key first."""
	qcodes = parse_combo(combo)
	press = {"events": [key_event(qcode, True) for qcode in qcodes]}
	release = {"events": [key_event(qcode, False) for qcode in reversed(qcodes)]}
	return press, release


def combos_to_input_steps(combos, delay_ms, hold_ms=None):
	"""Return ``(arguments, pause_ms)`` steps that press each combo, hold it ``hold_ms``, release it, and wait ``delay_ms``.

	Every combo is parsed first, so a typo never leaves a half-typed sequence in the guest.
	"""
	hold_ms = DEFAULT_HOLD_MS if hold_ms is None else hold_ms
	steps = []
	for combo in combos:
		press, release = combo_to_input_events(combo)
		steps += [(press, hold_ms), (release, delay_ms)]
	return steps


def send_input_steps(client, steps, sleep=None):
	"""Send each step with ``input-send-event`` and pause after it."""
	sleep = sleep or time.sleep
	for arguments, pause_ms in steps:
		client.execute(INPUT_SEND_EVENT, arguments)
		sleep(pause_ms / MILLISECONDS_PER_SECOND)


def send_combos(client, combos, delay_ms, hold_ms=None, sleep=None):
	"""Press and release each combo in turn through ``client``."""
	send_input_steps(client, combos_to_input_steps(combos, delay_ms, hold_ms), sleep)
