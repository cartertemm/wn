import io
import json
import os
import plistlib
import unittest
from unittest import mock

from fakes import short_temporary_directory
from test_efivars import secure_flash
from wn.backend import VMStatus
from wn.backends.utm import UTMCtlError
from wn.downloads import Downloads
from wn.efivars import EFI_IMAGE_SECURITY_DATABASE_GUID, find, parse_signature_lists, qcow2_image, read_flash
from wn.authenticode import file_authenticode_sha256
from wn.payload import BATTERY_DRIVER
from wn.qmp import QMPConnectionError
from wn.setup import (
	FINAL_DISPLAY,
	INSTALL_TIMEOUT_SECONDS,
	PAYLOAD_IMAGE,
	WINDOWS_ISO_IMAGE,
	Installer,
	SetupError,
	SetupOptions,
	clone_file,
	restart_utm,
)
from wn.unattend import SETUP_DONE_MARKER
from wn.utm_config import DEFAULT_AUDIO_ARGUMENTS, UTMConfigError


class FakeClock:
	def __init__(self):
		self.now = 1000.0

	def __call__(self):
		return self.now

	def sleep(self, seconds):
		self.now += seconds


class ScriptedBackend:
	"""Answers like UTMBackend. ``ip_after`` and ``done_after`` are clock times when the guest gets there."""

	def __init__(self, clock, ip_after=600.0, done_after=900.0, known=False):
		self.clock = clock
		self.ip_after = ip_after
		self.done_after = done_after
		self.known = known
		self.state = "stopped"
		self.calls = []
		self.start_time = None

	def exists(self):
		return self.known

	def machines(self):
		return [("UUID", "stopped", "Windows")] if self.known else []

	def start(self):
		self.calls.append("start")
		self.state = "running"
		self.known = True
		if self.start_time is None:
			self.start_time = self.clock()

	def stop(self, force=False):
		self.calls.append("stop --force" if force else "stop")
		self.state = "stopped"

	def status(self):
		return VMStatus(name="Windows", state=self.state)

	def elapsed(self):
		return self.clock() - self.start_time if self.start_time is not None else -1

	def ip(self):
		if self.state == "running" and self.elapsed() >= self.ip_after:
			return "192.168.64.7"
		raise UTMCtlError("utmctl reported no IPv4 address")

	def pull_file(self, path):
		self.calls.append(("pull", path))
		if self.elapsed() >= self.done_after:
			return "done\n"
		if self.elapsed() >= self.ip_after:
			return "rebooting\n"
		raise UTMCtlError("cannot open file")

	def qmp_socket_path(self, session=False):
		return "tcp:127.0.0.1:4444"


class FakeQMP:
	def __init__(self, log, refusals=0):
		self.log = log
		self.refusals = refusals

	def __call__(self, address):
		return self

	def connect(self):
		if self.refusals:
			self.refusals -= 1
			raise QMPConnectionError("refused")
		self.log.append("connect")

	def close(self):
		self.log.append("close")

	def __enter__(self):
		self.connect()
		return self

	def __exit__(self, *exc_info):
		self.close()

	def execute(self, command, arguments=None):
		self.log.append((command, arguments))


