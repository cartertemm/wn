// Windows service that queries the wn ACPI-WMI data block once a second. Each query runs the WQAA
// method in the wn-acpi SSDT, which notifies the battery and the AC adapter when the Mac changed the
// shared battery block, so Windows reads the new state.

using System;
using System.Runtime.InteropServices;
using System.ServiceProcess;
using System.Threading;

class WnBatteryNotify : ServiceBase {
	const string ServiceId = "wn-battery-notify";
	const string BlockGuid = "dacc92c7-b762-4ee1-a96c-de99be6cc7b6";
	const uint WmiGuidQuery = 1;
	const int PollMilliseconds = 1000;
	const int BufferSize = 4096;

	[DllImport("advapi32.dll")] static extern int WmiOpenBlock(ref Guid guid, uint access, out IntPtr handle);
	[DllImport("advapi32.dll")] static extern int WmiQueryAllDataW(IntPtr handle, ref uint size, byte[] buffer);
	[DllImport("advapi32.dll")] static extern int WmiCloseBlock(IntPtr handle);

	readonly ManualResetEvent stopping = new ManualResetEvent(false);
	Thread worker;

	WnBatteryNotify() {
		ServiceName = ServiceId;
	}

	protected override void OnStart(string[] args) {
		worker = new Thread(Poll);
		worker.Start();
	}

	protected override void OnStop() {
		stopping.Set();
		worker.Join();
	}

	void Poll() {
		Guid guid = new Guid(BlockGuid);
		byte[] buffer = new byte[BufferSize];
		IntPtr handle = IntPtr.Zero;
		do {
			if (handle == IntPtr.Zero && WmiOpenBlock(ref guid, WmiGuidQuery, out handle) != 0) {
				handle = IntPtr.Zero;
				continue;
			}
			uint size = BufferSize;
			if (WmiQueryAllDataW(handle, ref size, buffer) != 0) {
				WmiCloseBlock(handle);
				handle = IntPtr.Zero;
			}
		} while (!stopping.WaitOne(PollMilliseconds));
		if (handle != IntPtr.Zero) {
			WmiCloseBlock(handle);
		}
	}

	static void Main() {
		Run(new WnBatteryNotify());
	}
}
