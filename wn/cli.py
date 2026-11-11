"""Command line interface for wn."""

import argparse
import json
import os
import random
import shlex
import shutil
import sys

from wn import __version__, keygrab, mouse
from wn.attach import attach
from wn.backend import create_backend
from wn.config import load_config
from wn.errors import WnError
from wn.keys import DEFAULT_HOLD_MS, INPUT_SEND_EVENT, combos_to_input_steps, send_input_steps
from wn.qmp import qmp_client_for
from wn.text_input import text_to_combos
from wn.utm_config import (
	AUDIO_COREAUDIO,
	AUDIO_MODES,
	BUNDLE_SUFFIX,
	DATA_DIRECTORY_NAME,
	DEFAULT_OPTIONS,
	DEFAULT_QEMU_IMG_PATH,
	create_disk_image,
	update_additional_arguments,
	write_bundle,
)

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
DEFAULT_BUNDLE_DIRECTORY = os.path.join("~", "Library", "Containers", "com.utmapp.UTM", "Data", "Documents")
DEFAULT_DISK_GIB = 64
DEFAULT_CPU_COUNT = 8
DEFAULT_MEMORY_MIB = 8192
QEMU_MAC_PREFIX = "52:54:00"
MAC_SUFFIX_OCTET_COUNT = 3
OCTET_VALUES = 256
DEFAULT_SCROLL_COUNT = 1
STOPPED_STATE = "stopped"


def build_parser():
	parser = argparse.ArgumentParser(prog="wn", description="Control a Windows VM from the terminal.")
	parser.add_argument("--version", action="version", version="wn " + __version__)
	subcommands = parser.add_subparsers(dest="command", metavar="command")
	subcommands.required = True

	subcommands.add_parser("start", help="start the VM")
	stop = subcommands.add_parser("stop", help="shut down the VM")
	stop.add_argument("--force", action="store_true", help="power off at once instead of a clean shutdown")
	subcommands.add_parser("status", help="show whether the VM is running")
	subcommands.add_parser("ip", help="print the guest IP address")

	key = subcommands.add_parser("key", help="press key combos, such as ctrl+alt+del or win+r")
	key.add_argument("combos", nargs="+", metavar="combo")
	_add_key_options(key)

	type_text = subcommands.add_parser("type", help="type text with a US keyboard layout")
	type_text.add_argument("text")
	_add_key_options(type_text)

	_add_mouse_parser(subcommands)

	attach_parser = subcommands.add_parser("attach", help="send terminal keystrokes to the guest. Press ctrl+] twice to detach")
	attach_parser.add_argument("--grab", action="store_true", help="use grab mode instead, the same as wn grab")
	attach_parser.set_defaults(start_released=False, release_hotkey=keygrab.DEFAULT_RELEASE_HOTKEY, dry_run=False)

	grab = subcommands.add_parser("grab", help="send every key press on the Mac keyboard to the guest, including the Windows key")
	grab.add_argument("--start-released", action="store_true", help="start with the keyboard released, not grabbed")
	grab.add_argument("--release-hotkey", default=keygrab.DEFAULT_RELEASE_HOTKEY, metavar="MODIFIERS", help="comma separated modifiers that release and grab the keyboard (default: %(default)s)")
	grab.add_argument("--dry-run", action="store_true", help="print the QMP commands as JSON instead of sending them")
	share = subcommands.add_parser("share", help="share a host folder with the guest")
	share.add_argument("host_path")
	subcommands.add_parser("net", help="show or change VM networking")
	_add_init_vm_parser(subcommands)
	_add_update_vm_args_parser(subcommands)
	return parser