class InstallerTestCase(unittest.TestCase):
	def setUp(self):
		self.directory = short_temporary_directory()
		self.addCleanup(self.directory.cleanup)
		root = self.directory.name
		home_patch = mock.patch.dict(os.environ, {"HOME": root})
		home_patch.start()
		self.addCleanup(home_patch.stop)
		self.bundle_dir = os.path.join(root, "bundles")
		os.makedirs(self.bundle_dir)
		self.windows_iso = os.path.join(root, "win.iso")
		with open(self.windows_iso, "wb") as iso:
			iso.write(b"windows")
		self.clock = FakeClock()
		self.backend = ScriptedBackend(self.clock)
		self.qmp_log = []
		self.restarts = []
		self.payload_calls = []
		self.fetch_calls = []
		self.agent_installs = []
		self.output = io.StringIO()
		self.group_container = os.path.join(root, "Library", "Group Containers", "WDNLXAD4W8.com.utmapp.UTM")
		os.makedirs(self.group_container)
		template = os.path.join(root, "edk2-arm-secure-vars.fd")
		with open(template, "wb") as template_file:
			template_file.write(qcow2_image(secure_flash()))
		template_patch = mock.patch("wn.utm_config.SECURE_VARS_TEMPLATE", template)
		template_patch.start()
		self.addCleanup(template_patch.stop)
		isdir_patch = mock.patch("wn.setup.os.path.isdir", side_effect=lambda path: path == "/Applications/UTM.app" or os.path.exists(path))
		isdir_patch.start()
		self.addCleanup(isdir_patch.stop)
		screen_patch = mock.patch("wn.setup.Installer.desktop_drawn", return_value=True)
		self.desktop_drawn = screen_patch.start()
		self.addCleanup(screen_patch.stop)
		self.free_bytes = 10 ** 15
		usage_patch = mock.patch("wn.setup.shutil.disk_usage", side_effect=lambda path: mock.Mock(free=self.free_bytes))
		self.disk_usage = usage_patch.start()
		self.addCleanup(usage_patch.stop)

	def fetch(self, downloads_dir, include_nvda=True, report=None):
		self.fetch_calls.append((downloads_dir, include_nvda))
		return Downloads(windows_iso=self.windows_iso, virtio_iso="virtio.iso", guest_tools_iso="tools.iso", nvda_installer="nvda.exe" if include_nvda else None)

	def build_payload(self, image_path, unattend_options, virtio_iso, guest_tools_iso, nvda_installer, battery=True):
		self.payload_calls.append((unattend_options, virtio_iso, guest_tools_iso, nvda_installer, battery))
		with open(image_path, "wb") as image:
			image.write(b"payload")

	def restart_utm(self):
		self.restarts.append(self.clock())
		self.backend.known = True

	def install_agent(self):
		self.agent_installs.append(self.clock())
		return os.path.join(self.directory.name, "Library", "LaunchAgents", "com.wn.agent.plist")

	def installer(self, **overrides):
		values = {"name": "Windows", "username": "carter", "bundle_dir": self.bundle_dir, "downloads_dir": "/dl"}
		values.update(overrides)
		return Installer(
			SetupOptions(**values), self.output, backend=self.backend, fetch=self.fetch, build_payload=self.build_payload,
			restart=self.restart_utm, qmp_client_factory=FakeQMP(self.qmp_log),
			clock=self.clock, sleep=self.clock.sleep, install_agent=self.install_agent,
		)

	def bundle_config(self):
		with open(os.path.join(self.bundle_dir, "Windows.utm", "config.plist"), "rb") as config:
			return plistlib.load(config)


