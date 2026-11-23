$ShareFolder = Join-Path (Join-Path $Root 'shares') $Params.name
try {
	[IO.Directory]::Delete($ShareFolder)
} catch [IO.IOException] {
}
Write-Output 'wn-result: {}'
