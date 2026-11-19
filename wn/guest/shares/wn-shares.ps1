$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = 'C:\ProgramData\wn'
$SharesFolder = Join-Path $Root 'shares'
$Rclone = Join-Path $Root 'bin\rclone.exe'
$StatusFile = Join-Path $Root 'status.json'
$LogFile = Join-Path $Root 'logs\wn-shares.log'
$LoopSeconds = 5
$MaxLogBytes = 10MB
$StopWaitSeconds = 10
$LogTailLines = 200
$TailGrowthFactor = 4
$JsonDepth = 4
$ReadWriteSecurity = 'FileSecurity=D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;AU)'
$ReadOnlySecurity = 'FileSecurity=D:P(A;;FRFX;;;SY)(A;;FRFX;;;BA)(A;;FRFX;;;AU)(A;;FRFX;;;OW)'
$CachePattern = '^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) .*vfs cache: cleaned: .* in use (\d+), to upload (\d+), uploading (\d+)'
$DirtyPattern = '"Dirty":\s*true'
$FailurePattern = '^\S+ \S+ ERROR : (.+): vfs cache: failed to upload try #\d+, will retry in \S+: (?:vfs cache: failed to transfer file from cache to remote: )?(.+)$'
$SuccessPattern = '^\S+ \S+ INFO  : (.+): vfs cache: upload succeeded try #\d+$'
$LongPathPrefix = '\\?\'
$RcloneTimeFormat = 'yyyy/MM/dd HH:mm:ss'
$LinkedParentDetail = '{0} is a link or mount point. Use a share path with no link in it'
$NoShareFileDetail = 'the share folder has no share.json'
$NotDeletedDetail = 'could not delete {0}'
$OpenFilesDetail = '{0} files are open in Windows. Close them so the share can be removed'
$CacheUploadDetail = 'starting rclone to upload the files in its cache'
$FailedDetail = '{0} cannot be uploaded to the Mac: {1}: {2}'
$Utf8 = New-Object System.Text.UTF8Encoding $false
$Processes = @{}
$Status = @{}
$Failures = @{}
$LogOffsets = @{}

function Get-Timestamp {
	(Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)
}

function Write-Log($Message) {
	[IO.File]::AppendAllText($LogFile, ('{0} {1}' -f (Get-Timestamp), $Message) + "`r`n", $Utf8)
}

function Limit-Log($Path) {
	if ((Test-Path -LiteralPath $Path) -and (Get-Item -LiteralPath $Path).Length -gt $MaxLogBytes) {
		Clear-Content -LiteralPath $Path
	}
}

function Set-State($Name, $State, $Detail, $ProcessId, $Pending) {
	$previous = $Status[$Name]
	if (-not $previous -or $previous.state -ne $State -or $previous.detail -ne $Detail) {
		Write-Log ('{0}: {1} {2}' -f $Name, $State, $Detail)
	}
	$entry = [ordered]@{ state = $State; detail = $Detail; pid = $ProcessId }
	if ($null -ne $Pending) { $entry.pending = $Pending }
	$Status[$Name] = $entry
}

function Write-Status {
	$document = [ordered]@{ updated = (Get-Timestamp); shares = $Status }
	$part = $StatusFile + '.part'
	[IO.File]::WriteAllText($part, ($document | ConvertTo-Json -Depth $JsonDepth -Compress), $Utf8)
	Move-Item -LiteralPath $part -Destination $StatusFile -Force
}

function Test-Junction($Path) {
	$item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
	return [bool]($item -and $item.LinkType -eq 'Junction')
}

function Find-LinkedParent($Path) {
	$parent = Split-Path -Parent $Path
	while ($parent) {
		$item = Get-Item -LiteralPath $parent -Force -ErrorAction SilentlyContinue
		if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $parent }
		$parent = Split-Path -Parent $parent
	}
	return $null
}

function Test-CacheFiles($Folder) {
	$vfs = Join-Path $Folder 'cache\vfs'
	if (-not (Test-Path -LiteralPath $vfs)) { return $false }
	try {
		return [bool](Get-ChildItem -LiteralPath $vfs -Recurse -File -Force | Select-Object -First 1)
	} catch {
		return $true
	}
}

