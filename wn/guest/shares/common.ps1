$Root = 'C:\ProgramData\wn'
$TaskName = 'wn-shares'
$StopWaitSeconds = 10
$LongPathPrefix = '\\?\'

function Test-CacheFiles($Folder) {
	$vfs = Join-Path $Folder 'cache\vfs'
	if (-not (Test-Path -LiteralPath $vfs)) { return $false }
	try {
		return [bool](Get-ChildItem -LiteralPath $vfs -Recurse -File -Force | Select-Object -First 1)
	} catch {
		return $true
	}
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

function Wait-TaskStopped {
	for ($second = 0; $second -lt $StopWaitSeconds; $second++) {
		if ((Get-ScheduledTask -TaskName $TaskName).State -ne 'Running') { return }
		Start-Sleep -Seconds 1
	}
}
