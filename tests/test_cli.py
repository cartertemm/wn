import io
import json
import os
import plistlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

from fake_keygrab_helper import read_log, write_fake_helper
from fake_qmp_server import TCPFakeQMPServer, reply
from fakes import FakeBackend, RecordingQMPClient
from wn.cli import main
from wn.keys import combo_to_input_events


class CLITestCase(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(self.home.cleanup)
		self.backend = FakeBackend()
		self.qmp_log = []
		sleep_patcher = mock.patch("wn.keys.time.sleep")
		sleep_patcher.start()
		self.addCleanup(sleep_patcher.stop)

	def write_config(self, values):
		config_directory = os.path.join(self.home.name, ".config", "wn")
		os.makedirs(config_directory, exist_ok=True)
		with open(os.path.join(config_directory, "config.json"), "w") as config_file:
			json.dump(values, config_file)

	def run_real_cli(self, *argv):
		stdout = io.StringIO()
		stderr = io.StringIO()
		exit_code = main(list(argv), stdout=stdout, stderr=stderr)
		return exit_code, stdout.getvalue(), stderr.getvalue()

	def run_cli(self, *argv):
		stdout = io.StringIO()
		stderr = io.StringIO()
		exit_code = main(
			list(argv),
			backend_factory=lambda config: self.backend,
			qmp_client_factory=RecordingQMPClient(self.qmp_log),
			stdout=stdout,
			stderr=stderr,
		)
		return exit_code, stdout.getvalue(), stderr.getvalue()


class LifecycleCommandTests(CLITestCase):
	def test_start(self):
		self.assertEqual(self.run_cli("start")[0], 0)
		self.assertEqual(self.backend.calls, [("start",)])

	def test_stop(self):
		self.run_cli("stop")
		self.assertEqual(self.backend.calls, [("stop", False), ("status",)])

	def test_stop_force(self):
		self.run_cli("stop", "--force")
		self.assertEqual(self.backend.calls, [("stop", True)])

	def test_status(self):
		exit_code, stdout, _ = self.run_cli("status")
		self.assertEqual(exit_code, 0)
		self.assertEqual(stdout, "Windows: running\n")

	def test_ip(self):
		self.assertEqual(self.run_cli("ip")[1], "192.168.64.5\n")

	def test_ip_unknown(self):
		self.backend.ip_address = None
		exit_code, _, stderr = self.run_cli("ip")
		self.assertEqual(exit_code, 1)
		self.assertIn("IP address", stderr)

	def test_share_not_implemented(self):
		exit_code, _, stderr = self.run_cli("share", "/Users/me/Projects")
		self.assertEqual(exit_code, 1)
		self.assertIn("share", stderr)

	def test_net_not_implemented(self):
		exit_code, _, stderr = self.run_cli("net")
		self.assertEqual(exit_code, 1)
		self.assertIn("net", stderr)

	def test_missing_subcommand_is_usage_error(self):
		with mock.patch("sys.stderr", io.StringIO()):
			with self.assertRaises(SystemExit) as context:
				self.run_cli()
		self.assertEqual(context.exception.code, 2)


FAKE_UTMCTL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_utmctl.py")


class StopCommandTests(CLITestCase):
	"""``wn stop`` against the fake utmctl, with a fake clock so the waits take no time."""

	def setUp(self):
		super().setUp()
		self.state_path = os.path.join(self.home.name, "utmctl.json")
		self.write_vm(status="started")
		patcher = mock.patch.dict(os.environ, {"FAKE_UTMCTL_STATE": self.state_path})
		patcher.start()
		self.addCleanup(patcher.stop)
		wrapper = os.path.join(self.home.name, "utmctl")
		with open(wrapper, "w") as wrapper_file:
			wrapper_file.write("#!/bin/sh\nexec '{}' '{}' \"$@\"\n".format(sys.executable, FAKE_UTMCTL))
		os.chmod(wrapper, stat.S_IRWXU)
		self.write_config({"utmctl_path": wrapper})
		self.now = [0.0]
		for target, replacement in (("wn.cli.time.monotonic", lambda: self.now[0]), ("wn.cli.time.sleep", self.advance)):
			patcher = mock.patch(target, side_effect=replacement)
			patcher.start()
			self.addCleanup(patcher.stop)

	def advance(self, seconds):
		self.now[0] += seconds

	def write_vm(self, **fields):
		vm = {"uuid": "9E1A6F38-2D0B-4C41-8F7E-3B5A1C2D0001", "name": "Windows", "status": "stopped", "ip_addresses": []}
		vm.update(fields)
		with open(self.state_path, "w") as state_file:
			json.dump({"vms": [vm]}, state_file)

	def vm_state(self):
		with open(self.state_path) as state_file:
			return json.load(state_file)["vms"][0]

	def test_waits_until_windows_has_stopped(self):
		self.write_vm(status="started", request_stop_lists=3)
		exit_code, stdout, stderr = self.run_real_cli("stop")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(stdout, "Windows: stopped\n")
		self.assertEqual(self.vm_state()["last_stop_flags"], ["--request"])
		self.assertEqual(self.now[0], 4.0)

	def test_fails_when_windows_keeps_running(self):
		self.write_vm(status="started", request_stop_lists=-1)
		exit_code, stdout, stderr = self.run_real_cli("stop")
		self.assertEqual(exit_code, 1)
		self.assertIn("Windows is still running 120 s after the shutdown request", stderr)
		self.assertIn("wn stop --force", stderr)
		self.assertEqual(self.vm_state()["status"], "started")
		self.assertGreaterEqual(self.now[0], 120)

	def test_timeout_option(self):
		self.write_vm(status="started", request_stop_lists=-1)
		exit_code, _, stderr = self.run_real_cli("stop", "--timeout", "10")
		self.assertEqual(exit_code, 1)
		self.assertIn("10 s", stderr)
		self.assertEqual(self.now[0], 10.0)

	def test_force_powers_off_without_waiting(self):
		self.write_vm(status="started", request_stop_lists=-1)
		exit_code, stdout, stderr = self.run_real_cli("stop", "--force")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(stdout, "")
		self.assertEqual(self.vm_state()["last_stop_flags"], ["--force"])
		self.assertEqual(self.now[0], 0.0)


def input_commands(*combos):
	return [{"execute": "input-send-event", "arguments": arguments} for combo in combos for arguments in combo_to_input_events(combo)]


def printed_commands(stdout):
	return [json.loads(line) for line in stdout.splitlines()]


class KeyCommandTests(CLITestCase):
	def test_dry_run_prints_commands_without_connecting(self):
		exit_code, stdout, _ = self.run_cli("key", "ctrl+alt+del", "--dry-run")
		self.assertEqual(exit_code, 0)
		self.assertEqual(printed_commands(stdout), input_commands("ctrl+alt+del"))
		self.assertEqual(self.qmp_log, [])
		self.assertEqual(self.backend.calls, [])

	def test_dry_run_with_several_combos(self):
		_, stdout, _ = self.run_cli("key", "win+r", "enter", "--hold-ms", "100", "--dry-run")
		self.assertEqual(printed_commands(stdout), input_commands("win+r", "enter"))

	def test_repeated_key_is_pressed_twice(self):
		_, stdout, _ = self.run_cli("key", "down", "down", "--dry-run")
		self.assertEqual(printed_commands(stdout), input_commands("down", "down"))

	def test_sends_through_backend_socket(self):
		self.assertEqual(self.run_cli("key", "win+r")[0], 0)
		press, release = combo_to_input_events("win+r")
		self.assertEqual(self.qmp_log, [
			("connect", "/fake/qmp.sock"),
			("input-send-event", press),
			("input-send-event", release),
			("close",),
		])

	def test_hold_and_delay_set_the_pauses(self):
		with mock.patch("wn.keys.time.sleep") as sleep:
			self.run_cli("key", "a", "--hold-ms", "70", "--delay-ms", "30")
		self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.07, 0.03])

	def test_config_socket_overrides_backend(self):
		self.write_config({"qmp_socket": "/configured/qmp.sock"})
		self.run_cli("key", "a")
		self.assertEqual(self.qmp_log[0], ("connect", "/configured/qmp.sock"))
		self.assertEqual(self.backend.calls, [])

	def test_unknown_key_is_reported(self):
		exit_code, _, stderr = self.run_cli("key", "ctrl+entr")
		self.assertEqual(exit_code, 1)
		self.assertIn("enter", stderr)
		self.assertEqual(self.qmp_log, [])