function Test-DirtyCache($Folder) {
	$meta = $LongPathPrefix + (Join-Path $Folder 'cache\vfsMeta')
	if (-not [IO.Directory]::Exists($meta)) { return $false }
	try {
		foreach ($file in [IO.Directory]::EnumerateFiles($meta, '*', 'AllDirectories')) {
			if ([IO.File]::ReadAllText($file) -match $DirtyPattern) { return $true }
		}
		return $false
	} catch {
		return $true
	}
}

function Read-Share($Folder) {
	Get-Content -Raw -LiteralPath (Join-Path $Folder 'share.json') | ConvertFrom-Json
}

function Quote($Text) {
	'"' + $Text + '"'
}

function Get-MountArguments($Share, $Folder) {
	$security = $ReadWriteSecurity
	if ($Share.read_only) { $security = $ReadOnlySecurity }
	$arguments = @(
		'mount', 'share:', (Quote $Share.mount_path),
		'--config', (Quote (Join-Path $Folder 'rclone.conf')),
		'--cache-dir', (Quote (Join-Path $Folder 'cache')),
		'--vfs-cache-mode', 'writes',
		'--vfs-write-back', '5s',
		'--vfs-cache-max-size', '2G',
		'--vfs-cache-poll-interval', '10s',
		'--dir-cache-time', '1s',
		'--attr-timeout', '1s',
		'--contimeout', '5s',
		'--timeout', '30s',
		'--file-perms', '0777',
		'--dir-perms', '0777',
		'--volname', $Share.name,
		'-o', $security,
		'--log-file', (Quote (Join-Path $Folder 'mount.log')),
		'-v'
	)
	if ($Share.read_only) { $arguments += '--read-only' }
	return $arguments
}

function Get-ExitText($Process) {
	try { $code = $Process.ExitCode } catch { $code = $null }
	if ($null -eq $code) { return 'unknown' }
	return [string]$code
}

function Get-ExitDetail($Process, $Folder) {
	$lastLine = Get-Content -LiteralPath (Join-Path $Folder 'mount.log') -Tail 1 -ErrorAction SilentlyContinue
	return ('rclone exited with code {0}; last log line: {1}' -f (Get-ExitText $Process), $lastLine)
}

function Get-CacheCounts($Folder, $Since) {
	$log = Join-Path $Folder 'mount.log'
	if (-not (Test-Path -LiteralPath $log)) { return $null }
	for ($count = $LogTailLines; ; $count *= $TailGrowthFactor) {
		$lines = @(Get-Content -LiteralPath $log -Tail $count)
		for ($index = $lines.Count - 1; $index -ge 0; $index--) {
			if ($lines[$index] -match $CachePattern) {
				$time = [DateTime]::ParseExact($Matches[1], $RcloneTimeFormat, [Globalization.CultureInfo]::InvariantCulture)
				$pending = [int]$Matches[3] + [int]$Matches[4]
				if ($time -gt $Since) { return @{ open = [Math]::Max(0, [int]$Matches[2] - $pending); pending = $pending } }
				return $null
			}
		}
		if ($lines.Count -lt $count) { return $null }
	}
}

function Update-Failures($Name, $Folder) {
	$log = Join-Path $Folder 'mount.log'
	if (-not (Test-Path -LiteralPath $log)) { return }
	$stream = [IO.File]::Open($log, 'Open', 'Read', 'ReadWrite, Delete')
	try {
		if (-not $Failures.Contains($Name) -or $stream.Length -lt $LogOffsets[$Name]) {
			$Failures[$Name] = [ordered]@{}
			$LogOffsets[$Name] = 0
		}
		$stream.Position = $LogOffsets[$Name]
		$bytes = New-Object byte[] ($stream.Length - $stream.Position)
		$read = $stream.Read($bytes, 0, $bytes.Length)
	} finally {
		$stream.Dispose()
	}
	if ($read -eq 0) { return }
	$end = [Array]::LastIndexOf($bytes, [byte]10, $read - 1) + 1
	$LogOffsets[$Name] += $end
	foreach ($line in $Utf8.GetString($bytes, 0, $end).Split("`n")) {
		$line = $line.TrimEnd("`r")
		if ($line -match $FailurePattern) {
			$Failures[$Name].Remove($Matches[1])
			$Failures[$Name][$Matches[1]] = $Matches[2]
		} elseif ($line -match $SuccessPattern) {
			$Failures[$Name].Remove($Matches[1])
		} elseif ($line -match $CachePattern -and [int]$Matches[3] + [int]$Matches[4] -eq 0) {
			$Failures[$Name].Clear()
		}
	}
}

