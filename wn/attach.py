"""Attach the terminal keyboard to the guest.

``wn attach`` puts the terminal in raw mode, reads the bytes the terminal sends
for each key press, turns them into key combos, and sends them to the guest
with QMP ``send-key``.

To detach, press ctrl+] twice within one second. A single ctrl+] is passed to
the guest once that second has gone by or another key is pressed.

A terminal sends bytes, not key events, so some keys never reach wn:

* The Windows key, and anything combined with it. macOS keeps cmd for itself.
* ctrl+alt+del, and most ctrl+alt and ctrl+shift combos. They share bytes with
  plainer keys or are taken by the terminal or the OS.
* Most alt combos. They work only when the terminal sends option as Meta
  (ESC followed by the key), and alt+ctrl or alt+function keys still do not.
* ctrl with digits or punctuation, shift with arrows or function keys, and
  key releases or long holds.
* Any character that is not US-ASCII.

A lone ESC is sent after a short wait, because ESC is also the first byte of
arrow and function key sequences.

Use ``wn key`` for anything attach mode cannot deliver, for example
``wn key ctrl+alt+del`` or ``wn key win+r``.
"""

import os
import select
import sys
import termios
import time
import tty

from wn.errors import WnError
from wn.keys import send_combos
from wn.text_input import CHAR_COMBOS

ESCAPE_TIMEOUT_SECONDS = 0.05
DETACH_WINDOW_SECONDS = 1.0
POLL_INTERVAL_SECONDS = 0.02
READ_SIZE = 1024
MAX_SEQUENCE_LENGTH = 16

ESCAPE = 0x1b
DETACH = 0x1d
CSI_INTRODUCER = ord("[")
SS3_INTRODUCER = ord("O")
CSI_FINAL_BYTES = range(0x40, 0x7f)
FIRST_CTRL_LETTER = 0x01
LAST_CTRL_LETTER = 0x1a
DETACH_COMBO = "ctrl+bracket_right"

SPECIAL_BYTES = {
	0x0d: "ret",
	0x7f: "backspace",
	0x09: "tab",
	0x1c: "ctrl+backslash",
	DETACH: DETACH_COMBO,
}

ESCAPE_SEQUENCES = {
	b"\x1b[A": "up",
	b"\x1b[B": "down",
	b"\x1b[C": "right",
	b"\x1b[D": "left",
	b"\x1bOA": "up",
	b"\x1bOB": "down",
	b"\x1bOC": "right",
	b"\x1bOD": "left",
	b"\x1b[H": "home",
	b"\x1b[F": "end",
	b"\x1bOH": "home",
	b"\x1bOF": "end",
	b"\x1b[1~": "home",
	b"\x1b[4~": "end",
	b"\x1b[2~": "insert",
	b"\x1b[3~": "delete",
	b"\x1b[5~": "pgup",
	b"\x1b[6~": "pgdn",
	b"\x1bOP": "f1",
	b"\x1bOQ": "f2",
	b"\x1bOR": "f3",
	b"\x1bOS": "f4",
	b"\x1b[15~": "f5",
	b"\x1b[17~": "f6",
	b"\x1b[18~": "f7",
	b"\x1b[19~": "f8",
	b"\x1b[20~": "f9",
	b"\x1b[21~": "f10",
	b"\x1b[23~": "f11",
	b"\x1b[24~": "f12",
}


def single_byte_combo(byte):
	"""Return the combo for one byte outside an escape sequence, or None to drop it."""
	if byte in SPECIAL_BYTES:
		return SPECIAL_BYTES[byte]
	if FIRST_CTRL_LETTER <= byte <= LAST_CTRL_LETTER:
		return "ctrl+" + chr(ord("a") + byte - FIRST_CTRL_LETTER)
	return CHAR_COMBOS.get(chr(byte))


def _alt_combo(byte):
	combo = single_byte_combo(byte)
	return ["alt+" + combo] if combo else []


