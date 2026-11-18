import base64
import http.server
import io
import itertools
import os
import re
import socket
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

from fakes import FakeBackend
from wn.backends.utm import UTMCtlError, UnknownVMError
from wn.guest_runner import (
	EXEC_DIRECTORY,
	MAX_PUSH_BYTES,
	WRAPPER_LINES,
	FileOffer,
	GuestError,
	GuestRunner,
	GuestScriptError,
	GuestTimeoutError,
	OfferServer,
)

READY_ARGUMENTS = ("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand")
READY_VALUE = re.compile(r"^\$(Token|Ready) = '([^']*)'$", re.MULTILINE)
READY_TOKEN = "{:032x}".format(0)
READY_FILE = "{}\\ready-{}.txt".format(EXEC_DIRECTORY, READY_TOKEN)
READY_ENDING = "\t[IO.File]::WriteAllText($Ready, $Token)\n} catch {\n\texit 1\n}\n"
GROUP_NAMES = ("SYSTEM", "Administrators", "NT AUTHORITY", "BUILTIN")
POWERSHELL_QUOTES = "\u2018\u2019\u201a\u201b"
DONE_LINE = re.compile(r'echo %ERRORLEVEL% (\w+) > "([^"]+)"')
LOCALHOST = "127.0.0.1"
FIRST_ID = "{:016x}".format(1)
SCRIPT_TOKEN = "{:032x}".format(2)
FILE_CONTENT = b"binary content"
OFFER_WAIT_SECONDS = 0.5
SHORT_OFFER_TIMEOUT = 0.2
RACE_OFFER_TIMEOUT = 0.001
SLOW_CLOSE_SECONDS = 0.2
SCRIPT_TIMEOUT = 10
ERROR_LINE_COUNT = 30
LARGE_FILE_BYTES = 64 * 1024 * 1024
SMALL_RECEIVE_BUFFER = 4096
ERROR_WAIT_SECONDS = 10


class FakeClock:
	def __init__(self):
		self.now = 0.0

	def __call__(self):
		return self.now

	def sleep(self, seconds):
		self.now += seconds


class FakeGuest(FakeBackend):
	"""Answers the ready command and runs nothing: each .cmd finishes after ``polls_until_done`` pulls of its .done file."""

	def __init__(self, exit_code=0, output="", polls_until_done=0, done_token=None):
		super().__init__()
		self.exit_code = exit_code
		self.output = output
		self.polls_until_done = polls_until_done
		self.done_token = done_token
		self.on_exec = self.handle_exec
		self.pending = {}

	def handle_exec(self, argv):
		command = argv[-1]
		if argv[:-1] == READY_ARGUMENTS:
			values = dict(READY_VALUE.findall(decode_ready_script(command)))
			self.files[values["Ready"]] = values["Token"]
		elif command.endswith(".cmd"):
			token, done = DONE_LINE.search(self.files[command]).groups()
			self.pending[done] = (self.polls_until_done, self.done_token or token, command[:-len(".cmd")] + ".out")

	def pull_file(self, guest_path):
		if guest_path in self.pending:
			remaining, token, output_path = self.pending[guest_path]
			if remaining == 0:
				del self.pending[guest_path]
				self.files[guest_path] = "{} {} \r\n".format(self.exit_code, token)
				self.files[output_path] = self.output
			else:
				self.pending[guest_path] = (remaining - 1, token, output_path)
		return super().pull_file(guest_path)


class SilentGuest(FakeGuest):
	def handle_exec(self, argv):
		pass


class UnknownVMGuest(FakeGuest):
	def pull_file(self, guest_path):
		raise UnknownVMError("UTM has no VM named 'Windows'")


class FailingDeleteGuest(FakeGuest):
	def handle_exec(self, argv):
		if argv[-1].startswith("del ") and argv[-1].endswith(".*"):
			raise UTMCtlError("utmctl exec failed with exit code 1")
		super().handle_exec(argv)


def decode_ready_script(encoded):
	return base64.b64decode(encoded).decode("utf-16-le")


def ready_script(guest):
	call = next(call for call in guest.calls if call[1:-1] == READY_ARGUMENTS)
	return decode_ready_script(call[-1])


def counting_id():
	counter = itertools.count()
	return lambda size: "{:0{}x}".format(next(counter), size * 2)


def make_runner(guest, clock=None):
	clock = clock or FakeClock()
	return GuestRunner(guest, clock=clock, sleep=clock.sleep, new_id=counting_id())