class TypeCommandTests(CLITestCase):
	def test_dry_run(self):
		_, stdout, _ = self.run_cli("type", "Hi", "--dry-run")
		self.assertEqual(printed_commands(stdout), input_commands("shift+h", "i"))

	def test_repeated_letters_are_pressed_twice(self):
		_, stdout, _ = self.run_cli("type", "hello", "--dry-run")
		self.assertEqual(printed_commands(stdout), input_commands("h", "e", "l", "l", "o"))

	def test_sends_text(self):
		self.run_cli("type", "ok")
		self.assertEqual([entry[0] for entry in self.qmp_log], ["connect"] + ["input-send-event"] * 4 + ["close"])

	def test_unsupported_character(self):
		exit_code, _, stderr = self.run_cli("type", "café", "--dry-run")
		self.assertEqual(exit_code, 1)
		self.assertIn("U+00E9", stderr)


def abs_move(x, y):
	return {"events": [{"type": "abs", "data": {"axis": "x", "value": x}}, {"type": "abs", "data": {"axis": "y", "value": y}}]}


def button(name, down):
	return {"events": [{"type": "btn", "data": {"down": down, "button": name}}]}


class MouseCommandTests(CLITestCase):
	def mouse_dry_run(self, *argv):
		exit_code, stdout, stderr = self.run_cli("mouse", *argv, "--dry-run")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.qmp_log, [])
		return [command["arguments"] for command in printed_commands(stdout)]

	def test_move_absolute(self):
		self.assertEqual(self.mouse_dry_run("move", "16384", "100"), [abs_move(16384, 100)])

	def test_move_with_screen_size(self):
		self.assertEqual(self.mouse_dry_run("move", "640", "360", "--screen", "1280x720"), [abs_move(16383, 16383)])

	def test_move_uses_config_screen_size(self):
		self.write_config({"screen_size": "1920x1080"})
		self.assertEqual(self.mouse_dry_run("move", "960", "540"), [abs_move(16383, 16383)])

	def test_move_out_of_range(self):
		exit_code, _, stderr = self.run_cli("mouse", "move", "40000", "0", "--dry-run")
		self.assertEqual(exit_code, 1)
		self.assertIn("32767", stderr)

	def test_move_outside_screen(self):
		exit_code, _, stderr = self.run_cli("mouse", "move", "1280", "0", "--screen", "1280x720", "--dry-run")
		self.assertEqual(exit_code, 1)
		self.assertIn("1280x720", stderr)

	def test_bad_screen_size(self):
		exit_code, _, stderr = self.run_cli("mouse", "move", "1", "1", "--screen", "big", "--dry-run")
		self.assertEqual(exit_code, 1)
		self.assertIn("WIDTHxHEIGHT", stderr)

	def test_click_defaults_to_left(self):
		self.assertEqual(self.mouse_dry_run("click"), [button("left", True), button("left", False)])

	def test_click_right(self):
		self.assertEqual(self.mouse_dry_run("click", "right"), [button("right", True), button("right", False)])

	def test_doubleclick(self):
		self.assertEqual(self.mouse_dry_run("doubleclick"), [button("left", True), button("left", False)] * 2)

	def test_down_and_up(self):
		self.assertEqual(self.mouse_dry_run("down", "middle"), [button("middle", True)])
		self.assertEqual(self.mouse_dry_run("up"), [button("left", False)])

	def test_scroll(self):
		self.assertEqual(self.mouse_dry_run("scroll", "down", "2"), [button("wheel-down", True), button("wheel-down", False)] * 2)
		self.assertEqual(self.mouse_dry_run("scroll", "up"), [button("wheel-up", True), button("wheel-up", False)])

	def test_bad_button_is_usage_error(self):
		with mock.patch("sys.stderr", io.StringIO()):
			with self.assertRaises(SystemExit):
				self.run_cli("mouse", "click", "side")

	def test_sends_through_one_shot_socket(self):
		self.assertEqual(self.run_cli("mouse", "click")[0], 0)
		self.assertEqual(self.qmp_log, [
			("connect", "/fake/qmp.sock"),
			("input-send-event", button("left", True)),
			("input-send-event", button("left", False)),
			("close",),
		])