class TerminalTranslator:
	"""Turns terminal input bytes into key combos.

	Call ``feed`` with each chunk read from the terminal and ``expire`` when a
	read times out. Both take the current time so the class has no clock of
	its own. After a double ctrl+], ``detach_requested`` is True.
	"""

	def __init__(self):
		self.detach_requested = False
		self._pending_escape = bytearray()
		self._escape_started_at = None
		self._detach_pressed_at = None

	def feed(self, data, now):
		combos = self.expire(now)
		for byte in data:
			if self.detach_requested:
				break
			combos.extend(self._feed_byte(byte, now))
		return combos

	def expire(self, now):
		"""Resolve a lone ESC or a single ctrl+] whose waiting time has run out."""
		combos = []
		if self._detach_pressed_at is not None and now - self._detach_pressed_at > DETACH_WINDOW_SECONDS:
			combos.append(DETACH_COMBO)
			self._detach_pressed_at = None
		if self._pending_escape and now - self._escape_started_at >= ESCAPE_TIMEOUT_SECONDS:
			combos.extend(self._resolve_incomplete_escape())
		return combos

	def _feed_byte(self, byte, now):
		if byte == DETACH and not self._pending_escape:
			return self._handle_detach_byte(now)
		combos = []
		if self._detach_pressed_at is not None:
			combos.append(DETACH_COMBO)
			self._detach_pressed_at = None
		if self._pending_escape:
			self._pending_escape.append(byte)
			combos.extend(self._parse_escape())
		elif byte == ESCAPE:
			self._start_escape(now)
		else:
			combo = single_byte_combo(byte)
			if combo:
				combos.append(combo)
		return combos

	def _handle_detach_byte(self, now):
		if self._detach_pressed_at is not None:
			self.detach_requested = True
			self._detach_pressed_at = None
			return []
		self._detach_pressed_at = now
		return []

	def _start_escape(self, now):
		self._pending_escape = bytearray([ESCAPE])
		self._escape_started_at = now

	def _finish_escape(self, combos):
		self._pending_escape = bytearray()
		self._escape_started_at = None
		return combos

	def _parse_escape(self):
		sequence = bytes(self._pending_escape)
		introducer = sequence[1]
		if introducer == ESCAPE:
			self._pending_escape = bytearray([ESCAPE])
			return ["esc"]
		if introducer == CSI_INTRODUCER:
			return self._parse_csi(sequence)
		if introducer == SS3_INTRODUCER:
			return self._parse_ss3(sequence)
		return self._finish_escape(_alt_combo(introducer))

	def _parse_csi(self, sequence):
		if any(byte in CSI_FINAL_BYTES for byte in sequence[2:]):
			return self._finish_escape(self._known_sequence(sequence))
		if len(sequence) >= MAX_SEQUENCE_LENGTH:
			return self._finish_escape([])
		return []

	def _parse_ss3(self, sequence):
		if len(sequence) < len(b"\x1bOP"):
			return []
		return self._finish_escape(self._known_sequence(sequence))

	@staticmethod
	def _known_sequence(sequence):
		combo = ESCAPE_SEQUENCES.get(sequence)
		return [combo] if combo else []

	def _resolve_incomplete_escape(self):
		sequence = bytes(self._pending_escape)
		if len(sequence) == 1:
			return self._finish_escape(["esc"])
		if len(sequence) == 2:
			return self._finish_escape(_alt_combo(sequence[1]))
		return self._finish_escape([])


def run_attach_loop(read_chunk, send, clock=time.monotonic):
	"""Read input until detach or end of input, sending combos as they resolve.

	``read_chunk`` returns bytes, None when no input arrived in time, or b"" at
	end of input. ``send`` gets a non-empty list of combos.
	"""
	translator = TerminalTranslator()
	while not translator.detach_requested:
		data = read_chunk()
		if data == b"":
			return
		now = clock()
		combos = translator.expire(now) if data is None else translator.feed(data, now)
		if combos:
			send(combos)


def _read_with_timeout(file_descriptor):
	ready, _, _ = select.select([file_descriptor], [], [], POLL_INTERVAL_SECONDS)
	if not ready:
		return None
	return os.read(file_descriptor, READ_SIZE)


def attach(client, delay_ms, hold_ms=None, file_descriptor=None):
	"""Attach the controlling terminal to the guest until the user detaches."""
	if file_descriptor is None:
		file_descriptor = sys.stdin.fileno()
	if not os.isatty(file_descriptor):
		raise WnError("attach needs an interactive terminal on stdin")
	sys.stderr.write("Attached. Press ctrl+] twice to detach.\r\n")
	sys.stderr.flush()
	saved_attributes = termios.tcgetattr(file_descriptor)
	try:
		tty.setraw(file_descriptor)
		run_attach_loop(
			lambda: _read_with_timeout(file_descriptor),
			lambda combos: send_combos(client, combos, delay_ms, hold_ms),
		)
	finally:
		termios.tcsetattr(file_descriptor, termios.TCSADRAIN, saved_attributes)
		sys.stderr.write("\r\nDetached.\n")
