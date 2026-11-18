$Root = 'C:\ProgramData\wn'
$TaskName = 'wn-shares'
$StopWaitSeconds = 10
$SharesFolder = Join-Path $Root 'shares'

function Wait-TaskStopped {
	for ($second = 0; $second -lt $StopWaitSeconds; $second++) {
		if ((Get-ScheduledTask -TaskName $TaskName).State -ne 'Running') { return }
		Start-Sleep -Seconds 1
	}
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

$sharesWithCache = @()
if (Test-Path -LiteralPath $SharesFolder) {
	$sharesWithCache = @(Get-ChildItem -LiteralPath $SharesFolder -Directory | Where-Object { Test-CacheFiles $_.FullName } | ForEach-Object { [string]$_.Name })
}
if ($sharesWithCache.Count -eq 0) {
	if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
		Stop-ScheduledTask -TaskName $TaskName
		Wait-TaskStopped
		Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
	}
	foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name = 'rclone.exe'")) {
		if ($process.CommandLine -and $process.CommandLine.Contains($Root + '\')) {
			Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
			Wait-Process -Id $process.ProcessId -Timeout $StopWaitSeconds -ErrorAction SilentlyContinue
		}
	}
	foreach ($name in 'bin', 'keys', 'shares') {
		$path = Join-Path $Root $name
		if (Test-Path -LiteralPath $path) { & cmd.exe /c rd /s /q ('\\?\' + $path) }
	}
	foreach ($name in 'wn-shares.ps1', 'status.json') {
		$path = Join-Path $Root $name
		if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
	}
}
Write-Output ('wn-result: ' + ([ordered]@{ cleaned = ($sharesWithCache.Count -eq 0); shares_with_cache = $sharesWithCache } | ConvertTo-Json -Compress))
