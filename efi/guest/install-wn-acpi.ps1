# Copies the signed wn-acpi UEFI driver to the EFI System Partition and registers it as a Driver#### load option.
# Run as administrator or as SYSTEM. wn-acpi.efi and wn-sb.cer must be in the same folder as this script.

$ErrorActionPreference = 'Stop'

$LogPath = 'C:\wn-acpi-install.log'
$DriverFile = 'wn-acpi.efi'
$CertificateFile = 'wn-sb.cer'
$EspFolder = 'EFI\wn'
$EfiFilePath = '\EFI\wn\wn-acpi.efi'
$Description = 'wn-acpi'
$EspGptType = '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
$GlobalVariableGuid = '{8be4df61-93ca-11d2-aa0d-00e098032b8c}'
$VariableAttributes = 7
$LoadOptionActive = 1
$MaxProbedOption = 0xFF
$MaxVariableSize = 4096
$PreferredLetters = 'STUVWXYZRQPONMLKJIHGFE'

function Write-Log([string]$Message) {
	$line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
	Add-Content -Path $LogPath -Value $line
	Write-Host $line
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class WnFirmware {
	[StructLayout(LayoutKind.Sequential, Pack = 4)]
	struct TokenPrivileges {
		public uint Count;
		public long Luid;
		public uint Attributes;
	}

	const uint SePrivilegeEnabled = 2;
	const uint TokenAdjustPrivileges = 0x20;
	const uint TokenQuery = 0x8;

	[DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
	[DllImport("advapi32.dll", SetLastError = true)] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
	[DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern bool LookupPrivilegeValue(string system, string name, out long luid);
	[DllImport("advapi32.dll", SetLastError = true)] static extern bool AdjustTokenPrivileges(IntPtr token, bool disableAll, ref TokenPrivileges state, uint length, IntPtr previous, IntPtr returned);
	[DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern uint GetFirmwareEnvironmentVariableExW(string name, string guid, byte[] buffer, uint size, out uint attributes);
	[DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern bool SetFirmwareEnvironmentVariableExW(string name, string guid, byte[] value, uint size, uint attributes);

	public static void EnablePrivilege() {
		IntPtr token;
		if (!OpenProcessToken(GetCurrentProcess(), TokenAdjustPrivileges | TokenQuery, out token)) throw new System.ComponentModel.Win32Exception();
		TokenPrivileges state = new TokenPrivileges();
		state.Count = 1;
		state.Attributes = SePrivilegeEnabled;
		if (!LookupPrivilegeValue(null, "SeSystemEnvironmentPrivilege", out state.Luid)) throw new System.ComponentModel.Win32Exception();
		if (!AdjustTokenPrivileges(token, false, ref state, 0, IntPtr.Zero, IntPtr.Zero)) throw new System.ComponentModel.Win32Exception();
		int error = Marshal.GetLastWin32Error();
		if (error != 0) throw new System.ComponentModel.Win32Exception(error);
	}

	public static byte[] Read(string name, string guid, int maxSize) {
		byte[] buffer = new byte[maxSize];
		uint attributes;
		uint size = GetFirmwareEnvironmentVariableExW(name, guid, buffer, (uint)maxSize, out attributes);
		if (size == 0) return null;
		byte[] value = new byte[size];
		Array.Copy(buffer, value, size);
		return value;
	}

	public static void Write(string name, string guid, byte[] value, uint attributes) {
		uint size = value == null ? 0 : (uint)value.Length;
		if (!SetFirmwareEnvironmentVariableExW(name, guid, value, size, attributes)) throw new System.ComponentModel.Win32Exception();
	}
}
'@

function ConvertTo-HexString([byte[]]$Bytes) {
	($Bytes | ForEach-Object { $_.ToString('x2') }) -join ' '
}

function Get-OptionName([int]$Number) {
	'Driver{0:X4}' -f $Number
}

function Get-OptionDescription([byte[]]$Option) {
	$end = 6
	while ($end + 1 -lt $Option.Length -and ($Option[$end] -ne 0 -or $Option[$end + 1] -ne 0)) {
		$end += 2
	}
	[System.Text.Encoding]::Unicode.GetString($Option, 6, $end - 6)
}

function Get-OptionFilePath([byte[]]$Option) {
	$length = [BitConverter]::ToUInt16($Option, 4)
	$start = 6 + ([System.Text.Encoding]::Unicode.GetByteCount((Get-OptionDescription $Option)) + 2)
	$Option[$start..($start + $length - 1)]
}

function New-HardDriveNode($Partition, [int]$SectorSize) {
	$node = New-Object System.Collections.Generic.List[byte]
	$node.AddRange([byte[]](4, 1, 42, 0))
	$node.AddRange([BitConverter]::GetBytes([uint32]$Partition.PartitionNumber))
	$node.AddRange([BitConverter]::GetBytes([uint64]($Partition.Offset / $SectorSize)))
	$node.AddRange([BitConverter]::GetBytes([uint64]($Partition.Size / $SectorSize)))
	$node.AddRange(([Guid]$Partition.Guid).ToByteArray())
	$node.AddRange([byte[]](2, 2))
	$node.ToArray()
}

function New-FilePathNode([string]$Path) {
	$name = [System.Text.Encoding]::Unicode.GetBytes($Path + [char]0)
	$node = New-Object System.Collections.Generic.List[byte]
	$node.AddRange([byte[]](4, 4))
	$node.AddRange([BitConverter]::GetBytes([uint16](4 + $name.Length)))
	$node.AddRange($name)
	$node.ToArray()
}

function New-LoadOption([byte[]]$FilePath) {
	$option = New-Object System.Collections.Generic.List[byte]
	$option.AddRange([BitConverter]::GetBytes([uint32]$LoadOptionActive))
	$option.AddRange([BitConverter]::GetBytes([uint16]$FilePath.Length))
	$option.AddRange([System.Text.Encoding]::Unicode.GetBytes($Description + [char]0))
	$option.AddRange($FilePath)
	$option.ToArray()
}

function Get-FreeDriveLetter {
	$used = [System.IO.DriveInfo]::GetDrives() | ForEach-Object { $_.Name.Substring(0, 1).ToUpper() }
	foreach ($letter in $PreferredLetters.ToCharArray()) {
		if ($used -notcontains [string]$letter) {
			return [string]$letter
		}
	}
	throw 'no free drive letter for the EFI System Partition'
}

function Copy-ToEsp([string]$SourceFolder) {
	$letter = Get-FreeDriveLetter
	$drive = '{0}:' -f $letter
	Write-Log "mounting the EFI System Partition on $drive"
	& mountvol.exe $drive /S
	if ($LASTEXITCODE -ne 0) {
		throw "mountvol $drive /S failed with exit code $LASTEXITCODE"
	}
	try {
		$target = Join-Path "$drive\" $EspFolder
		New-Item -ItemType Directory -Force -Path $target | Out-Null
		foreach ($file in $DriverFile, $CertificateFile) {
			Copy-Item -Force -Path (Join-Path $SourceFolder $file) -Destination (Join-Path $target $file)
			Write-Log ('copied {0} to {1} ({2} bytes)' -f $file, $target, (Get-Item (Join-Path $target $file)).Length)
		}
	} finally {
		& mountvol.exe $drive /D
		Write-Log "unmounted $drive"
	}
}

function Get-DriverOrder {
	$order = [WnFirmware]::Read('DriverOrder', $GlobalVariableGuid, $MaxVariableSize)
	$numbers = New-Object System.Collections.Generic.List[int]
	if ($order) {
		Write-Log ('existing DriverOrder: {0}' -f (ConvertTo-HexString $order))
		for ($index = 0; $index + 1 -lt $order.Length; $index += 2) {
			$numbers.Add([BitConverter]::ToUInt16($order, $index))
		}
	}
	$numbers
}

function Find-Options([int[]]$DriverOrder) {
	$found = @{}
	$candidates = @(0..$MaxProbedOption) + $DriverOrder | Sort-Object -Unique
	foreach ($number in $candidates) {
		$option = [WnFirmware]::Read((Get-OptionName $number), $GlobalVariableGuid, $MaxVariableSize)
		if ($option) {
			$found[$number] = [byte[]]$option
		}
	}
	$found
}

function Register-Driver {
	$partition = Get-Partition | Where-Object { $_.GptType -eq $EspGptType } | Select-Object -First 1
	if (-not $partition) {
		throw 'no EFI System Partition found'
	}
	$sectorSize = (Get-Disk -Number $partition.DiskNumber).LogicalSectorSize
	Write-Log ('ESP: disk {0}, partition {1}, GUID {2}, offset {3}, size {4}, sector size {5}' -f $partition.DiskNumber, $partition.PartitionNumber, $partition.Guid, $partition.Offset, $partition.Size, $sectorSize)
	$filePath = [byte[]]((New-HardDriveNode $partition $sectorSize) + (New-FilePathNode $EfiFilePath) + [byte[]](0x7F, 0xFF, 4, 0))
	$option = New-LoadOption $filePath
	Write-Log ('new device path ({0} bytes): {1}' -f $filePath.Length, (ConvertTo-HexString $filePath))

	[WnFirmware]::EnablePrivilege()
	$driverOrder = @(Get-DriverOrder)
	$existing = Find-Options $driverOrder
	$ours = @($existing.Keys | Where-Object { (Get-OptionDescription $existing[$_]) -eq $Description } | Sort-Object)
	foreach ($number in $existing.Keys | Sort-Object) {
		Write-Log ('found {0} "{1}", {2} bytes' -f (Get-OptionName $number), (Get-OptionDescription $existing[$number]), $existing[$number].Length)
	}
	if ($ours.Count -gt 0) {
		$number = $ours[0]
		$oldPath = [byte[]](Get-OptionFilePath $existing[$number])
		Write-Log ('old device path of {0} ({1} bytes): {2}' -f (Get-OptionName $number), $oldPath.Length, (ConvertTo-HexString $oldPath))
	} else {
		$number = 0
		while ($existing.ContainsKey($number) -or $driverOrder -contains $number) {
			$number++
		}
	}
	foreach ($duplicate in $ours | Select-Object -Skip 1) {
		[WnFirmware]::Write((Get-OptionName $duplicate), $GlobalVariableGuid, $null, $VariableAttributes)
		Write-Log ('deleted duplicate {0}' -f (Get-OptionName $duplicate))
	}

	$name = Get-OptionName $number
	$current = $existing[$number]
	if ($current -and (ConvertTo-HexString $current) -eq (ConvertTo-HexString $option)) {
		Write-Log "$name is already up to date"
	} else {
		[WnFirmware]::Write($name, $GlobalVariableGuid, $option, $VariableAttributes)
		Write-Log ('wrote {0} ({1} bytes): {2}' -f $name, $option.Length, (ConvertTo-HexString $option))
	}

	$newOrder = @($number) + @($driverOrder | Where-Object { $_ -ne $number -and $ours -notcontains $_ })
	$orderBytes = [byte[]]($newOrder | ForEach-Object { [BitConverter]::GetBytes([uint16]$_) })
	[WnFirmware]::Write('DriverOrder', $GlobalVariableGuid, $orderBytes, $VariableAttributes)
	Write-Log ('wrote DriverOrder: {0}' -f (ConvertTo-HexString $orderBytes))

	$readBack = [WnFirmware]::Read($name, $GlobalVariableGuid, $MaxVariableSize)
	if ((ConvertTo-HexString $readBack) -ne (ConvertTo-HexString $option)) {
		throw "$name read back does not match what was written"
	}
	Write-Log "$name read back matches"
}

try {
	Write-Log ('install-wn-acpi started as {0}' -f [System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
	Copy-ToEsp $PSScriptRoot
	Register-Driver
	Write-Log 'install-wn-acpi finished'
} catch {
	Write-Log ('install-wn-acpi failed: {0}' -f $_)
	exit 1
}
