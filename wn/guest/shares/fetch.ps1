$Root = 'C:\ProgramData\wn'
$LogTailLines = 20
$Rclone = Join-Path $Root 'bin\rclone.exe'
$RcloneInUseError = 'rclone.exe in Windows is in use by mounted shares, so wn cannot replace it. Run wn unshare for each share, then run wn share again'
$result = [ordered]@{ mismatch = $null; winfsp_exit = $null; winfsp_log = @() }

function Save-Verified($Url, $Sha256, $Destination) {
	$part = $Destination + '.part'
	Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $part
	$actual = (Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash.ToLowerInvariant()
	if ($actual -ne $Sha256) {
		Remove-Item -LiteralPath $part -Force
		return $false
	}
	Move-Item -LiteralPath $part -Destination $Destination -Force
	return $true
}

if ($Params.rclone_url) {
	if (@(Get-CimInstance Win32_Process -Filter "Name = 'rclone.exe'" | Where-Object { $_.ExecutablePath -eq $Rclone }).Count) {
		throw $RcloneInUseError
	}
	if (-not (Save-Verified $Params.rclone_url $Params.rclone_sha256 $Rclone)) {
		$result.mismatch = 'rclone.exe'
	}
}
if ($Params.winfsp_url -and -not $result.mismatch) {
	$msi = Join-Path $Root ('downloads\' + $Params.winfsp_name)
	$log = Join-Path $Root 'logs\winfsp-install.log'
	if (Save-Verified $Params.winfsp_url $Params.winfsp_sha256 $msi) {
		$arguments = @('/i', $msi, '/qn', '/norestart', '/l*v', $log)
		$process = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\msiexec.exe') -ArgumentList $arguments -Wait -PassThru
		$result.winfsp_exit = $process.ExitCode
		if ($process.ExitCode -ne 0) {
			$result.winfsp_log = @(Get-Content -LiteralPath $log -Tail $LogTailLines -ErrorAction SilentlyContinue | ForEach-Object { "$_" })
		}
		Remove-Item -LiteralPath $msi -Force
	} else {
		$result.mismatch = $Params.winfsp_name
	}
}
Write-Output ('wn-result: ' + ($result | ConvertTo-Json -Compress))
