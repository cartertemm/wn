using System;
using System.IO;
using System.Windows.Forms;

namespace WnTools
{
	// Writes %LOCALAPPDATA%\wn-tools\wn-tools.log. The file starts over at each launch.
	static class Log
	{
		const string FileName = "wn-tools.log";

		static string path;

		public static bool Verbose { get; private set; }

		public static void Start(bool verbose)
		{
			Verbose = verbose;
			string directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "wn-tools");
			try
			{
				Directory.CreateDirectory(directory);
				path = Path.Combine(directory, FileName);
				File.WriteAllText(path, "");
			}
			catch (Exception)
			{
				path = null;
			}
			Write("Started " + Application.ExecutablePath + (verbose ? " with --debug" : ""));
		}

		public static void Write(string message)
		{
			if (path == null)
				return;
			try
			{
				File.AppendAllText(path, DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + Environment.NewLine);
			}
			catch (Exception)
			{
			}
		}

		public static void Debug(string message)
		{
			if (Verbose)
				Write(message);
		}
	}
}
