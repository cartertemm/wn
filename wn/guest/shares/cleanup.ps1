$SharesFolder = Join-Path $Root 'shares'

$sharesWithCache = @()
if (Test-Path -LiteralPath $SharesFolder) {
	$sharesWithCache = @(Get-ChildItem -LiteralPath $SharesFolder -Directory | Where-Object { Test-CacheFiles $_.FullName } | ForEach-Object { [string]$_.Name })
}
if ($sharesWithCache.Count -eq 0) {
	if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
		Disable-ScheduledTask -TaskName $TaskName | Out-Null
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
		if (Test-Path -LiteralPath $path) { & cmd.exe /c rd /s /q ($LongPathPrefix + $path) }
	}
	foreach ($name in 'wn-shares.ps1', 'status.json') {
		$path = Join-Path $Root $name
		if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
	}
}
Write-Output ('wn-result: ' + ([ordered]@{ cleaned = ($sharesWithCache.Count -eq 0); shares_with_cache = $sharesWithCache } | ConvertTo-Json -Compress))
