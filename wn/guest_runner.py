"""Run PowerShell scripts as SYSTEM in the guest, move small files in and out, and offer large files over HTTP."""

import collections
import http
import http.server
import json
import os
import secrets
import shutil
import threading
import time

from wn.backend import BackendError
from wn.backends.utm import UnknownVMError, UTMCtlMissingError
from wn.errors import WnError
from wn.shares import GUEST_ROOT, HOST_ADDRESS

EXEC_DIRECTORY = GUEST_ROOT + "\\exec"
READY_FILE = EXEC_DIRECTORY + "\\ready.txt"
SYSTEM_SID = "*S-1-5-18"
ADMINISTRATORS_SID = "*S-1-5-32-544"
READY_COMMAND = "mkdir {exec} 2>nul & icacls {root} /inheritance:r /grant:r {system}:(OI)(CI)F {administrators}:(OI)(CI)F >nul & echo {token} > {ready}"
READY_TIMEOUT_SECONDS = 30
POLL_SECONDS = 1
DEFAULT_SCRIPT_TIMEOUT_SECONDS = 120
DEFAULT_LABEL = "guest script"
MAX_PUSH_BYTES = 1024 * 1024
ERROR_TAIL_LINES = 20
ID_BYTES = 8
TOKEN_BYTES = 16
DONE_FIELD_COUNT = 2
RESULT_PREFIX = "wn-result: "
UTF8_BOM = b"\xef\xbb\xbf"
BOM_TEXT = "\ufeff"
POWERSHELL_PREAMBLE = (
	"$ErrorActionPreference = 'Stop'\r\n"
	"$ProgressPreference = 'SilentlyContinue'\r\n"
	"[Console]::OutputEncoding = [Text.Encoding]::UTF8\r\n"
)
POWERSHELL_PARAMS_LINE = "$Params = '{}' | ConvertFrom-Json\r\n"
POWERSHELL_TRAP = "trap { Write-Output ('wn-error: {0}' -f $_); exit 1 }\r\n"
WRAPPER_LINES = (
	"@echo off",
	"powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"{script}\" > \"{output}\" 2>&1",
	"echo %ERRORLEVEL% {token} > \"{done}\"",
)
OFFER_TIMEOUT_SECONDS = 300
OFFER_TOKEN_BYTES = 32
OFFER_CHUNK_BYTES = 1024 * 1024

ExecPaths = collections.namedtuple("ExecPaths", "script wrapper output done pattern")


class GuestError(WnError):
	"""Raised when a guest command cannot run or gives no usable result."""


class GuestScriptError(GuestError):
	"""Raised when a guest script exits with a code other than 0."""

	def __init__(self, label, exit_code, output):
		self.exit_code = exit_code
		self.output = output
		super().__init__("{} failed in Windows with exit code {}:\n{}".format(label, exit_code, output_tail(output)))


class GuestTimeoutError(GuestError):
	"""Raised when a guest script does not finish in time. ``output`` holds what it printed so far."""

	def __init__(self, label, timeout, output):
		self.output = output
		super().__init__("{} did not finish in Windows within {} s. Output so far:\n{}".format(label, timeout, output_tail(output)))


def output_tail(output):
	return "\n".join(output.splitlines()[-ERROR_TAIL_LINES:])


def exec_paths(script_id):
	base = "{}\\{}".format(EXEC_DIRECTORY, script_id)
	return ExecPaths(base + ".ps1", base + ".cmd", base + ".out", base + ".done", base + ".*")


def render_powershell(script, params=None):
	params_json = json.dumps({} if params is None else params).replace("'", "''")
	text = POWERSHELL_PREAMBLE + POWERSHELL_PARAMS_LINE.format(params_json) + POWERSHELL_TRAP + script
	return UTF8_BOM + text.encode("utf-8")


def render_wrapper(paths, token):
	lines = [line.format(script=paths.script, output=paths.output, token=token, done=paths.done) for line in WRAPPER_LINES]
	return ("\r\n".join(lines) + "\r\n").encode("ascii")