def _add_mouse_parser(subcommands):
	mouse_parser = subcommands.add_parser("mouse", help="move the pointer, click, or scroll")
	actions = mouse_parser.add_subparsers(dest="mouse_action", metavar="action")
	actions.required = True
	dry_run = argparse.ArgumentParser(add_help=False)
	dry_run.add_argument("--dry-run", action="store_true", help="print the QMP commands as JSON instead of sending them")

	move = actions.add_parser("move", parents=[dry_run], help="move the pointer to an absolute position")
	move.add_argument("x", type=int)
	move.add_argument("y", type=int)
	move.add_argument("--screen", metavar="WxH", help="treat X and Y as pixels on a screen of this size (default: screen_size from the config, else X and Y are 0..32767)")
	for action, help_text in (
		("click", "press and release a button"),
		("doubleclick", "click a button twice"),
		("down", "press a button and keep it down"),
		("up", "release a button"),
	):
		button_parser = actions.add_parser(action, parents=[dry_run], help=help_text)
		button_parser.add_argument("button", nargs="?", choices=mouse.BUTTONS, default=mouse.DEFAULT_BUTTON)
	scroll = actions.add_parser("scroll", parents=[dry_run], help="turn the scroll wheel")
	scroll.add_argument("direction", choices=tuple(mouse.WHEEL_BUTTONS))
	scroll.add_argument("count", type=int, nargs="?", default=DEFAULT_SCROLL_COUNT, help="number of wheel notches (default: %(default)s)")


def _add_update_vm_args_parser(subcommands):
	update = subcommands.add_parser("update-vm-args", help="rewrite the QEMU additional arguments of an existing UTM bundle. Stop the VM first")
	update.add_argument("--bundle", metavar="PATH", help="the .utm bundle (default: vm_name.utm in UTM's documents folder)")
	update.add_argument("--headless", action="store_true", help="the VM has no display window")
	update.add_argument("--audio", choices=AUDIO_MODES, default=AUDIO_COREAUDIO, help="audio output the bundle was created with (default: %(default)s)")
	_add_audio_args_option(update)


def _add_audio_args_option(parser):
	parser.add_argument("--audio-args", metavar="ARGS", help="QEMU arguments that replace the default coreaudio block, such as \"-audiodev coreaudio,id=ca0,... -device intel-hda -device hda-output,audiodev=ca0\". Needs --audio coreaudio")


def _add_init_vm_parser(subcommands):
	init_vm = subcommands.add_parser("init-vm", help="create a UTM bundle for a new Windows 11 ARM64 VM")
	init_vm.add_argument("--name", help="VM name (default: vm_name from the config)")
	init_vm.add_argument("--disk-gib", type=int, default=DEFAULT_DISK_GIB, help="disk size in GiB (default: %(default)s)")
	init_vm.add_argument("--cpus", type=int, default=DEFAULT_CPU_COUNT, help="number of CPU cores (default: %(default)s)")
	init_vm.add_argument("--memory-mib", type=int, default=DEFAULT_MEMORY_MIB, help="memory in MiB (default: %(default)s)")
	init_vm.add_argument("--iso", metavar="PATH", help="Windows installer ISO to copy into the bundle")
	init_vm.add_argument("--driver-iso", metavar="PATH", help="driver ISO, such as virtio-win, to copy into the bundle as a second CD")
	init_vm.add_argument("--machine-properties", default=DEFAULT_OPTIONS["machine_properties"], metavar="PROPERTIES", help="QEMU machine properties for UTM. An empty string keeps UTM's defaults (default: %(default)s)")
	init_vm.add_argument("--headless", action="store_true", help="create the VM without a display window")
	init_vm.add_argument("--audio", choices=AUDIO_MODES, default=AUDIO_COREAUDIO, help="audio output. coreaudio has the lowest latency (default: %(default)s)")
	_add_audio_args_option(init_vm)
	init_vm.add_argument("--mac", help="MAC address (default: a random 52:54:00:xx:xx:xx address)")
	init_vm.add_argument("--output", metavar="DIR", default=DEFAULT_BUNDLE_DIRECTORY, help="directory for the bundle (default: %(default)s)")
	init_vm.add_argument("--qemu-img", metavar="PATH", default=DEFAULT_QEMU_IMG_PATH, help="qemu-img executable (default: %(default)s)")


