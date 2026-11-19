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
	"$ReadWriteSecurity = 'FileSecurity=D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;AU)'",
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


def function_body(text, name):
	start = text.index("function {}".format(name))
	return text[start:text.index("\n}\n", start) + len("\n}\n")]


class GuestScriptTextTests(unittest.TestCase):
	def test_scripts_are_ascii_with_empty_blank_lines(self):
		for name in RUNNER_SCRIPTS + (SUPERVISOR_SCRIPT,):
			for number, line in enumerate(read_script(name).split("\n"), start=1):
				self.assertFalse(line.strip() == "" and line != "", "{}:{} has an indented blank line".format(name, number))
				self.assertFalse(line.startswith(" "), "{}:{} is indented with spaces".format(name, number))
				self.assertEqual(line, line.rstrip(), "{}:{} has trailing whitespace".format(name, number))

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

	def test_read_only_mount_lets_windows_only_read_and_run(self):
		self.assertIn("$ReadOnlySecurity = 'FileSecurity=D:P(A;;FRFX;;;SY)(A;;FRFX;;;BA)(A;;FRFX;;;AU)(A;;FRFX;;;OW)'", self.text)
		mount = function_body(self.text, "Get-MountArguments")
		self.assertIn("\t$security = $ReadWriteSecurity\n\tif ($Share.read_only) { $security = $ReadOnlySecurity }\n", mount)
		self.assertIn("'-o', $security,", mount)

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

	def test_task_starts_a_killed_supervisor_again(self):
		text = read_script("task.ps1")
		self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes $RestartMinutes)", text)
		self.assertIn("-Trigger $t, $repeat", text)
		self.assertIn("-MultipleInstances IgnoreNew", text)

	def test_cleanup_disables_the_task_before_stopping_it(self):
		text = read_script("cleanup.ps1")
		self.assertLess(text.index("Disable-ScheduledTask -TaskName $TaskName"), text.index("Stop-ScheduledTask"))

	def test_cleanup_removes_what_the_spec_lists(self):
		text = read_script("cleanup.ps1")
		self.assertIn("'bin', 'keys', 'shares'", text)
		self.assertIn("'wn-shares.ps1', 'status.json'", text)
		self.assertIn("Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false", text)


