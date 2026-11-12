# Latency test agent. Listens on UDP 47000 and answers on UDP 47001 to the sender.
#   beep [HZ]  replies "ack beep", then plays [console]::Beep(HZ, 150). HZ defaults to 1000.
#   ding       replies "ack ding", then plays C:\Windows\Media\Windows Ding.wav.
#   key        drops pending keys, replies "ack key", waits for one key press,
#              then replies "key <char>". The window must have focus.
#   ping       replies "pong".
$listenPort = 47000
$replyPort = 47001
$beepMilliseconds = 150
$defaultBeepHz = 1000

$udp = New-Object System.Net.Sockets.UdpClient $listenPort
$player = New-Object System.Media.SoundPlayer "C:\Windows\Media\Windows Ding.wav"
$player.Load()
$remote = New-Object System.Net.IPEndPoint ([System.Net.IPAddress]::Any), 0

function Send-Reply([string]$text, [System.Net.IPAddress]$address) {
	$bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
	$target = New-Object System.Net.IPEndPoint $address, $replyPort
	[void]$udp.Send($bytes, $bytes.Length, $target)
}

Write-Host "latency agent listening on UDP $listenPort. Keep this window focused for key tests."
while ($true) {
	$words = [System.Text.Encoding]::UTF8.GetString($udp.Receive([ref]$remote)).Trim().Split(' ')
	$address = $remote.Address
	switch ($words[0]) {
		'beep' {
			$hz = $defaultBeepHz
			if ($words.Length -gt 1) { $hz = [int]$words[1] }
			Send-Reply 'ack beep' $address
			[console]::Beep($hz, $beepMilliseconds)
		}
		'ding' {
			Send-Reply 'ack ding' $address
			$player.Play()
		}
		'key' {
			while ([console]::KeyAvailable) { [void][console]::ReadKey($true) }
			Send-Reply 'ack key' $address
			$key = [console]::ReadKey($true)
			Send-Reply ("key " + $key.KeyChar) $address
		}
		'ping' { Send-Reply 'pong' $address }
	}
}
