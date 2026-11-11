using System;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace WnTools
{
	class HotkeyWindow : NativeWindow, IDisposable
	{
		const int HotkeyId = 1;
		const int WmHotkey = 0x0312;
		const uint ModAlt = 0x0001;
		const uint ModControl = 0x0002;
		const uint ModNoRepeat = 0x4000;

		[DllImport("user32.dll", SetLastError = true)]
		static extern bool RegisterHotKey(IntPtr window, int id, uint modifiers, uint key);

		[DllImport("user32.dll")]
		static extern bool UnregisterHotKey(IntPtr window, int id);

		readonly Action pressed;

		public HotkeyWindow(Action pressed)
		{
			this.pressed = pressed;
			CreateHandle(new CreateParams());
			RegisterHotKey(Handle, HotkeyId, ModControl | ModAlt | ModNoRepeat, (uint)Keys.B);
		}

		protected override void WndProc(ref Message message)
		{
			if (message.Msg == WmHotkey && message.WParam.ToInt32() == HotkeyId)
				pressed();
			base.WndProc(ref message);
		}

		public void Dispose()
		{
			UnregisterHotKey(Handle, HotkeyId);
			DestroyHandle();
		}
	}
}
