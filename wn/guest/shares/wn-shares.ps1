$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = 'C:\ProgramData\wn'
$SharesFolder = Join-Path $Root 'shares'
$Rclone = Join-Path $Root 'bin\rclone.exe'
$StatusFile = Join-Path $Root 'status.json'
$LogFile = Join-Path $Root 'logs\wn-shares.log'
$LoopSeconds = 5
$MaxLogBytes = 10MB
$CacheLineWaitSeconds = 150
$StopWaitSeconds = 10
$LogTailLines = 200
$JsonDepth = 4
$CachePattern = '^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) .*vfs cache: cleaned: .* to upload (\d+), uploading (\d+)'
$RcloneTimeFormat = 'yyyy/MM/dd HH:mm:ss'
$Utf8 = New-Object System.Text.UTF8Encoding $false
$Processes = @{}
$Status = @{}

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

function Read-Share($Folder) {
	Get-Content -Raw -LiteralPath (Join-Path $Folder 'share.json') | ConvertFrom-Json
}

function Quote($Text) {
	'"' + $Text + '"'
}

function Get-MountArguments($Share, $Folder) {
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
		'-o', 'FileSecurity=D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;AU)',
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

function Get-PendingUploads($Folder, $Since) {
	$log = Join-Path $Folder 'mount.log'
	if (-not (Test-Path -LiteralPath $log)) { return $null }
	$pending = $null
	foreach ($line in Get-Content -LiteralPath $log -Tail $LogTailLines) {
		if ($line -match $CachePattern) {
			$time = [DateTime]::ParseExact($Matches[1], $RcloneTimeFormat, [Globalization.CultureInfo]::InvariantCulture)
			if ($time -gt $Since) { $pending = [int]$Matches[2] + [int]$Matches[3] }
		}
	}
	return $pending
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

function Remove-Share($Name, $Folder) {
	$process = $Processes[$Name]
	if ($process -and -not $process.HasExited) {
		$removed = (Get-Item -LiteralPath (Join-Path $Folder 'remove')).LastWriteTime
		$since = $removed.AddTicks(-($removed.Ticks % [TimeSpan]::TicksPerSecond))
		$pending = Get-PendingUploads $Folder $since
		if ($null -eq $pending) { $pending = $Status[$Name].pending }
		if ($pending -gt 0) {
			Set-State $Name 'removing' ('{0} files to upload to the Mac' -f $pending) $process.Id $pending
			return
		}
		if ($null -eq $pending -and ((Get-Date) - $removed).TotalSeconds -lt $CacheLineWaitSeconds) {
			Set-State $Name 'removing' 'waiting for rclone to report its uploads' $process.Id $null
			return
		}
		Stop-Process -Id $process.Id -Force
		Wait-Process -Id $process.Id -Timeout $StopWaitSeconds -ErrorAction SilentlyContinue
	}
	if (Test-Path -LiteralPath (Join-Path $Folder 'share.json')) {
		$share = Read-Share $Folder
		if (Test-Junction $share.mount_path) { [IO.Directory]::Delete($share.mount_path) }
	}
	& cmd.exe /c rd /s /q ('\\?\' + $Folder)
	$Processes.Remove($Name)
	$Status.Remove($Name)
	Write-Log ('{0}: removed' -f $Name)
}

function Update-Share($Name, $Folder) {
	$share = Read-Share $Folder
	$process = $Processes[$Name]
	if ($process -and -not $process.HasExited) {
		if (Test-Junction $share.mount_path) {
			Set-State $Name 'mounted' '' $process.Id $null
		} elseif ($Status[$Name].state -ne 'waiting') {
			Set-State $Name 'starting' '' $process.Id $null
		}
		return
	}
	if ($process) {
		$Processes.Remove($Name)
		$lastLine = Get-Content -LiteralPath (Join-Path $Folder 'mount.log') -Tail 1 -ErrorAction SilentlyContinue
		Set-State $Name 'waiting' ('rclone exited with code {0}; last log line: {1}' -f (Get-ExitText $process), $lastLine) $null $null
		return
	}
	$item = Get-Item -LiteralPath $share.mount_path -Force -ErrorAction SilentlyContinue
	if ($item -and $item.LinkType -eq 'Junction') {
		[IO.Directory]::Delete($share.mount_path)
	} elseif ($item -and ((-not $item.PSIsContainer) -or (Get-ChildItem -LiteralPath $share.mount_path -Force | Select-Object -First 1))) {
		Set-State $Name 'blocked' ('{0} exists as a local folder with files. Move them out and delete the folder' -f $share.mount_path) $null $null
		return
	} elseif ($item) {
		Remove-Item -LiteralPath $share.mount_path -Force
	}
	Limit-Log (Join-Path $Folder 'mount.log')
	$process = Start-Process -FilePath $Rclone -ArgumentList (Get-MountArguments $share $Folder) -WindowStyle Hidden -PassThru
	$null = $process.Handle
	$Processes[$Name] = $process
	if ($Status[$Name].state -eq 'waiting') {
		$Status[$Name].pid = $process.Id
	} else {
		Set-State $Name 'starting' '' $process.Id $null
	}
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null
Limit-Log $LogFile
Write-Log 'wn-shares started'
Find-Adopted
while ($true) {
	foreach ($folder in @(Get-ChildItem -LiteralPath $SharesFolder -Directory -ErrorAction SilentlyContinue)) {
		try {
			if (Test-Path -LiteralPath (Join-Path $folder.FullName 'remove')) {
				Remove-Share $folder.Name $folder.FullName
			} elseif (Test-Path -LiteralPath (Join-Path $folder.FullName 'share.json')) {
				Update-Share $folder.Name $folder.FullName
			}
		} catch {
			Write-Log ('{0}: error: {1}' -f $folder.Name, $_)
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