class RunTests(InstallerTestCase):
	def test_full_run(self):
		address = self.installer().run()
		self.assertEqual(address, "192.168.64.7")
		self.assertEqual(self.fetch_calls, [("/dl", True)])
		unattend_options, virtio_iso, guest_tools_iso, nvda, _ = self.payload_calls[0]
		self.assertEqual((unattend_options.username, unattend_options.computer_name, unattend_options.edition), ("carter", "WINDOWS", "Pro"))
		self.assertEqual((virtio_iso, guest_tools_iso, nvda), ("virtio.iso", "tools.iso", "nvda.exe"))
		self.assertEqual(self.backend.calls.count("start"), 2)
		self.assertIn("stop", self.backend.calls)
		self.assertEqual(len(self.restarts), 2)
		self.assertIn(("pull", SETUP_DONE_MARKER), self.backend.calls)

	def test_final_bundle(self):
		self.installer().run()
		config = self.bundle_config()
		self.assertEqual([display["Hardware"] for display in config["Display"]], [FINAL_DISPLAY])
		self.assertEqual([drive["ImageType"] for drive in config["Drive"]], ["Disk"])
		data = os.listdir(os.path.join(self.bundle_dir, "Windows.utm", "Data"))
		self.assertEqual(sorted(data), ["disk.qcow2", "efi_vars.fd"])
		self.assertEqual(config["QEMU"]["MachinePropertyOverride"], "kernel-irqchip=off")
		audio = list(DEFAULT_AUDIO_ARGUMENTS)
		self.assertEqual(config["QEMU"]["AdditionalArguments"][4:4 + len(audio)], audio)

	def test_battery_is_on_by_default(self):
		self.installer().run()
		arguments = self.bundle_config()["QEMU"]["AdditionalArguments"]
		self.assertEqual(arguments[arguments.index("-machine") + 1], "nvdimm=on")
		self.assertIn("maxmem=8193M,slots=1", arguments)
		self.assertIn("memory-backend-file,id=wnbattery,share=on,mem-path=wn-battery-Windows.bin,size=1M", arguments)
		self.assertIn("nvdimm,id=wnbatterynv,memdev=wnbattery", arguments)
		with open(os.path.join(self.group_container, "wn-battery-Windows.bin"), "rb") as battery_file:
			self.assertEqual(battery_file.read(4), b"WNBT")
		self.assertTrue(self.payload_calls[0][4])

	def test_battery_file_exists_before_the_first_start(self):
		path = os.path.join(self.group_container, "wn-battery-Windows.bin")
		original_start = self.backend.start
		seen = []

		def start():
			seen.append(os.path.exists(path))
			original_start()
		self.backend.start = start
		self.installer().run()
		self.assertTrue(seen[0])

	def test_vars_file_has_the_driver_hash_in_db(self):
		self.installer().run()
		flash = read_flash(os.path.join(self.bundle_dir, "Windows.utm", "Data", "efi_vars.fd"))
		lists = parse_signature_lists(find(flash, "db", EFI_IMAGE_SECURITY_DATABASE_GUID).data)
		self.assertEqual(lists[-1][1][0][1], file_authenticode_sha256(BATTERY_DRIVER))

	def test_installs_the_agent_and_says_how_to_remove_it(self):
		self.installer().run()
		self.assertEqual(len(self.agent_installs), 1)
		output = self.output.getvalue()
		self.assertIn("com.wn.agent.plist", output)
		self.assertIn("python3 -m wn agent --uninstall", output)
		self.assertIn(os.path.join(self.group_container, "wn-battery-Windows.bin"), output)
		self.assertNotIn("Warning", output)

	def test_warns_when_the_agent_writes_another_file(self):
		path = os.path.join(self.directory.name, ".config", "wn", "config.json")
		os.makedirs(os.path.dirname(path))
		with open(path, "w") as config_file:
			json.dump({"vm_name": "Other"}, config_file)
		self.installer().run()
		self.assertIn("Warning: the VM reads", self.output.getvalue())

	def test_warns_when_the_agent_uses_another_camera_socket(self):
		path = os.path.join(self.directory.name, ".config", "wn", "config.json")
		os.makedirs(os.path.dirname(path))
		with open(path, "w") as config_file:
			json.dump({"vm_name": "Other"}, config_file)
		self.installer(battery=False).run()
		output = self.output.getvalue()
		self.assertIn("Warning: the VM listens on {}, but the agent connects to {}".format(
			os.path.join(self.group_container, "wn-Windows-camera.sock"), os.path.join(self.group_container, "wn-Other-camera.sock")), output)

	def test_no_battery(self):
		self.installer(battery=False, camera=False).run()
		arguments = self.bundle_config()["QEMU"]["AdditionalArguments"]
		self.assertNotIn("nvdimm=on", arguments)
		self.assertEqual(os.listdir(os.path.join(self.bundle_dir, "Windows.utm", "Data")), ["disk.qcow2"])
		self.assertEqual(os.listdir(self.group_container), [])
		self.assertFalse(self.payload_calls[0][4])
		self.assertEqual(self.agent_installs, [])

	def test_camera_is_on_by_default(self):
		self.installer().run()
		arguments = self.bundle_config()["QEMU"]["AdditionalArguments"]
		self.assertIn("usb-redir,chardev=wncam,id=wncam0,bus=wncam-xhci.0", arguments)
		self.assertIn("macOS will ask whether wn-agent may use the camera. Click Allow.", self.output.getvalue())

	def test_camera_without_battery_still_installs_the_agent(self):
		self.installer(battery=False).run()
		self.assertEqual(len(self.agent_installs), 1)
		output = self.output.getvalue()
		self.assertIn("Click Allow", output)
		self.assertNotIn("battery status", output)
		self.assertNotIn("Warning", output)

	def test_no_camera(self):
		self.installer(camera=False).run()
		arguments = self.bundle_config()["QEMU"]["AdditionalArguments"]
		self.assertFalse(any("wncam" in argument for argument in arguments))
		self.assertNotIn("Click Allow", self.output.getvalue())
		with open(os.path.join(self.directory.name, ".config", "wn", "config.json")) as config_file:
			config = json.load(config_file)
		self.assertFalse(config["camera"])
		self.assertTrue(config["battery_sync"])

	def test_camera_without_battery_turns_battery_sync_off(self):
		self.installer(battery=False).run()
		with open(os.path.join(self.directory.name, ".config", "wn", "config.json")) as config_file:
			config = json.load(config_file)
		self.assertTrue(config["camera"])
		self.assertFalse(config["battery_sync"])

	def test_no_battery_and_no_camera_leave_the_agent_settings_alone(self):
		self.installer(battery=False, camera=False).run()
		with open(os.path.join(self.directory.name, ".config", "wn", "config.json")) as config_file:
			self.assertEqual(json.load(config_file), {"backend": "utm", "vm_name": "Windows"})

	def test_other_vm_keeps_the_agent_settings(self):
		path = os.path.join(self.directory.name, ".config", "wn", "config.json")
		os.makedirs(os.path.dirname(path))
		with open(path, "w") as config_file:
			json.dump({"vm_name": "Other", "battery_sync": True}, config_file)
		self.installer(battery=False).run()
		with open(path) as config_file:
			self.assertEqual(json.load(config_file), {"vm_name": "Other", "battery_sync": True})
		self.assertIn("Warning: the agent serves VM 'Other', so wn setup left camera and battery_sync in {} as they were".format(path), self.output.getvalue())

	def test_no_battery_and_no_camera_with_the_agent_installed_says_how_to_remove_it(self):
		agent_path = os.path.join(self.directory.name, "Library", "LaunchAgents", "com.wn.agent.plist")
		os.makedirs(os.path.dirname(agent_path))
		open(agent_path, "w").close()
		self.installer(battery=False, camera=False).run()
		self.assertIn("If you no longer want the wn agent, run python3 -m wn agent --uninstall", self.output.getvalue())
		with open(os.path.join(self.directory.name, ".config", "wn", "config.json")) as config_file:
			self.assertEqual(json.load(config_file), {"backend": "utm", "vm_name": "Windows"})

	def test_no_battery_and_no_camera_without_the_agent_says_nothing_about_it(self):
		self.installer(battery=False, camera=False).run()
		self.assertNotIn("--uninstall", self.output.getvalue())

	def test_falls_back_to_plain_ramfb_when_desktop_stays_blank(self):
		self.desktop_drawn.return_value = False
		self.installer().run()
		self.assertEqual([display["Hardware"] for display in self.bundle_config()["Display"]], ["ramfb"])
		self.assertEqual(self.backend.calls.count("start"), 3)
		self.assertIn("Switching to ramfb", self.output.getvalue())

	def test_headless_keeps_no_display(self):
		self.installer(headless=True).run()
		config = self.bundle_config()
		self.assertEqual(config["Display"], [])
		self.desktop_drawn.assert_not_called()

	def test_boot_key_burst(self):
		self.installer().run()
		keys = [entry for entry in self.qmp_log if isinstance(entry, tuple) and entry[0] == "send-key"]
		self.assertEqual(len(keys), 30)
		self.assertEqual(keys[0][1], {"keys": [{"type": "qcode", "data": "ret"}]})

	def test_waits_for_qmp_port(self):
		installer = self.installer()
		installer.qmp_client_factory = FakeQMP(self.qmp_log, refusals=3)
		installer.run()
		self.assertIn("connect", self.qmp_log)

	def test_without_nvda(self):
		self.installer(software=()).run()
		self.assertEqual(self.fetch_calls, [("/dl", False)])
		self.assertIsNone(self.payload_calls[0][3])

	def test_writes_config_when_absent(self):
		self.installer(name="Windows").run()
		with open(os.path.join(self.directory.name, ".config", "wn", "config.json")) as config_file:
			self.assertEqual(json.load(config_file), {"backend": "utm", "vm_name": "Windows", "camera": True, "battery_sync": True})

	def test_keeps_existing_config(self):
		path = os.path.join(self.directory.name, ".config", "wn", "config.json")
		os.makedirs(os.path.dirname(path))
		with open(path, "w") as config_file:
			config_file.write('{"key_delay_ms": 30}')
		self.installer().run()
		with open(path) as config_file:
			self.assertEqual(json.load(config_file), {"key_delay_ms": 30, "camera": True, "battery_sync": True})

	def test_progress_lines_have_elapsed_time(self):
		self.installer().run()
		lines = [line for line in self.output.getvalue().splitlines() if line.startswith("[")]
		self.assertTrue(any("Windows Setup is installing" in line for line in lines))
		self.assertTrue(any("Restarting into the finished system" in line for line in lines))
		self.assertRegex(lines[-1], r"^\[\d\d:\d\d\] Done$")
		self.assertIn("IP address: 192.168.64.7", self.output.getvalue())