def pushes(guest):
	return [call for call in guest.calls if call[0] == "push_file"]


class GuestRunnerTests(unittest.TestCase):
	def run_script(self, guest, script="Write-Output hi", params=None, **arguments):
		return make_runner(guest).run(script, params, **arguments)

	def test_ready_step_runs_before_any_push(self):
		guest = FakeGuest()
		self.run_script(guest)
		first_exec = next(index for index, call in enumerate(guest.calls) if call[0] == "exec_command")
		first_push = next(index for index, call in enumerate(guest.calls) if call[0] == "push_file")
		self.assertEqual(guest.calls[first_exec][1:-1], READY_ARGUMENTS)
		self.assertLess(first_exec, first_push)

	def test_ready_script_uses_sids_and_no_group_names(self):
		guest = FakeGuest()
		self.run_script(guest)
		script = ready_script(guest)
		self.assertIn("$Sids = 'S-1-5-18', 'S-1-5-32-544'\n", script)
		for name in GROUP_NAMES:
			self.assertNotIn(name, script)
		self.assertTrue(script.isascii())

	def test_ready_script_values_come_from_wn(self):
		guest = FakeGuest()
		self.run_script(guest)
		script = ready_script(guest)
		self.assertIn("$Root = 'C:\\ProgramData\\wn'\n", script)
		self.assertIn("$Exec = 'C:\\ProgramData\\wn\\exec'\n", script)
		self.assertIn("$Ready = '{}'\n".format(READY_FILE), script)
		self.assertIn("$Token = '{}'\n".format(READY_TOKEN), script)

	def test_ready_script_creates_the_root_protected_and_checks_both_folders(self):
		guest = FakeGuest()
		self.run_script(guest)
		script = ready_script(guest)
		for part in ("SetAccessRuleProtection($true, $false)", "AreAccessRulesProtected", "ReparsePoint", "GetOwner(", "'FullControl'", "'ContainerInherit, ObjectInherit'", "[IO.Directory]::CreateDirectory($Root, $security)", "Assert-Folder $Root", "Assert-Folder $Exec"):
			self.assertIn(part, script)
		for part in ("icacls", "Recurse", "/T"):
			self.assertNotIn(part, script)

	def test_ready_token_is_written_last_and_only_on_success(self):
		guest = FakeGuest()
		self.run_script(guest)
		script = ready_script(guest)
		self.assertTrue(script.endswith(READY_ENDING))
		self.assertEqual(script.count("$Ready"), 2)
		self.assertLess(script.index("$ErrorActionPreference = 'Stop'"), script.index("try {"))

	def test_ready_file_is_removed_after_it_is_read(self):
		guest = FakeGuest()
		make_runner(guest).ensure_ready()
		self.assertEqual(guest.calls[-2], ("pull_file", READY_FILE))
		self.assertEqual(guest.calls[-1], ("exec_command", "cmd.exe", "/c", "del /q " + READY_FILE))

	def test_ready_waits_for_this_runners_token(self):
		guest = SilentGuest()
		guest.files[READY_FILE] = "old"
		clock = FakeClock()
		with self.assertRaises(GuestError) as caught:
			make_runner(guest, clock).run("Write-Output hi")
		self.assertIn("exists with unexpected ownership or type", str(caught.exception))
		self.assertIn("inspect or delete C:\\ProgramData\\wn in Windows", str(caught.exception))
		self.assertGreaterEqual(clock.now, 30)
		self.assertEqual(pushes(guest), [])

	def test_script_has_a_bom_and_the_preamble(self):
		guest = FakeGuest()
		self.run_script(guest, script="Write-Output body\r\n")
		text = guest.files["{}\\{}.ps1".format(EXEC_DIRECTORY, FIRST_ID)]
		self.assertTrue(text.startswith("\ufeff$ErrorActionPreference = 'Stop'\r\n"))
		positions = [text.index(part) for part in ("$ProgressPreference", "OutputEncoding", "$Params", "trap", "Write-Output body")]
		self.assertEqual(positions, sorted(positions))

	def test_single_quotes_in_params_are_doubled(self):
		guest = FakeGuest()
		self.run_script(guest, params={"path": "C:\\it's"})
		text = guest.files["{}\\{}.ps1".format(EXEC_DIRECTORY, FIRST_ID)]
		self.assertIn("$Params = '{\"path\": \"C:\\\\it''s\"}' | ConvertFrom-Json\r\n", text)

	def test_params_hold_no_raw_powershell_quotes_or_newlines(self):
		guest = FakeGuest()
		self.run_script(guest, params={"value": POWERSHELL_QUOTES + "'\n"})
		text = guest.files["{}\\{}.ps1".format(EXEC_DIRECTORY, FIRST_ID)]
		params = text[text.index("$Params = '") + len("$Params = '"):text.index("' | ConvertFrom-Json\r\n")]
		for character in POWERSHELL_QUOTES + "\r\n":
			self.assertNotIn(character, params)

	def test_wrapper_has_crlf_and_reads_errorlevel_on_its_own_line(self):
		guest = FakeGuest()
		self.run_script(guest)
		base = "{}\\{}".format(EXEC_DIRECTORY, FIRST_ID)
		lines = [line.format(script=base + ".ps1", output=base + ".out", token=SCRIPT_TOKEN, done=base + ".done") for line in WRAPPER_LINES]
		self.assertEqual(guest.files[base + ".cmd"], "\r\n".join(lines) + "\r\n")
		self.assertEqual(lines[-1], 'echo %ERRORLEVEL% {} > "{}.done"'.format(SCRIPT_TOKEN, base))

	def test_exit_code_zero_returns_the_output_without_carriage_returns(self):
		guest = FakeGuest(output="\ufeffone\r\ntwo\r\n")
		self.assertEqual(self.run_script(guest), "one\ntwo\n")

	def test_nonzero_exit_code_raises_with_the_last_20_lines(self):
		output = "".join("line {}\r\n".format(number) for number in range(1, ERROR_LINE_COUNT + 1))
		guest = FakeGuest(exit_code=3, output=output)
		with self.assertRaises(GuestScriptError) as caught:
			self.run_script(guest, label="x")
		self.assertEqual(caught.exception.exit_code, 3)
		message = str(caught.exception)
		self.assertTrue(message.startswith("x failed in Windows with exit code 3"))
		self.assertNotIn("line 10\n", message)
		self.assertIn("line 11\n", message)
		self.assertTrue(message.endswith("line 30"))

	def test_wrong_token_is_ignored(self):
		guest = FakeGuest(done_token="other")
		with self.assertRaises(GuestTimeoutError):
			self.run_script(guest, timeout=SCRIPT_TIMEOUT)

	def test_timeout_raises_with_partial_output(self):
		guest = FakeGuest(polls_until_done=1000)
		guest.files["{}\\{}.out".format(EXEC_DIRECTORY, FIRST_ID)] = "half done\r\n"
		clock = FakeClock()
		with self.assertRaises(GuestTimeoutError) as caught:
			make_runner(guest, clock).run("Write-Output hi", timeout=SCRIPT_TIMEOUT)
		self.assertEqual(caught.exception.output, "half done\n")
		self.assertGreaterEqual(clock.now, SCRIPT_TIMEOUT)
		self.assertEqual(guest.calls[-1], ("exec_command", "cmd.exe", "/c", "del /q C:\\ProgramData\\wn\\exec\\{}.*".format(FIRST_ID)))

	def test_timeout_is_raised_when_cleanup_fails(self):
		with self.assertRaises(GuestTimeoutError):
			self.run_script(FailingDeleteGuest(polls_until_done=1000), timeout=SCRIPT_TIMEOUT)

	def test_cleanup_deletes_the_exec_files(self):
		guest = FakeGuest()
		self.run_script(guest)
		executions = [call for call in guest.calls if call[0] == "exec_command"]
		self.assertEqual(executions[-1], ("exec_command", "cmd.exe", "/c", "del /q C:\\ProgramData\\wn\\exec\\{}.*".format(FIRST_ID)))

	def test_run_json_returns_the_last_result_line(self):
		guest = FakeGuest(output='wn-result: {"a": 1}\r\nnoise\r\nwn-result: {"a": 2}\r\n')
		self.assertEqual(make_runner(guest).run_json("x"), {"a": 2})

	def test_run_json_without_a_result_raises(self):
		guest = FakeGuest(output="nothing\r\n")
		with self.assertRaises(GuestError):
			make_runner(guest).run_json("x")

	def test_push_refuses_more_than_1_mb(self):
		guest = FakeGuest()
		with self.assertRaises(GuestError):
			make_runner(guest).push(EXEC_DIRECTORY + "\\big.bin", b"x" * (MAX_PUSH_BYTES + 1))
		self.assertEqual(pushes(guest), [])

	def test_push_readies_the_folder_first(self):
		guest = FakeGuest()
		make_runner(guest).push(EXEC_DIRECTORY + "\\1.secret", b"secret")
		self.assertEqual(guest.calls[0][0], "exec_command")
		self.assertEqual(guest.files[EXEC_DIRECTORY + "\\1.secret"], "secret")

	def test_unknown_vm_is_not_treated_as_not_done(self):
		with self.assertRaises(UnknownVMError):
			self.run_script(UnknownVMGuest())


