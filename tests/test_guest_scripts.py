import os
import re
import unittest

SCRIPTS_DIRECTORY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wn", "guest", "shares")
RUNNER_SCRIPTS = ("prepare.ps1", "fetch.ps1", "task.ps1", "cleanup.ps1")
SUPERVISOR_SCRIPT = "wn-shares.ps1"
FORBIDDEN_FLAGS = ("--pass", "--links", "-L", "--rc", "--sftp-chunk-size")
QUOTED_FLAG = re.compile(r"'(--?[A-Za-z][\w-]*)'")
MEASURED_ARGUMENTS = (
	"'--vfs-cache-mode', 'writes'",
	"'--vfs-write-back', '5s'",
	"'--dir-cache-time', '1s'",
	"'--attr-timeout', '1s'",
	"'--file-perms', '0777'",
	"'--dir-perms', '0777'",
	"'-o', 'FileSecurity=D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;AU)'",
	"'-v'",
)
ADDED_ARGUMENTS = (
	"'--vfs-cache-max-size', '2G'",
	"'--vfs-cache-poll-interval', '10s'",
	"'--contimeout', '5s'",
	"'--timeout', '30s'",
	"'--volname', $Share.name",
	"$arguments += '--read-only'",
)


def read_script(name):
	with open(os.path.join(SCRIPTS_DIRECTORY, name), encoding="ascii") as script_file:
		return script_file.read()


class GuestScriptTextTests(unittest.TestCase):
	def test_scripts_are_ascii_with_empty_blank_lines(self):
		for name in RUNNER_SCRIPTS + (SUPERVISOR_SCRIPT,):
			for number, line in enumerate(read_script(name).split("\n"), start=1):
				self.assertFalse(line.strip() == "" and line != "", "{}:{} has an indented blank line".format(name, number))
				self.assertFalse(line.startswith(" "), "{}:{} is indented with spaces".format(name, number))

	def test_runner_scripts_print_one_result_line(self):
		for name in RUNNER_SCRIPTS:
			self.assertEqual(read_script(name).count("Write-Output ('wn-result: '"), 1, name)

	def test_runner_scripts_leave_the_preamble_to_the_runner(self):
		for name in RUNNER_SCRIPTS:
			self.assertNotIn("$ErrorActionPreference", read_script(name), name)


class MountArgumentTests(unittest.TestCase):
	def setUp(self):
		self.text = read_script(SUPERVISOR_SCRIPT)

	def test_mount_has_the_measured_arguments(self):
		for argument in MEASURED_ARGUMENTS:
			self.assertIn(argument, self.text)

	def test_mount_has_the_added_arguments(self):
		for argument in ADDED_ARGUMENTS:
			self.assertIn(argument, self.text)

	def test_mount_never_has_a_forbidden_flag(self):
		flags = QUOTED_FLAG.findall(self.text)
		for flag in FORBIDDEN_FLAGS:
			self.assertNotIn(flag, flags)
		self.assertEqual(self.text.count("'--vfs-write-back'"), 1)
		self.assertNotIn("'0s'", self.text)

	def test_mount_uses_the_arm64_binary_path_only(self):
		self.assertNotIn("PROCESSOR_ARCHITECTURE", self.text)
		self.assertIn("$Rclone = Join-Path $Root 'bin\\rclone.exe'", self.text)


class TaskScriptTests(unittest.TestCase):
	def test_task_runs_on_battery(self):
		text = read_script("task.ps1")
		self.assertIn("-AllowStartIfOnBatteries", text)
		self.assertIn("-DontStopIfGoingOnBatteries", text)
		self.assertIn("-UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest", text)

	def test_cleanup_removes_what_the_spec_lists(self):
		text = read_script("cleanup.ps1")
		self.assertIn("'bin', 'keys', 'shares'", text)
		self.assertIn("'wn-shares.ps1', 'status.json'", text)
		self.assertIn("Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false", text)