class FailureTests(InstallerTestCase):
	def test_existing_vm(self):
		self.backend.known = True
		with self.assertRaises(SetupError):
			self.installer().run()
		self.assertEqual(self.fetch_calls, [])

	def test_vm_name_too_long_for_the_qmp_sockets(self):
		with self.assertRaises(UTMConfigError):
			self.installer(name="W" * 100).run()
		self.assertEqual(self.fetch_calls, [])

	def test_other_vm_running(self):
		self.backend.machines = lambda: [("UUID", "started", "Linux")]
		with self.assertRaises(SetupError) as context:
			self.installer().run()
		self.assertIn("Linux", str(context.exception))

	def test_vm_started_during_download_blocks_the_restart(self):
		def fetch_then_start_vm(*arguments, **keywords):
			self.backend.machines = lambda: [("UUID", "started", "Linux")]
			return self.fetch(*arguments, **keywords)
		installer = self.installer()
		installer.fetch = fetch_then_start_vm
		with self.assertRaises(SetupError) as context:
			installer.run()
		self.assertIn("Linux", str(context.exception))
		self.assertEqual(self.restarts, [])

	def test_vm_started_during_install_blocks_the_second_restart(self):
		original_stop = self.backend.stop

		def stop_then_start_other_vm(force=False):
			original_stop(force)
			self.backend.machines = lambda: [("UUID", "stopped", "Windows"), ("UUID2", "started", "Linux")]
		self.backend.stop = stop_then_start_other_vm
		with self.assertRaises(SetupError) as context:
			self.installer().run()
		self.assertIn("Linux", str(context.exception))
		self.assertEqual(len(self.restarts), 1)

	def test_not_enough_free_space(self):
		self.free_bytes = 10 * 1000 ** 3
		with self.assertRaises(SetupError) as context:
			self.installer().run()
		self.assertIn("GB free", str(context.exception))
		self.assertEqual(self.fetch_calls, [])

	def test_cached_downloads_count_as_free_space(self):
		downloads_dir = os.path.join(self.directory.name, "downloads")
		os.makedirs(downloads_dir)
		with open(os.path.join(downloads_dir, "win.iso"), "wb") as cached:
			cached.write(b"x" * 60)
		with mock.patch("wn.setup.DOWNLOAD_SPACE_BYTES", 100), mock.patch("wn.setup.BUNDLE_SPACE_BYTES", 1000):
			self.free_bytes = 1040
			self.installer(downloads_dir=downloads_dir).check_free_space()
			self.free_bytes = 1039
			with self.assertRaises(SetupError):
				self.installer(downloads_dir=downloads_dir).check_free_space()

	def test_existing_bundle(self):
		os.makedirs(os.path.join(self.bundle_dir, "Windows.utm"))
		with self.assertRaises(SetupError):
			self.installer().run()

	def test_timeout(self):
		self.backend.ip_after = INSTALL_TIMEOUT_SECONDS * 2
		with self.assertRaises(SetupError) as context:
			self.installer().run()
		self.assertIn("did not finish", str(context.exception))

	def test_vm_stops_during_setup(self):
		original_ip = self.backend.ip

		def stop_then_ip():
			self.backend.state = "stopped"
			return original_ip()
		self.backend.ip = stop_then_ip
		with self.assertRaises(SetupError) as context:
			self.installer().run()
		self.assertIn("stopped", str(context.exception))

	def test_bundle_removed_when_payload_fails(self):
		def broken_payload(*arguments, **keywords):
			raise OSError("disk full")
		installer = self.installer()
		installer.build_payload = broken_payload
		with self.assertRaises(SetupError):
			installer.run()
		self.assertFalse(os.path.exists(os.path.join(self.bundle_dir, "Windows.utm")))