class UTMWiringTests(CLITestCase):
	def test_key_reaches_utm_qmp_server_over_tcp(self):
		handlers = {"input-send-event": lambda request: [reply(request, {})]}
		with TCPFakeQMPServer(handlers) as server:
			self.write_config({"backend": "utm", "qmp_port": server.port})
			exit_code, _, stderr = self.run_real_cli("key", "win+r")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual([request["execute"] for request in server.received], ["qmp_capabilities", "input-send-event", "input-send-event"])
		self.assertEqual([request["arguments"] for request in server.received[1:]], list(combo_to_input_events("win+r")))

	def test_status_reports_missing_utmctl(self):
		self.write_config({"utmctl_path": "/no/such/utmctl"})
		exit_code, _, stderr = self.run_real_cli("status")
		self.assertEqual(exit_code, 1)
		self.assertIn("/no/such/utmctl", stderr)


FAKE_QEMU_IMG = """#!{python}
import sys
with open(sys.argv[-2], "w") as image:
	image.write(" ".join(sys.argv[1:]))
"""
FAILING_QEMU_IMG = "#!/bin/sh\necho 'qemu-img: broken' >&2\nexit 1\n"
MAC_PATTERN = r"52:54:00(:[0-9a-f]{2}){3}"


class InitVMTests(CLITestCase):
	def setUp(self):
		super().setUp()
		self.output = os.path.join(self.home.name, "Documents")
		self.qemu_img = self.write_script("qemu-img", FAKE_QEMU_IMG.format(python=sys.executable))
		self.iso = os.path.join(self.home.name, "win11-arm64.iso")
		with open(self.iso, "wb") as iso_file:
			iso_file.write(b"fake iso contents")

	def write_script(self, name, content):
		path = os.path.join(self.home.name, name)
		with open(path, "w") as script:
			script.write(content)
		os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
		return path

	def init_vm(self, *options):
		return self.run_real_cli("init-vm", "--output", self.output, "--qemu-img", self.qemu_img, *options)

	def read_plist(self, bundle):
		with open(os.path.join(bundle, "config.plist"), "rb") as plist_file:
			return plistlib.load(plist_file)

	def test_creates_bundle_with_defaults(self):
		exit_code, stdout, stderr = self.init_vm()
		self.assertEqual(exit_code, 0, stderr)
		bundle = os.path.join(self.output, "Windows.utm")
		self.assertIn(bundle, stdout)
		self.assertIn("open ", stdout)
		config = self.read_plist(bundle)
		self.assertEqual(config["Information"]["Name"], "Windows")
		self.assertEqual(config["System"]["CPUCount"], 8)
		self.assertEqual(config["System"]["MemorySize"], 8192)
		self.assertRegex(config["Network"][0]["MacAddress"], "^" + MAC_PATTERN + "$")
		self.assertEqual(config["Sound"], [])
		self.assertIn("coreaudio,id=ca0,out.buffer-length=20000", config["QEMU"]["AdditionalArguments"])
		self.assertIn("tcp:127.0.0.1:4444,server=on,wait=off", config["QEMU"]["AdditionalArguments"])
		self.assertIn("tcp:127.0.0.1:4445,server=on,wait=off", config["QEMU"]["AdditionalArguments"])
		self.assertEqual([drive["ImageType"] for drive in config["Drive"]], ["Disk"])
		self.assertEqual(config["QEMU"]["MachinePropertyOverride"], "kernel-irqchip=off")
		with open(os.path.join(bundle, "Data", "disk.qcow2")) as image:
			self.assertEqual(image.read(), "create -f qcow2 {} 64G".format(os.path.join(bundle, "Data", "disk.qcow2")))

	def test_audio_args(self):
		exit_code, _, stderr = self.init_vm("--audio-args", "-audiodev coreaudio,id=ca0 -device usb-audio,audiodev=ca0")
		self.assertEqual(exit_code, 0, stderr)
		arguments = self.read_plist(os.path.join(self.output, "Windows.utm"))["QEMU"]["AdditionalArguments"]
		self.assertEqual(arguments[4:], ["-audiodev", "coreaudio,id=ca0", "-device", "usb-audio,audiodev=ca0"])

	def test_extra_args(self):
		exit_code, _, stderr = self.init_vm("--extra-args", "-device virtio-keyboard-pci,id=vkbd")
		self.assertEqual(exit_code, 0, stderr)
		arguments = self.read_plist(os.path.join(self.output, "Windows.utm"))["QEMU"]["AdditionalArguments"]
		self.assertEqual(arguments[-2:], ["-device", "virtio-keyboard-pci,id=vkbd"])

	def test_options(self):
		exit_code, _, stderr = self.init_vm(
			"--name", "Win 11", "--disk-gib", "128", "--cpus", "4", "--memory-mib", "4096",
			"--iso", self.iso, "--headless", "--audio", "spice", "--mac", "52:54:00:12:34:56",
		)
		self.assertEqual(exit_code, 0, stderr)
		bundle = os.path.join(self.output, "Win 11.utm")
		config = self.read_plist(bundle)
		self.assertEqual(config["System"]["CPUCount"], 4)
		self.assertEqual(config["System"]["MemorySize"], 4096)
		self.assertEqual(config["Network"][0]["MacAddress"], "52:54:00:12:34:56")
		self.assertEqual(config["Display"], [])
		self.assertEqual(config["Sound"], [{"Hardware": "intel-hda"}])
		self.assertEqual([(drive["ImageType"], drive["ImageName"]) for drive in config["Drive"]], [("CD", "win11-arm64.iso"), ("Disk", "disk.qcow2")])
		with open(os.path.join(bundle, "Data", "win11-arm64.iso"), "rb") as iso_copy:
			self.assertEqual(iso_copy.read(), b"fake iso contents")
		with open(os.path.join(bundle, "Data", "disk.qcow2")) as image:
			self.assertTrue(image.read().endswith(" 128G"))

	def test_machine_properties(self):
		self.assertEqual(self.init_vm("--machine-properties", "highmem=on")[0], 0)
		self.assertEqual(self.read_plist(os.path.join(self.output, "Windows.utm"))["QEMU"]["MachinePropertyOverride"], "highmem=on")

	def test_empty_machine_properties_omits_key(self):
		self.assertEqual(self.init_vm("--machine-properties", "")[0], 0)
		self.assertNotIn("MachinePropertyOverride", self.read_plist(os.path.join(self.output, "Windows.utm"))["QEMU"])

	def test_driver_iso_is_copied_as_second_cd(self):
		driver_iso = os.path.join(self.home.name, "virtio-win.iso")
		with open(driver_iso, "wb") as iso_file:
			iso_file.write(b"fake drivers")
		self.assertEqual(self.init_vm("--iso", self.iso, "--driver-iso", driver_iso)[0], 0)
		bundle = os.path.join(self.output, "Windows.utm")
		config = self.read_plist(bundle)
		self.assertEqual([(drive["ImageType"], drive["ImageName"]) for drive in config["Drive"]], [("CD", "win11-arm64.iso"), ("CD", "virtio-win.iso"), ("Disk", "disk.qcow2")])
		with open(os.path.join(bundle, "Data", "virtio-win.iso"), "rb") as iso_copy:
			self.assertEqual(iso_copy.read(), b"fake drivers")

	def test_name_defaults_to_config_vm_name(self):
		self.write_config({"vm_name": "Configured", "qmp_port": 4500, "qmp_session_port": 4600})
		self.assertEqual(self.init_vm()[0], 0)
		config = self.read_plist(os.path.join(self.output, "Configured.utm"))
		self.assertIn("tcp:127.0.0.1:4500,server=on,wait=off", config["QEMU"]["AdditionalArguments"])
		self.assertIn("tcp:127.0.0.1:4600,server=on,wait=off", config["QEMU"]["AdditionalArguments"])

	def test_iso_is_copied_with_copyfile(self):
		with mock.patch("wn.cli.shutil.copyfile") as copyfile:
			self.assertEqual(self.init_vm("--iso", self.iso)[0], 0)
		copyfile.assert_called_once_with(self.iso, os.path.join(self.output, "Windows.utm", "Data", "win11-arm64.iso"))

	def test_disk_image_is_created_before_plist(self):
		order = []
		with mock.patch("wn.cli.create_disk_image", side_effect=lambda *arguments: order.append("disk")), \
			mock.patch("wn.cli.write_bundle", side_effect=lambda path, options: order.append("plist") or os.path.join(path, "Windows.utm")):
			self.assertEqual(self.init_vm()[0], 0)
		self.assertEqual(order, ["disk", "plist"])

	def test_refuses_existing_bundle(self):
		os.makedirs(os.path.join(self.output, "Windows.utm"))
		exit_code, _, stderr = self.init_vm()
		self.assertEqual(exit_code, 1)
		self.assertIn("already exists", stderr)
		self.assertEqual(os.listdir(os.path.join(self.output, "Windows.utm")), [])

	def test_failed_disk_image_removes_bundle(self):
		self.qemu_img = self.write_script("broken-qemu-img", FAILING_QEMU_IMG)
		exit_code, _, stderr = self.init_vm()
		self.assertEqual(exit_code, 1)
		self.assertIn("qemu-img: broken", stderr)
		self.assertFalse(os.path.exists(os.path.join(self.output, "Windows.utm")))

	def test_missing_iso_removes_bundle(self):
		exit_code, _, stderr = self.init_vm("--iso", os.path.join(self.home.name, "missing.iso"))
		self.assertEqual(exit_code, 1)
		self.assertIn("missing.iso", stderr)
		self.assertFalse(os.path.exists(os.path.join(self.output, "Windows.utm")))

	def test_generated_macs_differ(self):
		self.init_vm("--name", "One")
		self.init_vm("--name", "Two")
		macs = [self.read_plist(os.path.join(self.output, name + ".utm"))["Network"][0]["MacAddress"] for name in ("One", "Two")]
		self.assertNotEqual(macs[0], macs[1])

	def test_bad_audio_is_usage_error(self):
		with mock.patch("sys.stderr", io.StringIO()):
			with self.assertRaises(SystemExit):
				self.init_vm("--audio", "pulse")

	def test_default_output_is_utm_documents(self):
		with mock.patch("wn.cli.write_bundle", side_effect=lambda path, options: os.path.join(path, "Windows.utm")), \
			mock.patch("wn.cli.create_disk_image"):
			exit_code, stdout, _ = self.run_real_cli("init-vm")
		self.assertEqual(exit_code, 0)
		self.assertIn(os.path.join(self.home.name, "Library", "Containers", "com.utmapp.UTM", "Data", "Documents", "Windows.utm"), stdout)