function Get-FailureDetail($Name) {
	$failures = $Failures[$Name]
	if (-not $failures -or $failures.Count -eq 0) { return '' }
	$files = '{0} files' -f $failures.Count
	if ($failures.Count -eq 1) { $files = '1 file' }
	$newest = @($failures.Keys)[-1]
	return ($FailedDetail -f $files, $newest, $failures[$newest])
}

function Find-Adopted {
	$running = @(Get-CimInstance Win32_Process -Filter "Name = 'rclone.exe'")
	foreach ($folder in @(Get-ChildItem -LiteralPath $SharesFolder -Directory -ErrorAction SilentlyContinue)) {
		$config = Join-Path $folder.FullName 'rclone.conf'
		foreach ($candidate in $running) {
			if ($candidate.CommandLine -and $candidate.CommandLine.Contains($config)) {
				$Processes[$folder.Name] = Get-Process -Id $candidate.ProcessId -ErrorAction SilentlyContinue
				Write-Log ('{0}: adopted rclone with PID {1}' -f $folder.Name, $candidate.ProcessId)
			}
		}
	}
}

function Start-Mount($Name, $Share, $Folder) {
	$linked = Find-LinkedParent $Share.mount_path
	if ($linked) {
		Set-State $Name 'blocked' ($LinkedParentDetail -f $linked) $null $null
		return $null
	}
	$item = Get-Item -LiteralPath $Share.mount_path -Force -ErrorAction SilentlyContinue
	if ($item -and $item.LinkType -eq 'Junction') {
		[IO.Directory]::Delete($Share.mount_path)
	} elseif ($item -and ((-not $item.PSIsContainer) -or (Get-ChildItem -LiteralPath $Share.mount_path -Force | Select-Object -First 1))) {
		Set-State $Name 'blocked' ('{0} exists as a local folder with files. Move them out and delete the folder' -f $Share.mount_path) $null $null
		return $null
	} elseif ($item) {
		Remove-Item -LiteralPath $Share.mount_path -Force
	}
	Limit-Log (Join-Path $Folder 'mount.log')
	$process = Start-Process -FilePath $Rclone -ArgumentList (Get-MountArguments $Share $Folder) -WindowStyle Hidden -PassThru
	$null = $process.Handle
	$Processes[$Name] = $process
	return $process
}

