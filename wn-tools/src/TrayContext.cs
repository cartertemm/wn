using System;
using System.Drawing;
using System.Net.Http;
using System.Windows.Forms;

namespace WnTools
{
	class TrayContext : ApplicationContext
	{
		const int PollIntervalMilliseconds = 30000;
		const int RequestTimeoutSeconds = 5;
		const int BalloonTimeoutMilliseconds = 5000;
		const string BalloonTitle = "Mac battery";

		readonly HostSettings settings;
		readonly NotifyIcon notifyIcon;
		readonly Timer pollTimer;
		readonly HotkeyWindow hotkeyWindow;
		readonly HttpClient httpClient;
		BatteryStatus status = BatteryStatus.Unreachable;
		bool polling;

		public TrayContext(HostSettings settings)
		{
			this.settings = settings;
			httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(RequestTimeoutSeconds) };
			var menu = new ContextMenuStrip();
			menu.Items.Add("&Speak battery status", null, delegate { ShowBalloon(); });
			menu.Items.Add("&Quit", null, delegate { ExitThread(); });
			notifyIcon = new NotifyIcon { ContextMenuStrip = menu };
			notifyIcon.MouseClick += delegate(object sender, MouseEventArgs e)
			{
				if (e.Button == MouseButtons.Left)
					ShowBalloon();
			};
			Show(status);
			notifyIcon.Visible = true;
			hotkeyWindow = new HotkeyWindow(ShowBalloon);
			pollTimer = new Timer { Interval = PollIntervalMilliseconds };
			pollTimer.Tick += delegate { Poll(); };
			pollTimer.Start();
			Poll();
		}

		async void Poll()
		{
			if (polling)
				return;
			polling = true;
			BatteryStatus next;
			try
			{
				Uri uri = settings.BatteryUri();
				next = uri == null ? BatteryStatus.Unreachable : BatteryStatus.Parse(await httpClient.GetStringAsync(uri));
			}
			catch (Exception)
			{
				next = BatteryStatus.Unreachable;
			}
			finally
			{
				polling = false;
			}
			Show(next);
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
