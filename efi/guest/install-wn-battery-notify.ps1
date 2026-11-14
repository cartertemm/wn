# Compiles the wn-battery-notify service with the inbox .NET Framework compiler and sets it to start with Windows.
# Run as administrator or as SYSTEM. wn-battery-notify.cs must be in the same folder as this script.

$ErrorActionPreference = 'Stop'

$LogPath = 'C:\wn-battery-notify-install.log'
$ServiceName = 'wn-battery-notify'
$DisplayName = 'wn battery notify'
$ServiceDescription = 'Tells Windows when the battery state from the Mac changes.'
$SourceFile = 'wn-battery-notify.cs'
$InstallFolder = Join-Path $env:ProgramFiles 'wn'
$Compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$RestartDelayMilliseconds = 1000
$FailureResetSeconds = 86400

function Write-Log([string]$Message) {
	$line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
	Add-Content -Path $LogPath -Value $line
	Write-Host $line
}

try {
	Write-Log ('install-wn-battery-notify started as {0}' -f [System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
	$program = Join-Path $InstallFolder "$ServiceName.exe"
	$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
	if ($service -and $service.Status -ne 'Stopped') {
		Stop-Service -Name $ServiceName
		Write-Log 'stopped the running service'
	}
	New-Item -ItemType Directory -Force -Path $InstallFolder | Out-Null
	$output = & $Compiler /nologo /optimize "/out:$program" (Join-Path $PSScriptRoot $SourceFile)
	if ($LASTEXITCODE -ne 0) {
		throw "csc failed with exit code ${LASTEXITCODE}: $output"
	}
	Write-Log ('compiled {0} ({1} bytes)' -f $program, (Get-Item $program).Length)
	if ($service) {
		Write-Log 'the service already exists'
	} else {
		New-Service -Name $ServiceName -BinaryPathName "`"$program`"" -DisplayName $DisplayName -Description $ServiceDescription -StartupType Automatic | Out-Null
		Write-Log 'created the service'
	}
	$restart = "restart/$RestartDelayMilliseconds"
	& sc.exe failure $ServiceName reset= $FailureResetSeconds actions= "$restart/$restart/$restart" | Out-Null
	if ($LASTEXITCODE -ne 0) {
		throw "sc failure failed with exit code $LASTEXITCODE"
	}
	Start-Service -Name $ServiceName
	Write-Log ('service status: {0}' -f (Get-Service -Name $ServiceName).Status)
	Write-Log 'install-wn-battery-notify finished'
} catch {
	Write-Log ('install-wn-battery-notify failed: {0}' -f $_)
	exit 1
}