GRAB_LINES = [
	{"type": "grab", "active": True},
	{"type": "down", "keycode": 55, "flags": 0, "ts": 1.0},
	{"type": "up", "keycode": 55, "flags": 0, "ts": 1.0},
]
META_DOWN = {"events": [{"type": "key", "data": {"down": True, "key": {"type": "qcode", "data": "meta_l"}}}]}
META_UP = {"events": [{"type": "key", "data": {"down": False, "key": {"type": "qcode", "data": "meta_l"}}}]}


class GrabCommandTests(CLITestCase):
	def setUp(self):
		super().setUp()
		self.helper_path, self.helper_log = write_fake_helper(self.home.name, GRAB_LINES)
		self.write_config({"keygrab_binary": self.helper_path})

	def test_grab_sends_input_events(self):
		exit_code, stdout, stderr = self.run_cli("grab")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.qmp_log, [
			("connect", "/fake/qmp-session.sock"),
			("input-send-event", META_DOWN),
			("input-send-event", META_UP),
			("close",),
		])
		self.assertIn("grab on", stdout)

	def test_dry_run_prints_commands_without_connecting(self):
		exit_code, stdout, _ = self.run_cli("grab", "--dry-run")
		self.assertEqual(exit_code, 0)
		commands = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
		self.assertEqual(commands, [
			{"execute": "input-send-event", "arguments": META_DOWN},
			{"execute": "input-send-event", "arguments": META_UP},
		])
		self.assertEqual(self.qmp_log, [])
		self.assertEqual(self.backend.calls, [])

	def test_options_reach_helper(self):
		self.run_cli("grab", "--start-released", "--release-hotkey", "control,shift", "--dry-run")
		self.assertEqual(read_log(self.helper_log)[0], ["--release-hotkey", "control,shift", "--start-released"])

	def test_default_release_hotkey(self):
		self.run_cli("grab", "--dry-run")
		self.assertEqual(read_log(self.helper_log)[0], ["--release-hotkey", "control,option,command"])

	def test_missing_helper_is_reported(self):
		self.write_config({"keygrab_binary": "/no/such/wn-keygrab"})
		exit_code, _, stderr = self.run_cli("grab", "--dry-run")
		self.assertEqual(exit_code, 1)
		self.assertIn("build.sh", stderr)

	def test_keycodes_override(self):
		keycodes_path = os.path.join(self.home.name, "keycodes.json")
		with open(keycodes_path, "w") as keycodes_file:
			json.dump({"keycodes": {"55": "meta_r"}}, keycodes_file)
		self.write_config({"keygrab_binary": self.helper_path, "keygrab_keycodes": keycodes_path})
		_, stdout, _ = self.run_cli("grab", "--dry-run")
		self.assertIn('"meta_r"', stdout)


