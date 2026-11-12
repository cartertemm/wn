using System;
using System.Threading;
using System.Windows.Forms;

namespace WnTools
{
	static class Program
	{
		const string SingleInstanceMutexName = "Local\\wn-tools";
		const string DebugFlag = "--debug";

		[STAThread]
		static void Main(string[] arguments)
		{
			bool createdNew;
			using (var mutex = new Mutex(true, SingleInstanceMutexName, out createdNew))
			{
				if (!createdNew)
					return;
				Log.Start(Array.IndexOf(arguments, DebugFlag) >= 0);
				Application.EnableVisualStyles();
				Application.SetCompatibleTextRenderingDefault(false);
				Application.Run(new TrayContext());
			}
		}
	}
}
