"""Run PowerShell scripts as SYSTEM in the guest, move small files in and out, and offer large files over HTTP."""

import base64
import collections
import http
import http.server
import json
import os
import secrets
import shutil
import sys
import threading
import time

from wn.backend import BackendError
from wn.backends.utm import UnknownVMError, UTMCtlMissingError
from wn.errors import WnError
from wn.shares import GUEST_ROOT, HOST_ADDRESS

EXEC_DIRECTORY = GUEST_ROOT + "\\exec"
READY_FILE = EXEC_DIRECTORY + "\\ready-{}.txt"
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
READY_COMMAND = ("powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand")
READY_VALUES = "$Root = '{root}'\n$Exec = '{exec}'\n$Ready = '{ready}'\n$Token = '{token}'\n$Sids = '{system}', '{administrators}'\n"
READY_SCRIPT = """$ErrorActionPreference = 'Stop'
function New-Security {
	$security = [Security.AccessControl.DirectorySecurity]::new()
	$security.SetAccessRuleProtection($true, $false)
	foreach ($sid in $Sids) {
		$security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid), 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow'))
	}
	$security
}
function Get-Rules($Security) {
	$Security.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object { '{0} {1} {2} {3} {4} {5}' -f $_.IdentityReference, $_.AccessControlType, $_.FileSystemRights, $_.InheritanceFlags, $_.PropagationFlags, $_.IsInherited } | Sort-Object
}
function Test-Protected {
	$security = [IO.Directory]::GetAccessControl($Root)
	$security.AreAccessRulesProtected -and ((Get-Rules $security) -join ';') -eq ((Get-Rules (New-Security)) -join ';')
}
function Assert-Folder($Path) {
	$attributes = [IO.File]::GetAttributes($Path)
	if (-not $attributes.HasFlag([IO.FileAttributes]::Directory) -or $attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { throw "$Path is not a plain folder" }
	$owner = [IO.Directory]::GetAccessControl($Path).GetOwner([Security.Principal.SecurityIdentifier])
	if ($Sids -notcontains $owner.Value) { throw "$Path has an unexpected owner" }
}
try {
	if (-not [IO.Directory]::Exists($Root)) {
		$security = New-Security
		$security.SetOwner([Security.Principal.SecurityIdentifier]::new($Sids[0]))
		[IO.Directory]::CreateDirectory($Root, $security) | Out-Null
	}
	Assert-Folder $Root
	if (-not (Test-Protected)) { [IO.Directory]::SetAccessControl($Root, (New-Security)) }
	if (-not (Test-Protected)) { throw "$Root is not protected" }
	Assert-Folder $Root
	[IO.Directory]::CreateDirectory($Exec) | Out-Null
	Assert-Folder $Exec
	[IO.File]::WriteAllText($Ready, $Token)
} catch {
	exit 1
}
"""
READY_TIMEOUT_SECONDS = 30
POLL_SECONDS = 1
OUTPUT_READ_ATTEMPTS = 3
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
OFFER_TOKEN_BYTES = 32
OFFER_CHUNK_BYTES = 1024 * 1024
OFFER_POLL_SECONDS = 0.05

ExecPaths = collections.namedtuple("ExecPaths", "script wrapper output done pattern")


class GuestError(WnError):
	"""Raised when a guest command cannot run or gives no usable result."""


def output_tail(output):
	return "\n".join(output.splitlines()[-ERROR_TAIL_LINES:])


def clean_text(text):
	return text.lstrip(BOM_TEXT).replace("\r", "")


def exec_paths(script_id):
	base = "{}\\{}".format(EXEC_DIRECTORY, script_id)
	return ExecPaths(base + ".ps1", base + ".cmd", base + ".out", base + ".done", base + ".*")


def render_powershell(script, params=None):
	params_json = json.dumps({} if params is None else params, ensure_ascii=True).replace("'", "''")
	text = POWERSHELL_PREAMBLE + POWERSHELL_PARAMS_LINE.format(params_json) + POWERSHELL_TRAP + script
	return UTF8_BOM + text.encode("utf-8")


def render_wrapper(paths, token):
	lines = [line.format(script=paths.script, output=paths.output, token=token, done=paths.done) for line in WRAPPER_LINES]
	return ("\r\n".join(lines) + "\r\n").encode("ascii")


def encode_ready_script(token, ready_file):
	values = READY_VALUES.format(root=GUEST_ROOT, exec=EXEC_DIRECTORY, ready=ready_file, token=token, system=SYSTEM_SID, administrators=ADMINISTRATORS_SID)
	return base64.b64encode((values + READY_SCRIPT).encode("utf-16-le")).decode("ascii")