class RemovalTests(unittest.TestCase):
	def setUp(self):
		self.text = read_script(SUPERVISOR_SCRIPT)
		self.remove = function_body(self.text, "Remove-Share")

	def test_rclone_starts_in_one_function(self):
		self.assertEqual(self.text.count("Start-Process -FilePath $Rclone"), 1)
		self.assertIn("Start-Process -FilePath $Rclone", function_body(self.text, "Start-Mount"))
		self.assertIn("Start-Mount $Name $share $Folder", function_body(self.text, "Update-Share"))

	def test_removal_starts_rclone_when_the_cache_has_files(self):
		self.assertIn("if (-not $process -and (Test-Path -LiteralPath $shareFile) -and (Test-CacheFiles $Folder)) {", self.remove)
		self.assertLess(self.remove.index("Start-Mount $Name"), self.remove.index("Stop-Process"))
		cache_check = function_body(self.text, "Test-CacheFiles")
		self.assertIn("'cache\\vfs'", cache_check)
		self.assertIn("} catch {\n\t\treturn $true", cache_check)

	def test_removal_never_stops_rclone_while_pending_is_unknown(self):
		self.assertNotIn("CacheLineWaitSeconds", self.text)
		self.assertNotIn("$Status[$Name].pending", self.remove)
		self.assertLess(self.remove.index("if ((-not $counts -and (Test-CacheFiles $Folder)) -or (Test-DirtyCache $Folder)) {"), self.remove.index("Stop-Process"))

	def test_removal_waits_while_files_are_open_in_windows(self):
		self.assertIn(" in use (\\d+), to upload (\\d+), uploading (\\d+)'", self.text)
		counts = function_body(self.text, "Get-CacheCounts")
		self.assertIn("$pending = [int]$Matches[3] + [int]$Matches[4]", counts)
		self.assertIn("return @{ open = [Math]::Max(0, [int]$Matches[2] - $pending); pending = $pending }", counts)
		self.assertIn("if ($counts -and $counts.open -gt 0) {\n\t\t\tSet-State $Name 'removing' ($OpenFilesDetail -f $counts.open) $process.Id $null\n\t\t\treturn\n", self.remove)
		self.assertLess(self.remove.index("$counts.open -gt 0"), self.remove.index("Stop-Process"))

	def test_removal_starts_rclone_again_when_the_cache_is_dirty_after_the_stop(self):
		dirty = function_body(self.text, "Test-DirtyCache")
		self.assertIn("$DirtyPattern = '\"Dirty\":\\s*true'", self.text)
		self.assertIn("$LongPathPrefix + (Join-Path $Folder 'cache\\vfsMeta')", dirty)
		self.assertIn("} catch {\n\t\treturn $true", dirty)
		stop = self.remove.index("Stop-Process")
		recheck = self.remove.index("if (Test-DirtyCache $Folder) {\n\t\t\t$Processes.Remove($Name)\n\t\t\tSet-State $Name 'removing' $CacheUploadDetail $null $null\n\t\t\treturn\n")
		self.assertLess(stop, recheck)
		self.assertLess(recheck, self.remove.index("rd /s /q"))

	def test_removal_measures_from_the_later_of_marker_and_rclone_start(self):
		self.assertIn("if ($process.StartTime -gt $since) { $since = $process.StartTime }", self.remove)

	def test_pending_count_reads_past_the_log_tail(self):
		pending = function_body(self.text, "Get-CacheCounts")
		self.assertIn("$count *= $TailGrowthFactor", pending)
		self.assertIn("if ($lines.Count -lt $count) { return $null }", pending)

	def test_removal_keeps_the_share_listed_when_the_folder_stays(self):
		self.assertLess(self.remove.index("rd /s /q"), self.remove.index("if (Test-Path -LiteralPath $Folder) {"))
		self.assertLess(self.remove.index("Set-State $Name 'error' ($NotDeletedDetail -f $Folder)"), self.remove.index("$Status.Remove($Name)"))

	def test_removal_accepts_a_junction_that_winfsp_removed_first(self):
		self.assertIn("try { [IO.Directory]::Delete($share.mount_path) } catch { if (Test-Path -LiteralPath $share.mount_path) { throw } }", self.remove)

	def test_startup_errors_do_not_end_the_supervisor(self):
		self.assertRegex(self.text, r"\ntry \{\n(\t.*\n)*\tFind-Adopted\n\} catch \{\n")
		self.assertNotIn("\nFind-Adopted\n", self.text)


