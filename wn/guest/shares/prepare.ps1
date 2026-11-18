$Root = 'C:\ProgramData\wn'
$TaskName = 'wn-shares'
$MinimumMajor = 2
$MinimumMinor = 1
$LongPathPrefix = '\\?\'
$ShareFolder = Join-Path $Root ('shares\' + $Params.name)
$FinalPathSource = @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
public static class WnFinalPath {
	const uint ReadAttributes = 0x80;
	const uint ShareAll = 7;
	const uint OpenExisting = 3;
	const uint BackupSemantics = 0x02000000;
	const uint NormalizedDosName = 0;
	const int MaxPathChars = 32768;
	[DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
	static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
	[DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
	static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, StringBuilder path, uint size, uint flags);
	public static string Get(string name) {
		using (SafeFileHandle handle = CreateFileW(name, ReadAttributes, ShareAll, IntPtr.Zero, OpenExisting, BackupSemantics, IntPtr.Zero)) {
			if (handle.IsInvalid) { throw new Win32Exception(); }
			StringBuilder path = new StringBuilder(MaxPathChars);
			uint length = GetFinalPathNameByHandleW(handle, path, (uint)path.Capacity, NormalizedDosName);
			if (length == 0 || length >= path.Capacity) { throw new Win32Exception(); }
			return path.ToString();
		}
	}
}
'@

function Test-CacheFiles($Folder) {
	$vfs = Join-Path $Folder 'cache\vfs'
	if (-not (Test-Path -LiteralPath $vfs)) { return $false }
	try {
		return [bool](Get-ChildItem -LiteralPath $vfs -Recurse -File -Force | Select-Object -First 1)
	} catch {
		return $true
	}
}

function Test-ShareActive {
	$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
	if ($task -and $task.State -eq 'Running') { return $true }
	$config = Join-Path $ShareFolder 'rclone.conf'
	foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name = 'rclone.exe'")) {
		if ($process.CommandLine -and $process.CommandLine.Contains($config)) { return $true }
	}
	return $false
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

function Get-FinalPath($Path) {
	$final = [WnFinalPath]::Get($Path)
	if ($final.StartsWith($LongPathPrefix)) { $final = $final.Substring($LongPathPrefix.Length) }
	return $final.TrimEnd('\')
}

foreach ($folder in 'bin', 'downloads', 'keys', 'logs', 'shares') {
	New-Item -ItemType Directory -Force -Path (Join-Path $Root $folder) | Out-Null
}
$marker = Join-Path $ShareFolder 'remove'
$removing = Test-Path -LiteralPath $marker
if ($removing -and -not (Test-ShareActive) -and -not (Test-CacheFiles $ShareFolder)) {
	& cmd.exe /c rd /s /q ($LongPathPrefix + $ShareFolder)
	$removing = Test-Path -LiteralPath $marker
}
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
	Add-Type -TypeDefinition $FinalPathSource
	$parentFinal = Get-FinalPath $parent
	foreach ($folder in $env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:ProgramData) {
		$folderFinal = Get-FinalPath $folder
		if ($parentFinal -eq $folderFinal -or $parentFinal.StartsWith($folderFinal + '\', [StringComparison]::OrdinalIgnoreCase)) {
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