class CloneFileTests(unittest.TestCase):
	def setUp(self):
		self.directory = short_temporary_directory()
		self.addCleanup(self.directory.cleanup)
		self.source = os.path.join(self.directory.name, "source.iso")
		self.destination = os.path.join(self.directory.name, "copy.iso")
		with open(self.source, "wb") as source:
			source.write(b"windows")

	def test_clones_with_cp(self):
		commands = []

		def run(command, **kwargs):
			commands.append(command)
			return mock.Mock(returncode=0)
		clone_file(self.source, self.destination, run=run)
		self.assertEqual(commands, [["cp", "-c", self.source, self.destination]])
		self.assertFalse(os.path.exists(self.destination))

	def test_copies_when_cp_fails(self):
		clone_file(self.source, self.destination, run=lambda command, **kwargs: mock.Mock(returncode=1))
		with open(self.destination, "rb") as copy:
			self.assertEqual(copy.read(), b"windows")

	def test_real_clone(self):
		clone_file(self.source, self.destination)
		with open(self.destination, "rb") as copy:
			self.assertEqual(copy.read(), b"windows")


class RestartUTMTests(unittest.TestCase):
	def test_kills_by_pid_then_opens(self):
		commands = []
		alive = {"checks": 2}

		def run(command, **kwargs):
			commands.append(command)
			result = mock.Mock(stdout="", returncode=0)
			if command[0] == "pgrep":
				result.stdout = "4242\n"
			if command[:2] == ["kill", "-0"]:
				alive["checks"] -= 1
				result.returncode = 0 if alive["checks"] > 0 else 1
			return result
		clock = FakeClock()
		restart_utm(run=run, sleep=clock.sleep, clock=clock)
		self.assertEqual(commands[0], ["pgrep", "-x", "UTM"])
		self.assertEqual(commands[1], ["kill", "4242"])
		self.assertEqual(commands[-1], ["open", "-g", "/Applications/UTM.app"])

	def test_opens_when_not_running(self):
		commands = []

		def run(command, **kwargs):
			commands.append(command)
			return mock.Mock(stdout="", returncode=1 if command[0] == "pgrep" else 0)
		restart_utm(run=run)
		self.assertEqual(commands, [["pgrep", "-x", "UTM"], ["open", "-g", "/Applications/UTM.app"]])


if __name__ == "__main__":
	unittest.main()
