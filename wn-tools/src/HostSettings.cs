using System;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Windows.Forms;

namespace WnTools
{
	// Reads wn-tools.ini next to the exe. Lines are key=value. Known keys: host, port.
	class HostSettings
	{
		public const int DefaultPort = 47100;
		const string IniFileName = "wn-tools.ini";

		public string ConfiguredHost { get; private set; }
		public int Port { get; private set; }

		public static HostSettings Load()
		{
			var settings = new HostSettings { Port = DefaultPort };
			string path = Path.Combine(Path.GetDirectoryName(Application.ExecutablePath), IniFileName);
			if (!File.Exists(path))
				return settings;
			foreach (string rawLine in File.ReadAllLines(path))
			{
				string line = rawLine.Trim();
				int equals = line.IndexOf('=');
				if (line.Length == 0 || line.StartsWith(";") || line.StartsWith("#") || line.StartsWith("[") || equals < 0)
					continue;
				string key = line.Substring(0, equals).Trim().ToLowerInvariant();
				string value = line.Substring(equals + 1).Trim();
				int port;
				if (key == "host" && value.Length > 0)
					settings.ConfiguredHost = value;
				else if (key == "port" && int.TryParse(value, out port))
					settings.Port = port;
			}
			return settings;
		}

		// The Mac is the default gateway of the UTM shared network adapter.
		public string Host()
		{
			if (ConfiguredHost != null)
				return ConfiguredHost;
			foreach (NetworkInterface adapter in NetworkInterface.GetAllNetworkInterfaces())
			{
				if (adapter.OperationalStatus != OperationalStatus.Up || adapter.NetworkInterfaceType == NetworkInterfaceType.Loopback)
					continue;
				foreach (GatewayIPAddressInformation gateway in adapter.GetIPProperties().GatewayAddresses)
				{
					IPAddress address = gateway.Address;
					if (address.AddressFamily == AddressFamily.InterNetwork && !address.Equals(IPAddress.Any))
						return address.ToString();
				}
			}
			return null;
		}

		public Uri BatteryUri()
		{
			string host = Host();
			return host == null ? null : new Uri(string.Format("http://{0}:{1}/battery", host, Port));
		}
	}
}
