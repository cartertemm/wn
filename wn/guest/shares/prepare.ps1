$MinimumMajor = 2
$MinimumMinor = 1
$SharesFolder = Join-Path $Root 'shares'
$ShareFolder = Join-Path $SharesFolder $Params.name

foreach ($folder in 'bin', 'downloads', 'keys', 'logs', 'shares') {
	New-Item -ItemType Directory -Force -Path (Join-Path $Root $folder) | Out-Null
}
$marker = Join-Path $ShareFolder 'remove'
$removing = Test-Path -LiteralPath $marker
if (-not $removing) {
	New-Item -ItemType Directory -Force -Path $ShareFolder | Out-Null
}

function Get-Sha256($Path) {
	if (Test-Path -LiteralPath $Path -PathType Leaf) {
		return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
	}
	return $null
}

$winfsp = 'missing'
$dll = Join-Path ${env:ProgramFiles(x86)} 'WinFsp\bin\winfsp-a64.dll'
if (Test-Path -LiteralPath $dll) {
	$info = (Get-Item -LiteralPath $dll).VersionInfo
	$current = ($info.FileMajorPart -gt $MinimumMajor) -or ($info.FileMajorPart -eq $MinimumMajor -and $info.FileMinorPart -ge $MinimumMinor)
	$launcher = Get-Service -Name 'WinFsp.Launcher' -ErrorAction SilentlyContinue
	if ($current -and $launcher) { $winfsp = 'ok' } else { $winfsp = 'old' }
}

$vmPath = $Params.vm_path
$pathState = 'missing'
$item = Get-Item -LiteralPath $vmPath -Force -ErrorAction SilentlyContinue
if ($item -and $item.LinkType -eq 'Junction') {
	$pathState = 'junction'
} elseif ($item -and -not $item.PSIsContainer) {
	$pathState = 'file'
} elseif ($item -and (Get-ChildItem -LiteralPath $vmPath -Force | Select-Object -First 1)) {
	$pathState = 'files'
} elseif ($item) {
	$pathState = 'empty'
}

$parent = Split-Path -Parent $vmPath
$parentExists = Test-Path -LiteralPath $parent -PathType Container
$systemFolder = $null
if ($parentExists) {
	foreach ($folder in $env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData) {
		if ([string]::Equals($parent, $folder, [StringComparison]::OrdinalIgnoreCase) -or $parent.StartsWith($folder + '\', [StringComparison]::OrdinalIgnoreCase)) {
			$systemFolder = $folder
		}
	}
}

$result = [ordered]@{
	winfsp = $winfsp
	rclone_sha256 = (Get-Sha256 (Join-Path $Root 'bin\rclone.exe'))
	supervisor_sha256 = (Get-Sha256 (Join-Path $Root 'wn-shares.ps1'))
	client_key_sha256 = (Get-Sha256 (Join-Path $Root 'keys\client_ed25519'))
	parent_exists = $parentExists
	vm_path = $pathState
	removing = $removing
	linked_parent = (Find-LinkedParent $vmPath)
	system_folder = $systemFolder
}
Write-Output ('wn-result: ' + ($result | ConvertTo-Json -Compress))