OLD_ARGUMENTS = ["-qmp", "tcp:127.0.0.1:4444,server=on,wait=off", "-audiodev", "coreaudio,id=ca0,out.buffer-length=20000", "-device", "intel-hda", "-device", "hda-output,audiodev=ca0"]


TUNED_ARGUMENTS = OLD_ARGUMENTS[:2] + ["-qmp", "tcp:127.0.0.1:4445,server=on,wait=off"] + [
	"-audiodev", "coreaudio,id=ca0,timer-period=2500,out.buffer-length=5000,out.buffer-count=3,out.frequency=48000,out.fixed-settings=on",
	"-device", "intel-hda", "-device", "hda-output,audiodev=ca0",
]


class UpdateVMArgsTests(CLITestCase):
	def setUp(self):
		super().setUp()
		self.bundle = os.path.join(self.home.name, "Windows.utm")
		os.makedirs(self.bundle)
		self.plist_path = os.path.join(self.bundle, "config.plist")
		with open(self.plist_path, "wb") as plist_file:
			plistlib.dump({"Information": {"Name": "Windows"}, "QEMU": {"AdditionalArguments": OLD_ARGUMENTS, "UEFIBoot": True}}, plist_file)
		self.backend.state = "stopped"

	def read_plist(self):
		with open(self.plist_path, "rb") as plist_file:
			return plistlib.load(plist_file)

	def test_adds_session_qmp_server(self):
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--qmp")
		self.assertEqual(exit_code, 0, stderr)
		config = self.read_plist()
		self.assertEqual(config["QEMU"]["AdditionalArguments"], OLD_ARGUMENTS[:2] + ["-qmp", "tcp:127.0.0.1:4445,server=on,wait=off"] + OLD_ARGUMENTS[2:])
		self.assertTrue(config["QEMU"]["UEFIBoot"])
		self.assertIn("4445", stdout)

	def test_audio_args_replace_audio_block(self):
		audio_args = "-audiodev coreaudio,id=ca0,timer-period=2500 -device intel-hda -device hda-output,audiodev=ca0"
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--audio-args", audio_args)
		self.assertEqual(exit_code, 0, stderr)
		arguments = self.read_plist()["QEMU"]["AdditionalArguments"]
		self.assertEqual(arguments, OLD_ARGUMENTS[:2] + audio_args.split())
		self.assertIn("+-audiodev coreaudio,id=ca0,timer-period=2500", stdout)
		self.assertIn("--audiodev coreaudio,id=ca0,out.buffer-length=20000", stdout)

	def test_extra_args_follow_audio_args(self):
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--audio-args", "-device intel-hda", "--extra-args", "-device virtio-keyboard-pci,id=vkbd")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.read_plist()["QEMU"]["AdditionalArguments"][2:], ["-device", "intel-hda", "-device", "virtio-keyboard-pci,id=vkbd"])
		self.assertIn("vkbd", stdout)

	def test_audio_args_with_spice_is_error(self):
		exit_code, _, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--audio", "spice", "--audio-args", "-device intel-hda")
		self.assertEqual(exit_code, 1)
		self.assertIn("audio_args", stderr)
		self.assertEqual(self.read_plist()["QEMU"]["AdditionalArguments"], OLD_ARGUMENTS)

	def test_default_bundle_path_uses_vm_name(self):
		with mock.patch("wn.cli.update_additional_arguments", return_value=([], [])) as update:
			self.assertEqual(self.run_cli("update-vm-args")[0], 0)
		expected = os.path.join(self.home.name, "Library", "Containers", "com.utmapp.UTM", "Data", "Documents", "Windows.utm")
		self.assertEqual(update.call_args.args[0], expected)

	def add_memory_size(self, memory_mib):
		config = self.read_plist()
		config["System"] = {"MemorySize": memory_mib}
		with open(self.plist_path, "wb") as plist_file:
			plistlib.dump(config, plist_file)
		os.makedirs(os.path.join(self.bundle, "Data"))
		os.makedirs(self.group_container)

	@property
	def group_container(self):
		return os.path.join(self.home.name, "Library", "Group Containers", "WDNLXAD4W8.com.utmapp.UTM")

	def test_battery_adds_the_nvdimm_and_prepares_the_file(self):
		self.add_memory_size(8192)
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--battery")
		self.assertEqual(exit_code, 0, stderr)
		path = os.path.join(self.group_container, "wn-battery-Windows.bin")
		arguments = self.read_plist()["QEMU"]["AdditionalArguments"]
		self.assertIn("memory-backend-file,id=wnbattery,share=on,mem-path=wn-battery-Windows.bin,size=1M", arguments)
		self.assertIn("maxmem=8193M,slots=1", arguments)
		with open(path, "rb") as data_file:
			self.assertEqual(data_file.read(4), b"WNBT")
		self.assertIn(path, stdout)

	def test_battery_keeps_an_existing_file(self):
		self.add_memory_size(8192)
		path = os.path.join(self.group_container, "wn-battery-Windows.bin")
		with open(path, "wb") as data_file:
			data_file.write(b"keep")
		self.assertEqual(self.run_cli("update-vm-args", "--bundle", self.bundle, "--battery")[0], 0)
		with open(path, "rb") as data_file:
			self.assertEqual(data_file.read(4), b"keep")

	def test_battery_uses_the_configured_file(self):
		self.add_memory_size(4096)
		path = os.path.join(self.home.name, "battery.bin")
		os.makedirs(os.path.join(self.home.name, ".config", "wn"), exist_ok=True)
		with open(os.path.join(self.home.name, ".config", "wn", "config.json"), "w") as config_file:
			json.dump({"battery_file": path}, config_file)
		exit_code, _, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--battery")
		self.assertEqual(exit_code, 0, stderr)
		self.assertIn("memory-backend-file,id=wnbattery,share=on,mem-path={},size=1M".format(path), self.read_plist()["QEMU"]["AdditionalArguments"])
		self.assertTrue(os.path.exists(path))

	def write_arguments(self, arguments):
		config = self.read_plist()
		config["QEMU"]["AdditionalArguments"] = arguments
		with open(self.plist_path, "wb") as plist_file:
			plistlib.dump(config, plist_file)

	def test_no_flags_keeps_custom_audio(self):
		self.write_arguments(TUNED_ARGUMENTS)
		with open(self.plist_path, "rb") as plist_file:
			original = plist_file.read()
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle)
		self.assertEqual(exit_code, 0, stderr)
		with open(self.plist_path, "rb") as plist_file:
			self.assertEqual(plist_file.read(), original)
		self.assertIn("did not change", stdout)

	def test_battery_keeps_custom_audio_and_prints_a_diff(self):
		self.add_memory_size(8192)
		self.write_arguments(TUNED_ARGUMENTS)
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--battery")
		self.assertEqual(exit_code, 0, stderr)
		arguments = self.read_plist()["QEMU"]["AdditionalArguments"]
		self.assertEqual(arguments[:len(TUNED_ARGUMENTS)], TUNED_ARGUMENTS)
		self.assertIn("+-machine nvdimm=on", stdout)
		self.assertNotIn("\n-", stdout.split("+++", 1)[1])

	def test_no_battery_removes_the_nvdimm(self):
		self.add_memory_size(8192)
		self.write_arguments(TUNED_ARGUMENTS)
		self.assertEqual(self.run_cli("update-vm-args", "--bundle", self.bundle, "--battery")[0], 0)
		exit_code, stdout, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle, "--no-battery")
		self.assertEqual(exit_code, 0, stderr)
		self.assertEqual(self.read_plist()["QEMU"]["AdditionalArguments"], TUNED_ARGUMENTS)
		self.assertIn("--machine nvdimm=on", stdout)

	def test_refuses_running_vm(self):
		self.backend.state = "running"
		exit_code, _, stderr = self.run_cli("update-vm-args", "--bundle", self.bundle)
		self.assertEqual(exit_code, 1)
		self.assertIn("stop", stderr)
		self.assertEqual(self.read_plist()["QEMU"]["AdditionalArguments"], OLD_ARGUMENTS)


