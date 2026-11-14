# The microphone codec (QEMU hda-micro, on UTM's SPICE audio) also has a speaker, and Windows may pick it as
# the default playback device. This hides that speaker so sound stays on the low latency CoreAudio codec.
# Run as administrator or as SYSTEM.

$MicCodec = 'DEV_0032'
$RenderKey = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render'
$TopologyValue = '{b3f8fa53-0004-438e-9003-51a46e139bfc},2'
$RenderPrefix = '{0.0.0.00000000}.'
$WaitSeconds = 60

Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
[Guid("F8679F50-850A-41CF-9C72-430F290290C8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPolicyConfig {
	int GetMixFormat(); int GetDeviceFormat(); int ResetDeviceFormat(); int SetDeviceFormat();
	int GetProcessingPeriod(); int SetProcessingPeriod(); int GetShareMode(); int SetShareMode();
	int GetPropertyValue(); int SetPropertyValue(); int SetDefaultEndpoint();
	int SetEndpointVisibility([MarshalAs(UnmanagedType.LPWStr)] string id, int visible);
}
[ComImport, Guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")] class PolicyConfigClient {}
public static class EndpointPolicy {
	public static int Hide(string id) { return ((IPolicyConfig)new PolicyConfigClient()).SetEndpointVisibility(id, 0); }
}
'@

function Get-MicSpeakers {
	Get-ChildItem $RenderKey -ErrorAction SilentlyContinue | Where-Object {
		(Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue).$TopologyValue -match $MicCodec
	}
}

for ($second = 0; $second -lt $WaitSeconds -and -not (Get-MicSpeakers); $second++) {
	Start-Sleep -Seconds 1
}
$speakers = @(Get-MicSpeakers)
if (-not $speakers) {
	Write-Host 'hide-mic-speaker: no hda-micro speaker found'
}
foreach ($speaker in $speakers) {
	$result = [EndpointPolicy]::Hide($RenderPrefix + $speaker.PSChildName)
	Write-Host ('hide-mic-speaker: hid {0}, result 0x{1:x}' -f $speaker.PSChildName, $result)
}
