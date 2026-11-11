using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using Microsoft.Win32;

namespace WnTools
{
	static class BatteryIcon
	{
		const int LowPercent = 20;
		const string ThemeKey = @"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize";
		const string ThemeValue = "SystemUsesLightTheme";

		[DllImport("user32.dll")]
		static extern bool DestroyIcon(IntPtr handle);

		public static Icon Draw(BatteryStatus status)
		{
			Size size = SystemInformation.SmallIconSize;
			using (var bitmap = new Bitmap(size.Width, size.Height))
			{
				using (Graphics graphics = Graphics.FromImage(bitmap))
					Paint(graphics, size, status);
				IntPtr handle = bitmap.GetHicon();
				try
				{
					return (Icon)Icon.FromHandle(handle).Clone();
				}
				finally
				{
					DestroyIcon(handle);
				}
			}
		}

		static void Paint(Graphics graphics, Size size, BatteryStatus status)
		{
			graphics.SmoothingMode = SmoothingMode.AntiAlias;
			Color ink = LightTaskbar() ? Color.Black : Color.White;
			float unit = size.Width / 16f;
			var body = new RectangleF(1 * unit, 4 * unit, 12 * unit, 8 * unit);
			var terminal = new RectangleF(13 * unit, 6.5f * unit, 2 * unit, 3 * unit);
			using (var pen = new Pen(ink, Math.Max(1f, unit)))
			using (var inkBrush = new SolidBrush(ink))
			{
				graphics.DrawRectangle(pen, body.X, body.Y, body.Width, body.Height);
				graphics.FillRectangle(inkBrush, terminal);
				if (status.State == BatteryState.Unreachable)
				{
					graphics.DrawLine(pen, body.Left + 3 * unit, body.Top + 2 * unit, body.Right - 3 * unit, body.Bottom - 2 * unit);
					graphics.DrawLine(pen, body.Left + 3 * unit, body.Bottom - 2 * unit, body.Right - 3 * unit, body.Top + 2 * unit);
					return;
				}
				if (status.State != BatteryState.Present)
					return;
				float inner = body.Width - 2 * unit;
				var fill = new RectangleF(body.X + unit, body.Y + unit, inner * status.Percent / 100f, body.Height - 2 * unit);
				Color fillColor = status.Percent <= LowPercent && !status.Charging ? Color.FromArgb(232, 17, 35) : Color.FromArgb(16, 160, 64);
				using (var fillBrush = new SolidBrush(fillColor))
					graphics.FillRectangle(fillBrush, fill);
				if (status.Charging)
					DrawLightning(graphics, body, unit, ink);
			}
		}

		static void DrawLightning(Graphics graphics, RectangleF body, float unit, Color ink)
		{
			float centerX = body.X + body.Width / 2;
			PointF[] bolt =
			{
				new PointF(centerX + 1 * unit, body.Top - 1 * unit),
				new PointF(centerX - 3 * unit, body.Top + 4.5f * unit),
				new PointF(centerX - 0.2f * unit, body.Top + 4.5f * unit),
				new PointF(centerX - 1 * unit, body.Bottom + 1 * unit),
				new PointF(centerX + 3 * unit, body.Top + 3.5f * unit),
				new PointF(centerX + 0.2f * unit, body.Top + 3.5f * unit),
			};
			Color outline = ink == Color.White ? Color.Black : Color.White;
			using (var boltBrush = new SolidBrush(Color.FromArgb(255, 200, 0)))
			using (var outlinePen = new Pen(outline, Math.Max(1f, unit * 0.75f)))
			{
				graphics.DrawPolygon(outlinePen, bolt);
				graphics.FillPolygon(boltBrush, bolt);
			}
		}

		static bool LightTaskbar()
		{
			using (RegistryKey key = Registry.CurrentUser.OpenSubKey(ThemeKey))
			{
				object value = key == null ? null : key.GetValue(ThemeValue);
				return value is int && (int)value == 1;
			}
		}
	}
}
