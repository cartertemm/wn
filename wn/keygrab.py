"""Grab the whole Mac keyboard and send every key press to the guest.

``wn grab`` runs the ``wn-keygrab`` helper from ``keygrab/``. The helper reads
the keyboard through a macOS event tap and prints one JSON line per key down
or key up. This module turns those lines into QMP ``input-send-event``
commands, so the guest gets real key downs and ups: the Windows key (Command),
ctrl+alt+del, and keys held down all work.

The helper starts grabbed. Press its release hotkey (Control+Option+Command by
default) to give the keyboard back to macOS, and again to grab it.
"""

import json
import os
import signal
import subprocess

from wn.errors import WnError
from wn.keys import key_event

PACKAGE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
KEYGRAB_DIRECTORY = os.path.join(os.path.dirname(PACKAGE_DIRECTORY), "keygrab")
DEFAULT_RELEASE_HOTKEY = "control,option,command"
QUIT_TIMEOUT_SECONDS = 2.0
EXIT_NO_PERMISSION = 2
EXIT_USAGE = 64

PERMISSION_INSTRUCTIONS = (
	"wn-keygrab needs Accessibility permission to read the keyboard. "
	"Open System Settings > Privacy & Security > Accessibility and turn it on for your terminal application, "
	"and for {helper} if it is in the list. After a rebuild of the helper, turn its entry off and on again, "
	"because the permission follows the signed binary."
)


def default_keycodes_path():
	return os.path.join(KEYGRAB_DIRECTORY, "keycodes.json")


def default_helper_path():
	return os.path.join(KEYGRAB_DIRECTORY, "bin", "wn-keygrab")


def load_keycode_table(path):
	"""Return a dict from macOS virtual keycode to QEMU qcode name."""
	try:
		with open(path) as table_file:
			keycodes = json.load(table_file)["keycodes"]
		return {int(keycode): qcode for keycode, qcode in keycodes.items()}
	except OSError as error:
		raise WnError("cannot read keycode table {}: {}".format(path, error)) from None
	except (ValueError, KeyError, TypeError, AttributeError):
		raise WnError("{} is not a keycode table like keygrab/keycodes.json".format(path)) from None


class KeyEventTranslator:
	"""Turns helper JSON lines into ``input-send-event`` arguments.

	It remembers which keys the guest holds down, so it can release all of them
	when the grab turns off or keys may have been lost. An up for a key that is
	not held is dropped, so the helper's own synthetic ups do no harm.
	"""

	def __init__(self, keycode_table, send_repeats=True):
		self.keycode_table = keycode_table
		self.send_repeats = send_repeats
		self.dropped_count = 0
		self._held_qcodes = {}

	def translate(self, line):
		"""Return a list of ``input-send-event`` arguments for one parsed helper line."""
		line_type = line.get("type")
		if line_type in ("down", "up"):
			return self._translate_key(line, line_type == "down")
		if line_type == "tap" or (line_type == "grab" and not line.get("active")):
			return self.release_all()
		return []

	def release_all(self):
		"""Return the arguments that release every held key, last pressed first."""
		qcodes = list(reversed(list(self._held_qcodes)))
		self._held_qcodes.clear()
		if not qcodes:
			return []
		return [{"events": [key_event(qcode, False) for qcode in qcodes]}]

	def _translate_key(self, line, down):
		qcode = self.keycode_table.get(line.get("keycode"))
		if qcode is None:
			self.dropped_count += 1
			return []
		if down:
			if line.get("repeat") and not self.send_repeats:
				return []
			self._held_qcodes[qcode] = True
		elif qcode not in self._held_qcodes:
			return []
		else:
			del self._held_qcodes[qcode]
		return [{"events": [key_event(qcode, down)]}]


def _raise_keyboard_interrupt(signal_number, frame):
	raise KeyboardInterrupt


def _start_helper(helper_path, release_hotkey, start_released):
	command = [helper_path, "--release-hotkey", release_hotkey]
	if start_released:
		command.append("--start-released")
	try:
		return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, universal_newlines=True, bufsize=1)
	except OSError as error:
		raise WnError("cannot run {}: {}. Build it with keygrab/build.sh".format(helper_path, error)) from None


def _stop_helper(process):
	"""Ask the helper to quit, terminate it if it does not, and return True if it was terminated."""
	terminated = False
	if process.poll() is None:
		try:
			process.stdin.write("quit\n")
			process.stdin.flush()
		except OSError:
			pass
		try:
			process.wait(QUIT_TIMEOUT_SECONDS)
		except subprocess.TimeoutExpired:
			process.terminate()
			process.wait()
			terminated = True
	for stream in (process.stdin, process.stdout):
		try:
			stream.close()
		except OSError:
			pass
	return terminated


def _parse_line(text):
	try:
		line = json.loads(text)
	except ValueError:
		return None
	return line if isinstance(line, dict) else None


def _status_message(line, hotkey_name):
	line_type = line.get("type")
	if line_type == "grab":
		if line.get("active"):
			return "grab on. Press {} to release the keyboard.".format(hotkey_name)
		return "grab off. Press {} to grab the keyboard again.".format(hotkey_name)
	if line_type == "tap":
		return "keys may have been lost (event tap {}: {}). Released all held keys.".format(line.get("state"), line.get("reason"))
	if line_type == "error" and line.get("reason") == "accessibility-permission":
		return "permission needed: wn-keygrab has no Accessibility permission."
	return None


def _forward_lines(process, translator, qmp_client, hotkey_name, stdout):
	for text in iter(process.stdout.readline, ""):
		line = _parse_line(text)
		if line is None:
			continue
		for arguments in translator.translate(line):
			qmp_client.execute("input-send-event", arguments)
		message = _status_message(line, hotkey_name)
		if message:
			stdout.write(message + "\n")
			stdout.flush()


def _check_exit_code(exit_code, helper_path, release_hotkey, terminated):
	if exit_code == EXIT_NO_PERMISSION:
		raise WnError(PERMISSION_INSTRUCTIONS.format(helper=helper_path))
	if exit_code == EXIT_USAGE:
		raise WnError("wn-keygrab rejected its arguments. Check the release hotkey '{}'".format(release_hotkey))
	if exit_code > 0:
		raise WnError("wn-keygrab exited with code {}".format(exit_code))
	if exit_code < 0 and not terminated:
		raise WnError("wn-keygrab was stopped by signal {}".format(-exit_code))


def run_grab(qmp_client, helper_path, keycode_table, release_hotkey, start_released, stdout, send_repeats=True):
	"""Run the helper and send its key events to the guest until it exits, ctrl+c, or SIGTERM."""
	translator = KeyEventTranslator(keycode_table, send_repeats)
	hotkey_name = release_hotkey.replace(",", "+")
	process = _start_helper(helper_path, release_hotkey, start_released)
	previous_sigterm_handler = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
	try:
		_forward_lines(process, translator, qmp_client, hotkey_name, stdout)
	except KeyboardInterrupt:
		pass
	finally:
		signal.signal(signal.SIGTERM, previous_sigterm_handler)
		terminated = _stop_helper(process)
	for arguments in translator.release_all():
		qmp_client.execute("input-send-event", arguments)
	if translator.dropped_count:
		stdout.write("dropped {} key events with no QEMU key.\n".format(translator.dropped_count))
	_check_exit_code(process.returncode, helper_path, release_hotkey, terminated)