class GuestRunner:
	"""Runs PowerShell scripts as SYSTEM through utmctl exec, and reads their output and exit code from files."""

	def __init__(self, backend, clock=time.monotonic, sleep=time.sleep, new_id=secrets.token_hex):
		self.backend = backend
		self.clock = clock
		self.sleep = sleep
		self.new_id = new_id
		self._ready = False

	def ensure_ready(self):
		"""Create the exec folder and lock it to SYSTEM and Administrators before anything is pushed into it."""
		if self._ready:
			return
		token = self.new_id(TOKEN_BYTES)
		command = READY_COMMAND.format(exec=EXEC_DIRECTORY, root=GUEST_ROOT, system=SYSTEM_SID, administrators=ADMINISTRATORS_SID, token=token, ready=READY_FILE)
		self.backend.exec_command("cmd.exe", "/c", command)
		deadline = self.clock() + READY_TIMEOUT_SECONDS
		while self._read_text(READY_FILE).strip() != token:
			if self.clock() >= deadline:
				raise GuestError("Windows did not create {} within {} s. Check that the UTM guest tools run in Windows".format(EXEC_DIRECTORY, READY_TIMEOUT_SECONDS))
			self.sleep(POLL_SECONDS)
		self._ready = True

	def run(self, script, params=None, timeout=DEFAULT_SCRIPT_TIMEOUT_SECONDS, label=DEFAULT_LABEL):
		"""Run ``script`` with ``$Params`` set from ``params`` and return what it printed."""
		self.ensure_ready()
		paths = exec_paths(self.new_id(ID_BYTES))
		token = self.new_id(TOKEN_BYTES)
		self.push(paths.script, render_powershell(script, params))
		self.push(paths.wrapper, render_wrapper(paths, token))
		self.backend.exec_command("cmd.exe", "/c", paths.wrapper)
		deadline = self.clock() + timeout
		exit_code = self._exit_code(paths.done, token)
		while exit_code is None:
			if self.clock() >= deadline:
				raise GuestTimeoutError(label, timeout, self._read_text(paths.output))
			self.sleep(POLL_SECONDS)
			exit_code = self._exit_code(paths.done, token)
		output = self._read_text(paths.output)
		self.backend.exec_command("cmd.exe", "/c", "del /q {}".format(paths.pattern))
		if exit_code != 0:
			raise GuestScriptError(label, exit_code, output)
		return output

	def run_json(self, script, params=None, timeout=DEFAULT_SCRIPT_TIMEOUT_SECONDS, label=DEFAULT_LABEL):
		"""Run ``script`` and return the object on its last ``wn-result:`` line."""
		output = self.run(script, params, timeout, label)
		for line in reversed(output.splitlines()):
			if line.startswith(RESULT_PREFIX):
				try:
					return json.loads(line[len(RESULT_PREFIX):])
				except ValueError:
					break
		raise GuestError("{} printed no result in Windows:\n{}".format(label, output_tail(output)))

	def push(self, guest_path, data):
		if len(data) > MAX_PUSH_BYTES:
			raise GuestError("refusing to push {} bytes to {}. utmctl file push is only safe up to {} bytes".format(len(data), guest_path, MAX_PUSH_BYTES))
		self.ensure_ready()
		self.backend.push_file(guest_path, data)

	def pull(self, guest_path):
		return self.backend.pull_file(guest_path)

	def _read_text(self, guest_path):
		"""Return the file's text without a BOM or carriage returns, or an empty string when it cannot be read yet."""
		try:
			text = self.backend.pull_file(guest_path)
		except (UnknownVMError, UTMCtlMissingError):
			raise
		except BackendError:
			return ""
		return text.lstrip(BOM_TEXT).replace("\r", "")

	def _exit_code(self, done_path, token):
		fields = self._read_text(done_path).split()
		if len(fields) != DONE_FIELD_COUNT or fields[1] != token:
			return None
		try:
			return int(fields[0])
		except ValueError:
			return None


class FileOffer:
	"""Serves a fixed set of files over HTTP under a random token until the block ends or the timeout passes."""

	def __init__(self, files, host=HOST_ADDRESS, timeout=OFFER_TIMEOUT_SECONDS, new_token=secrets.token_urlsafe):
		self.files = dict(files)
		self.host = host
		self.timeout = timeout
		self.token = new_token(OFFER_TOKEN_BYTES)
		self.server = None
		self.thread = None
		self.timer = None
		self.lock = threading.Lock()
		self.stopped = False

	def __enter__(self):
		offer = self

		class Handler(http.server.BaseHTTPRequestHandler):
			def do_GET(self):
				offer._serve(self)

			def log_message(self, format, *arguments):
				pass

		self.server = http.server.ThreadingHTTPServer((self.host, 0), Handler)
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()
		self.timer = threading.Timer(self.timeout, self._stop)
		self.timer.daemon = True
		self.timer.start()
		return self

	def __exit__(self, *exc_info):
		self.timer.cancel()
		self.timer.join()
		self._stop()
		self.thread.join()

	def url(self, name):
		return "http://{}:{}/{}/{}".format(self.host, self.server.server_address[1], self.token, name)

	def _stop(self):
		with self.lock:
			if self.stopped:
				return
			self.stopped = True
		self.server.shutdown()
		self.server.server_close()

	def _serve(self, request):
		prefix = "/{}/".format(self.token)
		path = self.files.get(request.path[len(prefix):]) if request.path.startswith(prefix) else None
		if path is None:
			request.send_error(http.HTTPStatus.NOT_FOUND)
			return
		request.send_response(http.HTTPStatus.OK)
		request.send_header("Content-Type", "application/octet-stream")
		request.send_header("Content-Length", str(os.path.getsize(path)))
		request.end_headers()
		with open(path, "rb") as source:
			shutil.copyfileobj(source, request.wfile, OFFER_CHUNK_BYTES)