class FileOfferTests(unittest.TestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.path = os.path.join(directory.name, "tool.zip")
		with open(self.path, "wb") as target:
			target.write(FILE_CONTENT)

	def offer(self, timeout=SCRIPT_TIMEOUT):
		return FileOffer({"tool.zip": self.path}, host=LOCALHOST, timeout=timeout)

	def assert_status(self, url, status):
		with self.assertRaises(urllib.error.HTTPError) as caught:
			urllib.request.urlopen(url)
		caught.exception.close()
		self.assertEqual(caught.exception.code, status)

	def assert_refused(self, url):
		with self.assertRaises((urllib.error.URLError, ConnectionError)):
			urllib.request.urlopen(url)

	def raw_request(self, offer, path, receive_buffer=None):
		connection = socket.socket()
		if receive_buffer:
			connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
		connection.connect(offer.server.server_address)
		connection.sendall(b"GET " + path + b" HTTP/1.0\r\n\r\n")
		return connection

	def test_offer_serves_mapped_names(self):
		with self.offer() as offer:
			with urllib.request.urlopen(offer.url("tool.zip")) as response:
				self.assertEqual(response.read(), FILE_CONTENT)

	def test_offer_answers_404_for_other_names(self):
		with self.offer() as offer:
			self.assert_status(offer.url("other.zip"), 404)
			self.assert_status(offer.url("../tool.zip"), 404)

	def test_offer_answers_404_for_a_wrong_token(self):
		with self.offer() as offer:
			self.assert_status(offer.url("tool.zip").replace(offer.token, "wrong"), 404)

	def test_offer_answers_404_for_a_token_with_other_characters(self):
		with self.offer() as offer:
			with self.raw_request(offer, b"/\xe9/tool.zip") as connection:
				self.assertTrue(connection.recv(len(b"HTTP/1.0 404")).endswith(b" 404"))

	def test_offer_is_quiet_when_the_client_drops_the_download(self):
		with open(self.path, "wb") as target:
			target.truncate(LARGE_FILE_BYTES)
		original = OfferServer.handle_error
		handled = threading.Event()

		def recording(server, request, client_address):
			original(server, request, client_address)
			handled.set()

		with mock.patch.object(OfferServer, "handle_error", recording), mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
			with self.offer() as offer:
				path = "/{}/tool.zip".format(offer.token).encode("ascii")
				with self.raw_request(offer, path, SMALL_RECEIVE_BUFFER) as connection:
					connection.recv(SMALL_RECEIVE_BUFFER)
				self.assertTrue(handled.wait(ERROR_WAIT_SECONDS))
		self.assertEqual(stderr.getvalue(), "")

	def test_offer_answers_only_get(self):
		with self.offer() as offer:
			self.assert_status(urllib.request.Request(offer.url("tool.zip"), data=b"x", method="POST"), 501)

	def test_offer_binds_only_the_given_host(self):
		with self.offer() as offer:
			self.assertEqual(offer.server.server_address[0], LOCALHOST)

	def test_offer_stops_after_the_timeout(self):
		with self.offer(timeout=SHORT_OFFER_TIMEOUT) as offer:
			threading.Event().wait(OFFER_WAIT_SECONDS)
			self.assert_refused(offer.url("tool.zip"))

	def test_offer_stops_at_the_end_of_the_block(self):
		with self.offer() as offer:
			url = offer.url("tool.zip")
		self.assert_refused(url)

	def test_offer_socket_is_closed_at_the_end_of_the_block_while_the_timeout_stops_it(self):
		original_close = http.server.ThreadingHTTPServer.server_close

		def slow_close(server):
			threading.Event().wait(SLOW_CLOSE_SECONDS)
			original_close(server)

		with mock.patch.object(http.server.ThreadingHTTPServer, "server_close", slow_close):
			with self.offer(timeout=RACE_OFFER_TIMEOUT) as offer:
				while not offer.stopped:
					threading.Event().wait(RACE_OFFER_TIMEOUT)
		self.assertEqual(offer.server.socket.fileno(), -1)


if __name__ == "__main__":
	unittest.main()