class UploadFailureTests(unittest.TestCase):
	def setUp(self):
		self.text = read_script(SUPERVISOR_SCRIPT)
		self.failures = function_body(self.text, "Update-Failures")

	def test_failure_lines_match_what_rclone_writes(self):
		self.assertIn("$FailurePattern = '^\\S+ \\S+ ERROR : (.+): vfs cache: failed to upload try #\\d+, will retry in \\S+: (?:vfs cache: failed to transfer file from cache to remote: )?(.+)$'", self.text)
		self.assertIn("$SuccessPattern = '^\\S+ \\S+ INFO  : (.+): vfs cache: upload succeeded try #\\d+$'", self.text)

	def test_only_new_log_bytes_are_read_each_pass(self):
		self.assertIn("$stream.Position = $LogOffsets[$Name]", self.failures)
		self.assertIn("$stream.Length -lt $LogOffsets[$Name]", self.failures)
		self.assertIn("$end = [Array]::LastIndexOf($bytes, [byte]10, $read - 1) + 1", self.failures)
		self.assertIn("$LogOffsets[$Name] += $end", self.failures)

	def test_a_failure_clears_on_success_or_when_nothing_waits_for_upload(self):
		self.assertIn("} elseif ($line -match $SuccessPattern) {\n\t\t\t$Failures[$Name].Remove($Matches[1])\n", self.failures)
		self.assertIn("} elseif ($line -match $CachePattern -and [int]$Matches[3] + [int]$Matches[4] -eq 0) {\n\t\t\t$Failures[$Name].Clear()\n", self.failures)

	def test_a_mounted_share_reports_its_failures(self):
		update = function_body(self.text, "Update-Share")
		self.assertIn("\t\t\tUpdate-Failures $Name $Folder\n\t\t\tSet-State $Name 'mounted' (Get-FailureDetail $Name) $process.Id $null\n\t\t\tif ($Failures[$Name].Count) { $Status[$Name].failed = $Failures[$Name].Count }\n", update)
		self.assertIn("$FailedDetail = '{0} cannot be uploaded to the Mac: {1}: {2}'", self.text)

	def test_a_removal_shows_its_failures_in_the_detail(self):
		remove = function_body(self.text, "Remove-Share")
		self.assertLess(remove.index("Update-Failures $Name $Folder"), remove.index("Get-CacheCounts"))
		self.assertIn("Set-State $Name 'removing' ('{0}. {1}' -f $detail, $failed) $process.Id $null\n\t\t\t\t$Status[$Name].failed = $Failures[$Name].Count\n\t\t\t\t$Status[$Name].to_upload = $counts.pending\n", remove)
		self.assertLess(remove.index("Get-FailureDetail $Name"), remove.index("Stop-Process"))


class StatusListingTests(unittest.TestCase):
	def setUp(self):
		self.text = read_script(SUPERVISOR_SCRIPT)
		self.loop = self.text[self.text.index("\nwhile ($true) {\n"):]

	def test_every_share_folder_gets_a_state(self):
		self.assertIn("} else {\n\t\t\t\tSet-State $folder.Name 'incomplete' $NoShareFileDetail $null $null\n", self.loop)
		self.assertIn("} catch {\n\t\t\tSet-State $folder.Name 'error' ([string]$_) $null $null\n", self.loop)

	def test_a_failed_folder_listing_writes_no_status(self):
		self.assertIn("$folders = @(Get-ChildItem -LiteralPath $SharesFolder -Directory) }", self.loop)
		self.assertLess(self.loop.index("\t\tcontinue\n"), self.loop.index("Write-Status"))
		self.assertNotIn("$SharesFolder -Directory -ErrorAction SilentlyContinue", self.loop)


class LinkedParentTests(unittest.TestCase):
	def setUp(self):
		self.text = read_script(SUPERVISOR_SCRIPT)

	def test_parents_are_checked_before_any_delete_or_mount(self):
		mount = function_body(self.text, "Start-Mount")
		check = mount.index("Find-LinkedParent")
		for use in ("[IO.Directory]::Delete", "Remove-Item", "Start-Process"):
			self.assertLess(check, mount.index(use))
		remove = function_body(self.text, "Remove-Share")
		self.assertLess(remove.index("Find-LinkedParent"), remove.index("[IO.Directory]::Delete"))
		self.assertIn("[IO.FileAttributes]::ReparsePoint", function_body(self.text, "Find-LinkedParent"))

	def test_removal_skips_the_mount_path_under_a_link_and_still_deletes_the_share_folder(self):
		remove = function_body(self.text, "Remove-Share")
		self.assertIn("if (-not (Find-LinkedParent $share.mount_path) -and (Test-Junction $share.mount_path)) {", remove)
		self.assertNotIn("'blocked'", remove)

	def test_a_blocked_removal_that_keeps_its_cache_tells_the_mac(self):
		remove = function_body(self.text, "Remove-Share")
		self.assertIn("} else {\n\t\t\t$Status[$Name].remove_requested = $true\n\t\t}\n\t\treturn\n", remove)

	def test_prepare_shares_the_supervisor_checks(self):
		prepare = read_script("prepare.ps1")
		for name in ("Find-LinkedParent", "Test-CacheFiles"):
			self.assertEqual(function_body(prepare, name), function_body(self.text, name))
		self.assertIn("linked_parent = (Find-LinkedParent $vmPath)", prepare)


