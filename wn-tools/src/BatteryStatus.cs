using System;
using System.Collections.Generic;
using System.Web.Script.Serialization;

namespace WnTools
{
	enum BatteryState { Unreachable, NoBattery, Present }

	class BatteryStatus
	{
		const int MaxTrayTextLength = 63;
		const int MinutesPerHour = 60;

		public BatteryState State { get; private set; }
		public int Percent { get; private set; }
		public bool Charging { get; private set; }
		public bool OnAc { get; private set; }
		public int? MinutesRemaining { get; private set; }

		public static readonly BatteryStatus Unreachable = new BatteryStatus { State = BatteryState.Unreachable };

		public static BatteryStatus Parse(string json)
		{
			var values = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(json);
			if (!(Get(values, "present") is bool) || !(bool)Get(values, "present"))
				return new BatteryStatus { State = BatteryState.NoBattery };
			object minutes = Get(values, "minutes_remaining");
			return new BatteryStatus
			{
				State = BatteryState.Present,
				Percent = Math.Max(0, Math.Min(100, Convert.ToInt32(Get(values, "percent") ?? 0))),
				Charging = Get(values, "charging") is bool && (bool)Get(values, "charging"),
				OnAc = Get(values, "on_ac") is bool && (bool)Get(values, "on_ac"),
				MinutesRemaining = minutes == null ? (int?)null : Convert.ToInt32(minutes),
			};
		}

		static object Get(Dictionary<string, object> values, string key)
		{
			object value;
			return values.TryGetValue(key, out value) ? value : null;
		}

		public string Text()
		{
			if (State == BatteryState.Unreachable)
				return "Battery: host not reachable";
			if (State == BatteryState.NoBattery)
				return "No battery";
			string power = Charging ? "charging" : OnAc ? "plugged in" : "on battery";
			string text = string.Format("Battery {0}%, {1}", Percent, power);
			if (MinutesRemaining.HasValue && MinutesRemaining.Value > 0)
				text += ", " + Duration(MinutesRemaining.Value) + " left";
			return text.Length > MaxTrayTextLength ? text.Substring(0, MaxTrayTextLength) : text;
		}

		static string Duration(int minutes)
		{
			int hours = minutes / MinutesPerHour;
			int rest = minutes % MinutesPerHour;
			if (hours == 0)
				return rest + " min";
			if (rest == 0)
				return hours + " h";
			return string.Format("{0} h {1} min", hours, rest);
		}
	}
}