def _add_key_options(parser):
	parser.add_argument("--delay-ms", type=int, help="pause after each key press (default: key_delay_ms from the config)")
	parser.add_argument("--hold-ms", type=int, help="how long each combo stays down before it is released (default: {})".format(DEFAULT_HOLD_MS))
	parser.add_argument("--dry-run", action="store_true", help="print the QMP commands as JSON instead of sending them")


class CommandContext:
	def __init__(self, arguments, config, backend_factory, qmp_client_factory, stdout):
		self.arguments = arguments
		self.config = config
		self.stdout = stdout
		self._backend_factory = backend_factory
		self._qmp_client_factory = qmp_client_factory
		self._backend = None

	@property
	def backend(self):
		if self._backend is None:
			self._backend = self._backend_factory(self.config)
		return self._backend

	def connect_qmp(self, session=False):
		socket_path = self.config.qmp_socket or self.backend.qmp_socket_path(session=session)
		return self._qmp_client_factory(socket_path)

	def print(self, text):
		self.stdout.write(text + "\n")


def run_start(context):
	context.backend.start()


def run_stop(context):
	context.backend.stop(force=context.arguments.force)


def run_status(context):
	status = context.backend.status()
	context.print("{}: {}".format(status.name, status.state))


def run_ip(context):
	address = context.backend.ip()
	if address is None:
		raise WnError("no IP address is known for VM '{}' yet".format(context.config.vm_name))
	context.print(address)


def run_share(context):
	context.backend.share(context.arguments.host_path)


def run_net(context):
	context.backend.net()


def _send_or_print_steps(context, steps):
	if context.arguments.dry_run:
		for arguments, _ in steps:
			context.print(json.dumps({"execute": INPUT_SEND_EVENT, "arguments": arguments}))
		return
	with context.connect_qmp() as client:
		send_input_steps(client, steps)


def _send_or_print_combos(context, combos):
	arguments = context.arguments
	delay_ms = context.config.key_delay_ms if arguments.delay_ms is None else arguments.delay_ms
	_send_or_print_steps(context, combos_to_input_steps(combos, delay_ms, arguments.hold_ms))


def run_key(context):
	_send_or_print_combos(context, context.arguments.combos)


def run_type(context):
	_send_or_print_combos(context, text_to_combos(context.arguments.text))


MOUSE_BUTTON_STEPS = {
	"click": mouse.click_steps,
	"doubleclick": mouse.double_click_steps,
	"down": mouse.press_steps,
	"up": mouse.release_steps,
}


def _mouse_steps(context):
	arguments = context.arguments
	if arguments.mouse_action == "move":
		screen_size = arguments.screen or context.config.screen_size
		return mouse.move_steps(*mouse.position_to_abs(arguments.x, arguments.y, screen_size))
	if arguments.mouse_action == "scroll":
		return mouse.scroll_steps(arguments.direction, arguments.count)
	return MOUSE_BUTTON_STEPS[arguments.mouse_action](arguments.button)


def run_mouse(context):
	_send_or_print_steps(context, _mouse_steps(context))


class DryRunQMPClient:
	def __init__(self, print_line):
		self._print_line = print_line

	def execute(self, command, arguments=None):
		self._print_line(json.dumps({"execute": command, "arguments": arguments}))


def _grab_with(context, client):
	config = context.config
	arguments = context.arguments
	keycode_table = keygrab.load_keycode_table(config.keygrab_keycodes or keygrab.default_keycodes_path())
	keygrab.run_grab(
		client,
		config.keygrab_binary or keygrab.default_helper_path(),
		keycode_table,
		arguments.release_hotkey,
		arguments.start_released,
		context.stdout,
		send_repeats=config.keygrab_send_repeats,
	)


def run_grab(context):
	if context.arguments.dry_run:
		_grab_with(context, DryRunQMPClient(context.print))
		return
	with context.connect_qmp(session=True) as client:
		_grab_with(context, client)


def run_attach(context):
	if context.arguments.grab:
		run_grab(context)
		return
	with context.connect_qmp(session=True) as client:
		attach(client, context.config.key_delay_ms)


def random_mac_address():
	suffix = ["{:02x}".format(random.randrange(OCTET_VALUES)) for _ in range(MAC_SUFFIX_OCTET_COUNT)]
	return ":".join([QEMU_MAC_PREFIX] + suffix)


