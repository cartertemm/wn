using System;
using System.Threading;
using System.Windows.Forms;

namespace WnTools
{
	static class Program
	{
		const string SingleInstanceMutexName = "Local\\wn-tools";

		[STAThread]
		static void Main()
		{
			bool createdNew;
			using (var mutex = new Mutex(true, SingleInstanceMutexName, out createdNew))
			{
				if (!createdNew)
					return;
				Application.EnableVisualStyles();
				Application.SetCompatibleTextRenderingDefault(false);
				Application.Run(new TrayContext(HostSettings.Load()));
			}
		}
	}
}
