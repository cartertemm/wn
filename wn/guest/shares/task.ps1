$Supervisor = 'C:\ProgramData\wn\wn-shares.ps1'
$RestartCount = 999
$RestartMinutes = 1
$NormalPriority = 4

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
	$a = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + $Supervisor)
	$p = New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest
	$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount $RestartCount -RestartInterval (New-TimeSpan -Minutes $RestartMinutes) -MultipleInstances IgnoreNew -Priority $NormalPriority
	$t = New-ScheduledTaskTrigger -AtStartup
	$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $RestartMinutes)
	Register-ScheduledTask -TaskName $TaskName -Action $a -Principal $p -Settings $s -Trigger $t, $repeat -Force | Out-Null
	Start-ScheduledTask -TaskName $TaskName
} elseif ($Params.restart) {
	Stop-ScheduledTask -TaskName $TaskName
	Wait-TaskStopped
	Start-ScheduledTask -TaskName $TaskName
} elseif ($task.State -ne 'Running') {
	Start-ScheduledTask -TaskName $TaskName
}
Write-Output 'wn-result: {}'