if __name__ == "__main__":
	unittest.main()


class SetupCommandTests(CLITestCase):
	def run_setup(self, *argv, answers=""):
		stdout = io.StringIO()
		stderr = io.StringIO()
		with mock.patch("wn.cli.Installer") as installer_class, mock.patch("wn.cli.getpass.getuser", return_value="macuser"):
			exit_code = main(["setup"] + list(argv), stdout=stdout, stderr=stderr, stdin=io.StringIO(answers))
		options = installer_class.call_args[0][0] if installer_class.called else None
		return exit_code, options, stdout.getvalue(), stderr.getvalue()

	def test_yes_uses_defaults(self):
		exit_code, options, _, _ = self.run_setup("--yes")
		self.assertEqual(exit_code, 0)
		self.assertEqual((options.name, options.username, options.password), ("Windows", "macuser", ""))
		self.assertEqual((options.cpus, options.memory_mib, options.disk_gib, options.headless), (8, 8192, 64, False))
		self.assertEqual((options.software, options.edition), (("nvda",), "Pro"))
		self.assertEqual(options.downloads_dir, os.path.join("~", "Library", "Application Support", "wn", "downloads"))

	def test_yes_with_flags(self):
		_, options, _, _ = self.run_setup("--yes", "--name", "Win", "--username", "carter", "--password", "pw", "--cpus", "4",
			"--memory-mib", "4096", "--disk-gib", "80", "--headless", "--software", "none", "--edition", "Home", "--downloads-dir", "/tmp/dl")
		self.assertEqual((options.name, options.username, options.password), ("Win", "carter", "pw"))
		self.assertEqual((options.cpus, options.memory_mib, options.disk_gib, options.headless), (4, 4096, 80, True))
		self.assertEqual((options.software, options.edition, options.downloads_dir), ((), "Home", "/tmp/dl"))

	def test_prompts_accept_defaults(self):
		exit_code, options, stdout, _ = self.run_setup(answers="\n" * 9)
		self.assertEqual(exit_code, 0)
		self.assertEqual((options.name, options.username, options.software), ("Windows", "macuser", ("nvda",)))
		self.assertIn("VM name [Windows]: ", stdout)
		self.assertIn("1. NVDA screen reader", stdout)
		self.assertIn("Numbers separated by commas, 0 for none [1]: ", stdout)

	def test_prompts_take_answers(self):
		answers = "\n".join(["Work", "carter", "secret", "6", "6144", "100", "yes", "0", "Education"]) + "\n"
		_, options, _, _ = self.run_setup(answers=answers)
		self.assertEqual((options.name, options.username, options.password), ("Work", "carter", "secret"))
		self.assertEqual((options.cpus, options.memory_mib, options.disk_gib, options.headless), (6, 6144, 100, True))
		self.assertEqual((options.software, options.edition), ((), "Education"))

	def test_prompt_repeats_bad_number(self):
		answers = "\n".join(["", "", "", "many", "4", "", "", "", "", ""]) + "\n"
		_, options, stdout, _ = self.run_setup(answers=answers)
		self.assertEqual(options.cpus, 4)
		self.assertIn("Enter a whole number", stdout)

	def test_flags_become_prompt_defaults(self):
		_, options, stdout, _ = self.run_setup("--name", "Lab", answers="\n" * 9)
		self.assertEqual(options.name, "Lab")
		self.assertIn("VM name [Lab]: ", stdout)

	def test_unknown_software(self):
		exit_code, _, _, stderr = self.run_setup("--yes", "--software", "jaws")
		self.assertEqual(exit_code, 1)
		self.assertIn("jaws", stderr)

	def test_unknown_edition(self):
		exit_code, _, _, stderr = self.run_setup("--yes", "--edition", "Ultimate")
		self.assertEqual(exit_code, 1)
		self.assertIn("Ultimate", stderr)

	def test_battery_is_on_by_default(self):
		_, options, _, _ = self.run_setup("--yes")
		self.assertTrue(options.battery)
		self.assertIsNone(options.battery_file)

	def test_no_battery(self):
		_, options, _, _ = self.run_setup("--yes", "--no-battery")
		self.assertFalse(options.battery)

	def test_battery_file_comes_from_the_config(self):
		self.write_config({"battery_file": "/tmp/wn-battery.bin"})
		_, options, _, _ = self.run_setup("--yes")
		self.assertEqual(options.battery_file, "/tmp/wn-battery.bin")

	def test_end_of_input_uses_defaults(self):
		exit_code, options, _, _ = self.run_setup(answers="")
		self.assertEqual(exit_code, 0)
		self.assertEqual(options.name, "Windows")