class PrepareScriptTests(unittest.TestCase):
	def setUp(self):
		self.text = read_script("prepare.ps1")

	def test_system_folders_come_from_the_environment(self):
		self.assertIn("foreach ($folder in $env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData) {", self.text)
		self.assertIn("GetFinalPathNameByHandleW", self.text)
		self.assertIn("system_folder = $systemFolder", self.text)
		self.assertNotIn("C:\\Windows", self.text)
		self.assertNotIn("C:\\Program Files", self.text)

	def test_leftover_removal_is_cleared_only_when_nothing_can_be_lost(self):
		self.assertIn("if ($removing -and -not (Test-ShareActive) -and -not (Test-CacheFiles $ShareFolder)) {", self.text)
		self.assertIn("\t$removing = Test-Path -LiteralPath $marker\n", self.text)
		active = function_body(self.text, "Test-ShareActive")
		self.assertIn("$task.State -eq 'Running'", active)
		self.assertIn("Join-Path $ShareFolder 'rclone.conf'", active)

	def test_share_folder_takes_the_requested_letter_case_only_when_nothing_can_be_lost(self):
		self.assertIn("$existing[0].Name -cne $Params.name -and -not (Test-Path -LiteralPath (Join-Path $ShareFolder 'share.json')) -and -not (Test-CacheFiles $ShareFolder)) {", self.text)
		self.assertLess(self.text.index("$existing[0].Name -cne $Params.name"), self.text.index("New-Item -ItemType Directory -Force -Path $ShareFolder"))


class FetchScriptTests(unittest.TestCase):
	def setUp(self):
		self.text = read_script("fetch.ps1")

	def test_winfsp_log_lines_are_plain_strings(self):
		self.assertIn("-Tail $LogTailLines -ErrorAction SilentlyContinue | ForEach-Object { \"$_\" })", self.text)

	def test_rclone_in_use_stops_before_the_download(self):
		check = self.text.index("Where-Object { $_.ExecutablePath -eq $Rclone }")
		self.assertLess(check, self.text.index("throw $RcloneInUseError"))
		self.assertLess(self.text.index("throw $RcloneInUseError"), self.text.index("Save-Verified $Params.rclone_url"))
		self.assertIn("Run wn unshare for each share, then run wn share again", self.text)


class CleanupScriptTests(unittest.TestCase):
	def test_cleanup_refuses_while_a_share_has_cache_files(self):
		text = read_script("cleanup.ps1")
		self.assertEqual(function_body(text, "Test-CacheFiles"), function_body(read_script(SUPERVISOR_SCRIPT), "Test-CacheFiles"))
		guard = text.index("if ($sharesWithCache.Count -eq 0) {")
		self.assertLess(text.index("Where-Object { Test-CacheFiles $_.FullName }"), guard)
		for action in ("Stop-ScheduledTask", "Stop-Process", "rd /s /q", "Remove-Item"):
			self.assertLess(guard, text.index(action))
		self.assertIn("cleaned = ($sharesWithCache.Count -eq 0); shares_with_cache = $sharesWithCache", text)

	def test_cleanup_waits_for_the_supervisor_before_stopping_rclone(self):
		text = read_script("cleanup.ps1")
		self.assertEqual(function_body(text, "Wait-TaskStopped"), function_body(read_script("task.ps1"), "Wait-TaskStopped"))
		self.assertLess(text.index("\tWait-TaskStopped\n"), text.index("Stop-Process"))