function Remove-Share($Name, $Folder) {
	$process = $Processes[$Name]
	$shareFile = Join-Path $Folder 'share.json'
	if ($process -and $process.HasExited) {
		$Processes.Remove($Name)
		Set-State $Name 'removing' (Get-ExitDetail $process $Folder) $null $null
		return
	}
	if (-not $process -and (Test-Path -LiteralPath $shareFile) -and (Test-CacheFiles $Folder)) {
		$process = Start-Mount $Name (Read-Share $Folder) $Folder
		if ($process) {
			Set-State $Name 'removing' $CacheUploadDetail $process.Id $null
		} else {
			$Status[$Name].remove_requested = $true
		}
		return
	}
	if ($process) {
		$since = (Get-Item -LiteralPath (Join-Path $Folder 'remove')).LastWriteTime
		if ($process.StartTime -gt $since) { $since = $process.StartTime }
		$since = $since.AddTicks(-($since.Ticks % [TimeSpan]::TicksPerSecond))
		Update-Failures $Name $Folder
		$counts = Get-CacheCounts $Folder $since
		if ($counts -and $counts.open -gt 0) {
			Set-State $Name 'removing' ($OpenFilesDetail -f $counts.open) $process.Id $null
			return
		}
		if ($counts -and $counts.pending -gt 0) {
			$detail = '{0} files to upload to the Mac' -f $counts.pending
			$failed = Get-FailureDetail $Name
			if ($failed) {
				Set-State $Name 'removing' ('{0}. {1}' -f $detail, $failed) $process.Id $null
				$Status[$Name].failed = $Failures[$Name].Count
				$Status[$Name].to_upload = $counts.pending
			} else {
				Set-State $Name 'removing' $detail $process.Id $counts.pending
			}
			return
		}
		if ((-not $counts -and (Test-CacheFiles $Folder)) -or (Test-DirtyCache $Folder)) {
			Set-State $Name 'removing' 'waiting for rclone to report its uploads' $process.Id $null
			return
		}
		Stop-Process -Id $process.Id -Force
		Wait-Process -Id $process.Id -Timeout $StopWaitSeconds -ErrorAction SilentlyContinue
		if (Test-DirtyCache $Folder) {
			$Processes.Remove($Name)
			Set-State $Name 'removing' $CacheUploadDetail $null $null
			return
		}
	}
	if (Test-Path -LiteralPath $shareFile) {
		$share = Read-Share $Folder
		if (-not (Find-LinkedParent $share.mount_path) -and (Test-Junction $share.mount_path)) {
			try { [IO.Directory]::Delete($share.mount_path) } catch { if (Test-Path -LiteralPath $share.mount_path) { throw } }
		}
	}
	& cmd.exe /c rd /s /q ('\\?\' + $Folder)
	$Processes.Remove($Name)
	if (Test-Path -LiteralPath $Folder) {
		Set-State $Name 'error' ($NotDeletedDetail -f $Folder) $null $null
		return
	}
	$Status.Remove($Name)
	$Failures.Remove($Name)
	Write-Log ('{0}: removed' -f $Name)
}

function Update-Share($Name, $Folder) {
	$share = Read-Share $Folder
	$process = $Processes[$Name]
	if ($process -and -not $process.HasExited) {
		if (Test-Junction $share.mount_path) {
			Update-Failures $Name $Folder
			Set-State $Name 'mounted' (Get-FailureDetail $Name) $process.Id $null
			if ($Failures[$Name].Count) { $Status[$Name].failed = $Failures[$Name].Count }
		} elseif ($Status[$Name].state -ne 'waiting') {
			Set-State $Name 'starting' '' $process.Id $null
		}
		return
	}
	if ($process) {
		$Processes.Remove($Name)
		Set-State $Name 'waiting' (Get-ExitDetail $process $Folder) $null $null
		return
	}
	$process = Start-Mount $Name $share $Folder
	if (-not $process) { return }
	if ($Status[$Name].state -eq 'waiting') {
		$Status[$Name].pid = $process.Id
	} else {
		Set-State $Name 'starting' '' $process.Id $null
	}
}

try {
	New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null
	Limit-Log $LogFile
	Write-Log 'wn-shares started'
	Find-Adopted
} catch {
	Write-Log ('error: {0}' -f $_)
}
while ($true) {
	try {
		$folders = @()
		if (Test-Path -LiteralPath $SharesFolder) { $folders = @(Get-ChildItem -LiteralPath $SharesFolder -Directory) }
	} catch {
		Write-Log ('error: {0}' -f $_)
		Start-Sleep -Seconds $LoopSeconds
		continue
	}
	foreach ($folder in $folders) {
		try {
			if (Test-Path -LiteralPath (Join-Path $folder.FullName 'remove')) {
				Remove-Share $folder.Name $folder.FullName
			} elseif (Test-Path -LiteralPath (Join-Path $folder.FullName 'share.json')) {
				Update-Share $folder.Name $folder.FullName
			} else {
				Set-State $folder.Name 'incomplete' $NoShareFileDetail $null $null
			}
		} catch {
			Set-State $folder.Name 'error' ([string]$_) $null $null
		}
	}
	try {
		foreach ($name in @($Status.Keys)) {
			if (-not (Test-Path -LiteralPath (Join-Path $SharesFolder $name))) { $Status.Remove($name) }
		}
		Write-Status
	} catch {
		Write-Log ('error: {0}' -f $_)
	}
	Start-Sleep -Seconds $LoopSeconds
}