class GuestRunner:
	"""Runs PowerShell scripts as SYSTEM through utmctl exec, and reads their output and exit code from files."""

	def __init__(self, backend, clock=time.monotonic, sleep=time.sleep, new_id=secrets.token_hex):
		self.backend = backend
		self.clock = clock
		self.sleep = sleep
		self.new_id = new_id
		self._ready = False

	def ensure_ready(self):
		"""Create the exec folder, or check that it is a plain folder locked to SYSTEM and Administrators, before anything is pushed into it."""
		if self._ready:
			return
		token = self.new_id(TOKEN_BYTES)
		ready_file = READY_FILE.format(token)
		self.backend.exec_command(*READY_COMMAND, encode_ready_script(token, ready_file))
		deadline = self.clock() + READY_TIMEOUT_SECONDS
		while self._read_text(ready_file).strip() != token:
			if self.clock() >= deadline:
				raise GuestError("Windows did not confirm {} within {} s. Either the UTM guest tools do not run in Windows, or {} exists with unexpected ownership or type. Check the guest tools, or inspect or delete {} in Windows, and run the command again".format(EXEC_DIRECTORY, READY_TIMEOUT_SECONDS, GUEST_ROOT, GUEST_ROOT))
			self.sleep(POLL_SECONDS)
		self._delete(ready_file)
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
				output = self._read_text(paths.output)
				try:
					self._delete(paths.pattern)
				except BackendError:
					pass
				raise GuestError("{} did not finish in Windows within {} s. Output so far:\n{}".format(label, timeout, output_tail(output)))
			self.sleep(POLL_SECONDS)
			exit_code = self._exit_code(paths.done, token)
		output, read_error = self._read_output(paths.output)
		self._clean_up(paths.pattern)
		if read_error is not None:
			raise GuestError("{} finished in Windows with exit code {}, but wn could not read its output: {}".format(label, exit_code, read_error))
		if exit_code != 0:
			raise GuestError("{} failed in Windows with exit code {}:\n{}".format(label, exit_code, output_tail(output)))
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
		return clean_text(text)

	def _read_output(self, guest_path):
		"""Return the output file's text and None, or None and the last error when it cannot be read in OUTPUT_READ_ATTEMPTS tries."""
		for attempt in range(OUTPUT_READ_ATTEMPTS):
			if attempt:
				self.sleep(POLL_SECONDS)
			try:
				return clean_text(self.backend.pull_file(guest_path)), None
			except (UnknownVMError, UTMCtlMissingError):
				raise
			except BackendError as error:
				read_error = error
		return None, read_error

	def _delete(self, pattern):
		self.backend.exec_command("cmd.exe", "/c", "del /q {}".format(pattern))

	def _clean_up(self, pattern):
		"""Delete a finished run's files. A failure here does not change the run's result, unless the VM is gone."""
		try:
			self._delete(pattern)
		except (UnknownVMError, UTMCtlMissingError):
			raise
		except BackendError:
			pass

	def _exit_code(self, done_path, token):
		fields = self._read_text(done_path).split()
		if len(fields) != DONE_FIELD_COUNT or fields[1] != token:
			return None
		try:
			return int(fields[0])
		except ValueError:
			return None


class OfferServer(http.server.ThreadingHTTPServer):
	"""Prints no traceback when a client drops its connection."""

	def handle_error(self, request, client_address):
		if not isinstance(sys.exc_info()[1], ConnectionError):
			super().handle_error(request, client_address)


class FileOffer:
	"""Serves a fixed set of files over HTTP under a random token until the block ends."""

	def __init__(self, files, host=HOST_ADDRESS, new_token=secrets.token_urlsafe):
		self.files = dict(files)
		self.host = host
		self.token = new_token(OFFER_TOKEN_BYTES)
		self.server = None
		self.thread = None

	def __enter__(self):
		offer = self

		class Handler(http.server.BaseHTTPRequestHandler):
			def do_GET(self):
				offer._serve(self)

			def log_message(self, format, *arguments):
				pass

		self.server = OfferServer((self.host, 0), Handler)
		self.thread = threading.Thread(target=self.server.serve_forever, args=(OFFER_POLL_SECONDS,), daemon=True)
		self.thread.start()
		return self

	def __exit__(self, *exc_info):
		self.server.shutdown()
		self.server.server_close()
		self.thread.join()

	def url(self, name):
		return "http://{}:{}/{}/{}".format(self.host, self.server.server_address[1], self.token, name)

	def _serve(self, request):
		token, _, name = request.path[1:].partition("/")
		path = self.files.get(name) if secrets.compare_digest(token.encode(), self.token.encode()) else None
		if path is None:
			request.send_error(http.HTTPStatus.NOT_FOUND)
			return
		request.send_response(http.HTTPStatus.OK)
		request.send_header("Content-Type", "application/octet-stream")
		request.send_header("Content-Length", str(os.path.getsize(path)))
		request.end_headers()
		with open(path, "rb") as source:
			shutil.copyfileobj(source, request.wfile, OFFER_CHUNK_BYTES)
