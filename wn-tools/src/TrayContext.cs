using System;
using System.Drawing;
using System.Net.Http;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace WnTools
{
	class TrayContext : ApplicationContext
	{
		const int PollIntervalMilliseconds = 30000;
		const int RetryIntervalMilliseconds = 5000;
		const int RequestTimeoutSeconds = 5;
		const int BalloonTimeoutMilliseconds = 5000;
		const string BalloonTitle = "Mac battery";

		readonly NotifyIcon notifyIcon;
		readonly Timer pollTimer;
		readonly HotkeyWindow hotkeyWindow;
		readonly HttpClient httpClient;
		BatteryStatus status = BatteryStatus.Unreachable;
		Task pollTask;
		string lastLogged;

		public TrayContext()
		{
			httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(RequestTimeoutSeconds) };
			var menu = new ContextMenuStrip();
			menu.Items.Add("&Speak battery status", null, delegate { RefreshAndShowBalloon(); });
			menu.Items.Add("&Quit", null, delegate { ExitThread(); });
			notifyIcon = new NotifyIcon { ContextMenuStrip = menu };
			notifyIcon.MouseClick += delegate(object sender, MouseEventArgs e)
			{
				if (e.Button == MouseButtons.Left)
					RefreshAndShowBalloon();
			};
			Show(status);
			notifyIcon.Visible = true;
			hotkeyWindow = new HotkeyWindow(RefreshAndShowBalloon);
			pollTimer = new Timer { Interval = PollIntervalMilliseconds };
			pollTimer.Tick += delegate { Poll(); };
			pollTimer.Start();
			Poll();
		}

		Task Poll()
		{
			if (pollTask == null || pollTask.IsCompleted)
				pollTask = PollOnce();
			return pollTask;
		}

		async Task PollOnce()
		{
			BatteryStatus next;
			string detail;
			Uri uri = null;
			try
			{
				uri = HostSettings.Load().BatteryUri();
				Log.Debug("GET " + uri);
				if (uri == null)
				{
					next = BatteryStatus.Unreachable;
					detail = "no IPv4 default gateway found";
				}
				else
				{
					string json = await httpClient.GetStringAsync(uri);
					Log.Debug("Response: " + json);
					next = BatteryStatus.Parse(json);
					detail = "ok";
				}
			}
			catch (Exception error)
			{
				next = BatteryStatus.Unreachable;
				detail = ErrorText(error);
			}
			string line = string.Format("{0}: {1} ({2})", uri, next.Text(), detail);
			if (Log.Verbose || line != lastLogged)
				Log.Write(line);
			lastLogged = line;
			pollTimer.Interval = next.State == BatteryState.Unreachable ? RetryIntervalMilliseconds : PollIntervalMilliseconds;
			Show(next);
		}

		static string ErrorText(Exception error)
		{
			string text = error.GetType().Name + ": " + error.Message;
			for (Exception inner = error.InnerException; inner != null; inner = inner.InnerException)
				text += " -> " + inner.GetType().Name + ": " + inner.Message;
			return text;
		}

		async void RefreshAndShowBalloon()
		{
			await Poll();
			ShowBalloon();
		}

		void Show(BatteryStatus next)
		{
			status = next;
			Icon old = notifyIcon.Icon;
			notifyIcon.Icon = BatteryIcon.Draw(status);
			notifyIcon.Text = status.Text();
			if (old != null)
				old.Dispose();
		}

		void ShowBalloon()
		{
			notifyIcon.ShowBalloonTip(BalloonTimeoutMilliseconds, BalloonTitle, status.Text(), ToolTipIcon.None);
		}

		protected override void ExitThreadCore()
		{
			pollTimer.Stop();
			hotkeyWindow.Dispose();
			notifyIcon.Visible = false;
			notifyIcon.Dispose();
			httpClient.Dispose();
			base.ExitThreadCore();
		}
	}
}
