$ShareFolder = Join-Path 'C:\ProgramData\wn\shares' $Params.name
$removed = $true
try {
	[IO.Directory]::Delete($ShareFolder)
} catch [IO.IOException] {
	$removed = $false
}
Write-Output ('wn-result: ' + (@{ removed = $removed } | ConvertTo-Json -Compress))
