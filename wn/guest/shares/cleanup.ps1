$Root = 'C:\ProgramData\wn'
$TaskName = 'wn-shares'
$StopWaitSeconds = 10
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
	Stop-ScheduledTask -TaskName $TaskName
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
Write-Output ('wn-result: ' + (@{ cleaned = $true } | ConvertTo-Json -Compress))