def _fill_bundle(bundle, output, options, arguments):
	data_directory = os.path.join(bundle, DATA_DIRECTORY_NAME)
	os.makedirs(data_directory)
	create_disk_image(os.path.join(data_directory, options["disk_image"]), arguments.disk_gib, arguments.qemu_img)
	for source, image in ((arguments.iso, options["iso_image"]), (arguments.driver_iso, options["driver_iso_image"])):
		if source:
			shutil.copyfile(source, os.path.join(data_directory, image))
	write_bundle(output, options)


def run_init_vm(context):
	arguments = context.arguments
	name = arguments.name or context.config.vm_name
	output = os.path.expanduser(arguments.output)
	bundle = os.path.join(output, name + BUNDLE_SUFFIX)
	if os.path.exists(bundle):
		raise WnError("{} already exists. Remove it first or pick another --name".format(bundle))
	options = {
		"name": name,
		"mac_address": arguments.mac or random_mac_address(),
		"cpu_count": arguments.cpus,
		"memory_mib": arguments.memory_mib,
		"disk_image": DEFAULT_OPTIONS["disk_image"],
		"iso_image": os.path.basename(arguments.iso) if arguments.iso else None,
		"driver_iso_image": os.path.basename(arguments.driver_iso) if arguments.driver_iso else None,
		"machine_properties": arguments.machine_properties,
		"headless": arguments.headless,
		"audio": arguments.audio,
		"audio_args": arguments.audio_args,
		"qmp_port": context.config.qmp_port,
		"qmp_session_port": context.config.qmp_session_port,
	}
	try:
		_fill_bundle(bundle, output, options, arguments)
	except BaseException as error:
		shutil.rmtree(bundle, ignore_errors=True)
		if isinstance(error, OSError):
			raise WnError("cannot create {}: {}".format(bundle, error)) from None
		raise
	context.print("Created {}".format(bundle))
	context.print("Next: open the bundle once in UTM, or run: open {}".format(shlex.quote(bundle)))


def run_update_vm_args(context):
	arguments = context.arguments
	config = context.config
	bundle = arguments.bundle or os.path.join(os.path.expanduser(DEFAULT_BUNDLE_DIRECTORY), config.vm_name + BUNDLE_SUFFIX)
	status = context.backend.status()
	if status.state != STOPPED_STATE:
		raise WnError("VM '{}' is {}. Run wn stop, wait for wn status to say stopped, then run update-vm-args again".format(status.name, status.state))
	new_arguments = update_additional_arguments(bundle, {
		"qmp_port": config.qmp_port,
		"qmp_session_port": config.qmp_session_port,
		"headless": arguments.headless,
		"audio": arguments.audio,
		"audio_args": arguments.audio_args,
	})
	context.print("Updated {}".format(os.path.join(bundle, "config.plist")))
	context.print("AdditionalArguments: {}".format(" ".join(shlex.quote(argument) for argument in new_arguments)))
	context.print("Next: quit UTM and open it again so it reads the new arguments, then run wn start")


COMMANDS = {
	"start": run_start,
	"stop": run_stop,
	"status": run_status,
	"ip": run_ip,
	"key": run_key,
	"type": run_type,
	"mouse": run_mouse,
	"attach": run_attach,
	"grab": run_grab,
	"share": run_share,
	"net": run_net,
	"init-vm": run_init_vm,
	"update-vm-args": run_update_vm_args,
}


def main(argv=None, backend_factory=create_backend, qmp_client_factory=qmp_client_for, stdout=None, stderr=None):
	stdout = stdout or sys.stdout
	stderr = stderr or sys.stderr
	arguments = build_parser().parse_args(argv)
	try:
		context = CommandContext(arguments, load_config(), backend_factory, qmp_client_factory, stdout)
		COMMANDS[arguments.command](context)
	except (WnError, NotImplementedError) as error:
		stderr.write("wn: error: {}\n".format(error))
		return EXIT_FAILURE
	return EXIT_SUCCESS
