import io
import os
import pty
import termios
import tty
import unittest
from unittest import mock

from wn.attach import DETACH_WINDOW_SECONDS, ESCAPE_TIMEOUT_SECONDS, TerminalTranslator, attach, run_attach_loop
from wn.errors import WnError
from wn.keys import combo_to_input_events

DETACH_BYTE = b"\x1d"
LOCAL_FLAGS_INDEX = 3
CANONICAL_ECHO_FLAGS = termios.ICANON | termios.ECHO


class TranslatorTests(unittest.TestCase):
	def setUp(self):
		self.translator = TerminalTranslator()

	def feed(self, data, now=0.0):
		return self.translator.feed(data, now)

	def test_printable_text(self):
		self.assertEqual(self.feed(b"aZ!"), ["a", "shift+z", "shift+1"])

	def test_enter_backspace_tab(self):
		self.assertEqual(self.feed(b"\r\x7f\t"), ["ret", "backspace", "tab"])

	def test_control_letters(self):
		self.assertEqual(self.feed(b"\x01\x03\x1a"), ["ctrl+a", "ctrl+c", "ctrl+z"])

	def test_arrows(self):
		self.assertEqual(self.feed(b"\x1b[A\x1b[B\x1b[C\x1b[D"), ["up", "down", "right", "left"])

	def test_home_end_variants(self):
		self.assertEqual(self.feed(b"\x1b[H\x1b[F\x1b[1~\x1b[4~"), ["home", "end", "home", "end"])

	def test_insert_delete_page_keys(self):
		self.assertEqual(self.feed(b"\x1b[2~\x1b[3~\x1b[5~\x1b[6~"), ["insert", "delete", "pgup", "pgdn"])

	def test_function_keys(self):
		data = b"\x1bOP\x1bOQ\x1bOR\x1bOS\x1b[15~\x1b[17~\x1b[18~\x1b[19~\x1b[20~\x1b[21~\x1b[23~\x1b[24~"
		self.assertEqual(self.feed(data), ["f{}".format(number) for number in range(1, 13)])

	def test_lone_escape_waits_then_expires(self):
		self.assertEqual(self.feed(b"\x1b", now=0.0), [])
		self.assertEqual(self.translator.expire(ESCAPE_TIMEOUT_SECONDS / 2), [])
		self.assertEqual(self.translator.expire(ESCAPE_TIMEOUT_SECONDS), ["esc"])
		self.assertEqual(self.translator.expire(ESCAPE_TIMEOUT_SECONDS * 2), [])

	def test_escape_sequence_split_across_reads(self):
		self.assertEqual(self.feed(b"\x1b"), [])
		self.assertEqual(self.feed(b"["), [])
		self.assertEqual(self.feed(b"A"), ["up"])

	def test_escape_then_printable_is_alt(self):
		self.assertEqual(self.feed(b"\x1bf\x1bB"), ["alt+f", "alt+shift+b"])

	def test_double_escape(self):
		self.assertEqual(self.feed(b"\x1b\x1b[A"), ["esc", "up"])

	def test_unknown_sequence_is_dropped(self):
		self.assertEqual(self.feed(b"\x1b[99~x"), ["x"])

	def test_non_ascii_bytes_are_dropped(self):
		self.assertEqual(self.feed("é".encode() + b"a"), ["a"])

	def test_double_ctrl_bracket_detaches(self):
		self.assertEqual(self.feed(DETACH_BYTE, now=0.0), [])
		self.assertFalse(self.translator.detach_requested)
		self.assertEqual(self.feed(DETACH_BYTE, now=DETACH_WINDOW_SECONDS / 2), [])
		self.assertTrue(self.translator.detach_requested)

	def test_single_ctrl_bracket_is_sent_after_window(self):
		self.feed(DETACH_BYTE, now=0.0)
		self.assertEqual(self.translator.expire(DETACH_WINDOW_SECONDS + 0.01), ["ctrl+bracket_right"])
		self.assertFalse(self.translator.detach_requested)

	def test_slow_second_ctrl_bracket_does_not_detach(self):
		self.feed(DETACH_BYTE, now=0.0)
		self.assertEqual(self.feed(DETACH_BYTE, now=DETACH_WINDOW_SECONDS + 0.5), ["ctrl+bracket_right"])
		self.assertFalse(self.translator.detach_requested)

	def test_ctrl_bracket_followed_by_other_key_is_sent(self):
		self.feed(DETACH_BYTE, now=0.0)
		self.assertEqual(self.feed(b"a", now=0.1), ["ctrl+bracket_right", "a"])


class AttachLoopTests(unittest.TestCase):
	def test_sends_combos_until_detach(self):
		chunks = [b"hi", None, b"\x1d\x1d", b"never read"]
		times = iter([0.0, 0.1, 0.2, 0.3])
		sent = []

		def read_chunk():
			return chunks.pop(0)

		run_attach_loop(read_chunk, sent.extend, clock=lambda: next(times))
		self.assertEqual(sent, ["h", "i"])
		self.assertEqual(chunks, [b"never read"])

	def test_timeout_reads_flush_lone_escape(self):
		chunks = [b"\x1b", None, b"\x1d\x1d"]
		times = iter([0.0, 1.0, 1.1])
		sent = []
		run_attach_loop(lambda: chunks.pop(0), sent.extend, clock=lambda: next(times))
		self.assertEqual(sent, ["esc"])

	def test_end_of_input_stops_loop(self):
		chunks = [b"a", b""]
		sent = []
		run_attach_loop(lambda: chunks.pop(0), sent.extend, clock=lambda: 0.0)
		self.assertEqual(sent, ["a"])



class RecordingClient:
	def __init__(self):
		self.commands = []

	def execute(self, command, arguments=None):
		self.commands.append((command, arguments))
		return {}


class AttachTerminalTests(unittest.TestCase):
	def test_attach_on_pseudo_terminal_restores_mode(self):
		controller, terminal = pty.openpty()
		self.addCleanup(os.close, controller)
		self.addCleanup(os.close, terminal)
		original_attributes = termios.tcgetattr(terminal)
		real_setraw = tty.setraw

		def setraw_then_type(file_descriptor):
			real_setraw(file_descriptor)
			os.write(controller, b"ok\x1d\x1d")

		client = RecordingClient()
		with mock.patch("sys.stderr", io.StringIO()), mock.patch("wn.attach.tty.setraw", setraw_then_type):
			attach(client, delay_ms=0, file_descriptor=terminal)
		self.assertEqual(client.commands, [("input-send-event", arguments) for arguments in combo_to_input_events("o") + combo_to_input_events("k")])
		restored_local_flags = termios.tcgetattr(terminal)[LOCAL_FLAGS_INDEX]
		self.assertEqual(restored_local_flags & CANONICAL_ECHO_FLAGS, original_attributes[LOCAL_FLAGS_INDEX] & CANONICAL_ECHO_FLAGS)

	def test_attach_refuses_non_terminal(self):
		read_end, write_end = os.pipe()
		self.addCleanup(os.close, read_end)
		self.addCleanup(os.close, write_end)
		with self.assertRaises(WnError):
			attach(RecordingClient(), delay_ms=0, file_descriptor=read_end)


if __name__ == "__main__":
	unittest.main()
