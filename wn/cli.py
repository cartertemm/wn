"""Command line interface for wn."""

import argparse
import difflib
import getpass
import json
import os
import shlex
import sys
import time

from wn import __version__, agent, keygrab, mouse
from wn.backend import create_backend
from wn.backends.utm import DEFAULT_VM_NAME
from wn.battery_file import battery_writer, prepare_battery_file
from wn.battery_sync import run_battery_command
from wn.config import load_config
from wn.downloads import DEFAULT_DOWNLOADS_DIR
from wn.errors import WnError
from wn.keys import DEFAULT_HOLD_MS, INPUT_SEND_EVENT, combos_to_input_steps, send_input_steps
from wn.qmp import QMPConnectionError, QMPError, qmp_client_for
from wn.screendump import save_screenshot
from wn.setup import DEFAULT_DISK_GIB, SOFTWARE_CHOICES, STOPPED_STATE, Installer, SetupOptions
from wn.text_input import text_to_combos
from wn.unattend import DEFAULT_EDITION, GENERIC_PRODUCT_KEYS
from wn.utm_config import (
	BLOCK_AUDIO,
	BLOCK_BATTERY,
	BLOCK_EXTRA,
	BLOCK_QMP,
	BLOCK_RAMFB,
	BUNDLE_SUFFIX,
	DEFAULT_BUNDLE_DIRECTORY,
	DEFAULT_OPTIONS,
	argument_units,
	battery_file_for_bundle,
	update_additional_arguments,
)

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
DEFAULT_SCROLL_COUNT = 1
DEFAULT_STOP_TIMEOUT_SECONDS = 120
STOP_POLL_SECONDS = 2
FORCE_STOP_TIMEOUT_SECONDS = 10
WAKE_DISPLAY_KEY = "shift"
WAKE_DISPLAY_SECONDS = 2
NO_SOFTWARE_ANSWERS = ("0", "none")
YES_ANSWERS = ("y", "yes")
NO_ANSWERS = ("n", "no")


def build_parser():
	parser = argparse.ArgumentParser(prog="wn", description="Control a Windows VM from the terminal.")
	parser.add_argument("--version", action="version", version="wn " + __version__)
	subcommands = parser.add_subparsers(dest="command", metavar="command")
	subcommands.required = True

	subcommands.add_parser("start", help="start the VM")
	stop = subcommands.add_parser("stop", help="shut down the VM")
	stop.add_argument("--force", action="store_true", help="power off at once instead of a clean shutdown")
	stop.add_argument("--timeout", type=int, metavar="SECONDS", help="how long to wait for Windows to shut down (default: {})".format(DEFAULT_STOP_TIMEOUT_SECONDS))
	stop.add_argument("--no-wait", action="store_true", help="request the shutdown and return at once")
	stop.add_argument("--force-after-timeout", action="store_true", help="power off if Windows is still running after the timeout")
	stop.add_argument("--yes", action="store_true", help="with --force, power off without asking first")
	stop.set_defaults(stop_parser=stop)
	subcommands.add_parser("status", help="show whether the VM is running")
	subcommands.add_parser("ip", help="print the guest IP address")

	key = subcommands.add_parser("key", help="press key combos, such as ctrl+alt+del or win+r")
	key.add_argument("combos", nargs="+", metavar="combo")
	_add_key_options(key)

	type_text = subcommands.add_parser("type", help="type text with a US keyboard layout")
	type_text.add_argument("text")
	_add_key_options(type_text)

	_add_mouse_parser(subcommands)

	screenshot = subcommands.add_parser("screenshot", help="save the guest screen as a PNG file")
	screenshot.add_argument("output", metavar="OUTPUT", help="path of the image to write. It is always PNG, whatever the extension")

	grab = subcommands.add_parser("grab", help="send every key press on the Mac keyboard to the guest, including the Windows key")
	grab.add_argument("--start-released", action="store_true", help="start with the keyboard released, not grabbed")
	grab.add_argument("--release-hotkey", default=keygrab.DEFAULT_RELEASE_HOTKEY, metavar="MODIFIERS", help="comma separated modifiers that release and grab the keyboard (default: %(default)s)")
	grab.add_argument("--dry-run", action="store_true", help="print the QMP commands as JSON instead of sending them")
	_add_update_vm_args_parser(subcommands)
	_add_setup_parser(subcommands)
	_add_agent_parser(subcommands)
	_add_battery_parser(subcommands)
	return parser


def _add_setup_parser(subcommands):
	setup = subcommands.add_parser("setup", help="create a Windows 11 VM in UTM and install it with no further input")
	setup.add_argument("--yes", action="store_true", help="do not ask questions. Use the flags and the defaults")
	setup.add_argument("--name", default=DEFAULT_VM_NAME, help="VM name in UTM (default: %(default)s)")
	setup.add_argument("--username", help="Windows user name (default: your Mac user name)")
	setup.add_argument("--password", default="", help="Windows password (default: none)")
	setup.add_argument("--cpus", type=int, default=DEFAULT_OPTIONS["cpu_count"], help="number of CPU cores (default: %(default)s)")
	setup.add_argument("--memory-mib", type=int, default=DEFAULT_OPTIONS["memory_mib"], help="memory in MiB (default: %(default)s)")
	setup.add_argument("--disk-gib", type=int, default=DEFAULT_DISK_GIB, help="disk size in GiB (default: %(default)s)")
	setup.add_argument("--headless", action="store_true", help="create the VM without a display window")
	setup.add_argument("--software", default=",".join(name for name, _ in SOFTWARE_CHOICES), help="comma separated software to install, or none (default: %(default)s)")
	setup.add_argument("--edition", default=DEFAULT_EDITION, help="Windows 11 edition (default: %(default)s)")
	setup.add_argument("--no-battery", action="store_true", help="do not add the native battery: no firmware driver, no Secure Boot certificate, no NVDIMM, and no wn agent")
	setup.add_argument("--downloads-dir", default=DEFAULT_DOWNLOADS_DIR, metavar="DIR", help="where to keep the downloaded ISOs and installers (default: %(default)s)")
	setup.add_argument("--output", metavar="DIR", default=DEFAULT_BUNDLE_DIRECTORY, help="directory for the bundle (default: %(default)s)")


def _add_agent_parser(subcommands):
	agent_parser = subcommands.add_parser("agent", help="write the Mac's battery status to the native battery in the guest")
	modes = agent_parser.add_mutually_exclusive_group()
	modes.add_argument("--once", action="store_true", help="print the battery status as JSON and exit")
	modes.add_argument("--install", action="store_true", help="start the agent at every login with a launchd agent in ~/Library/LaunchAgents")
	modes.add_argument("--uninstall", action="store_true", help="remove the launchd agent")
	actions = agent_parser.add_subparsers(dest="agent_command", metavar="action")
	start = actions.add_parser("start", help="start the launchd agent if it is installed, else run in the foreground until Ctrl+C")
	start.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
	actions.add_parser("stop", help="stop the launchd agent, or the agent running in the foreground")
	actions.add_parser("status", help="show whether the agent is installed and running. Exits 1 when it is not running")
	agent_parser.set_defaults(agent_parser=agent_parser)


def _add_battery_parser(subcommands):
	battery = subcommands.add_parser("battery", help="read or write the battery block that the guest firmware driver reads")
	actions = battery.add_subparsers(dest="battery_command", metavar="action")
	actions.required = True
	actions.add_parser("status", help="read the battery block from the guest and print it")
	actions.add_parser("sync", help="write the Mac's battery status to the guest once")
	set_parser = actions.add_parser("set", help="write a fake battery status once, for testing. A running wn agent overwrites it within 30 s")
	set_parser.add_argument("--percent", type=int, required=True, help="charge level, 0 to 100")
	power = set_parser.add_mutually_exclusive_group()
	power.add_argument("--charging", action="store_true", help="on AC power and charging")
	power.add_argument("--discharging", action="store_true", help="on battery power (the default)")
	power.add_argument("--ac", action="store_true", help="on AC power, not charging")


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
	update = subcommands.add_parser("update-vm-args", help="change parts of the QEMU additional arguments of an existing UTM bundle. Stop the VM first",
		description="Each flag replaces one part of the AdditionalArguments. Every argument that no flag names stays as it is.")
	update.add_argument("--bundle", metavar="PATH", help="the .utm bundle (default: vm_name.utm in UTM's documents folder)")
	update.add_argument("--qmp", action="store_true", help="write the QMP servers again, as Unix sockets in the UTM group container")
	update.add_argument("--headless", action="store_true", help="add the ramfb display that a VM with no display window needs")
	battery = update.add_mutually_exclusive_group()
	battery.add_argument("--battery", action="store_true", help="map the battery file into the guest as an NVDIMM, for the wn-acpi firmware driver")
	battery.add_argument("--no-battery", action="store_true", help="remove the battery NVDIMM")
	update.add_argument("--audio-args", metavar="ARGS", help="QEMU arguments that replace the audio arguments, such as \"-audiodev coreaudio,id=ca0,... -device intel-hda -device hda-output,audiodev=ca0\"")
	update.add_argument("--extra-args", metavar="ARGS", help="QEMU arguments that replace every argument that is not QMP, ramfb, audio, or battery, such as \"-device virtio-keyboard-pci,id=vkbd\"")


def _add_key_options(parser):
	parser.add_argument("--delay-ms", type=int, help="pause after each key press (default: key_delay_ms from the config)")
	parser.add_argument("--hold-ms", type=int, help="how long each combo stays down before it is released (default: {})".format(DEFAULT_HOLD_MS))
	parser.add_argument("--dry-run", action="store_true", help="print the QMP commands as JSON instead of sending them")


class CommandContext:
	def __init__(self, arguments, config, backend_factory, qmp_client_factory, stdout, stdin=None, stderr=None):
		self.arguments = arguments
		self.config = config
		self.stdout = stdout
		self.stdin = stdin
		self.stderr = stderr
		self._backend_factory = backend_factory
		self._qmp_client_factory = qmp_client_factory
		self._backend = None

	@property
	def backend(self):
		if self._backend is None:
			self._backend = self._backend_factory(self.config)
		return self._backend

	def qmp_address(self, session=False):
		return self.config.qmp_socket or self.backend.qmp_socket_path(session=session)

	def connect_qmp(self, session=False):
		return self._qmp_client_factory(self.qmp_address(session=session))

	def print(self, text):
		self.stdout.write(text + "\n")


def run_start(context):
	context.backend.start()


def run_stop(context):
	arguments = context.arguments
	if arguments.no_wait and (arguments.force_after_timeout or arguments.timeout is not None):
		arguments.stop_parser.error("--no-wait cannot be used with --timeout or --force-after-timeout")
	if arguments.force:
		if not arguments.yes and not _confirm_power_off(context):
			context.print("{}: not powered off".format(context.config.vm_name))
			return EXIT_FAILURE
		context.backend.stop(force=True)
		return
	_wake_display(context)
	context.backend.stop()
	if arguments.no_wait:
		context.print("{}: shutdown requested".format(context.config.vm_name))
		return
	timeout = DEFAULT_STOP_TIMEOUT_SECONDS if arguments.timeout is None else arguments.timeout
	status = _wait_until_stopped(context, timeout, announce=True)
	if status.state == STOPPED_STATE:
		context.print("{}: stopped".format(status.name))
		return
	if not arguments.force_after_timeout:
		context.stderr.write("wn: error: {} is still {} {} s after the shutdown request. Windows may be busy or showing a dialog. Check the screen, run wn stop --force to power off now, or use wn stop --force-after-timeout next time\n".format(status.name, status.state, timeout))
		return EXIT_FAILURE
	context.backend.stop(force=True)
	context.print("{}: still {} after {} s, powered off".format(status.name, status.state, timeout))
	status = _wait_until_stopped(context, FORCE_STOP_TIMEOUT_SECONDS)
	if status.state == STOPPED_STATE:
		context.print("{}: stopped".format(status.name))
		return
	context.stderr.write("wn: error: {} is still {} after the power off\n".format(status.name, status.state))
	return EXIT_FAILURE


def _wait_until_stopped(context, timeout, announce=False):
	"""Poll the VM until it has stopped or the timeout passes, and return the last status."""
	deadline = time.monotonic() + timeout
	while True:
		status = context.backend.status()
		if status.state == STOPPED_STATE or time.monotonic() >= deadline:
			return status
		if announce:
			context.print("{}: waiting up to {} s for Windows to shut down".format(status.name, timeout))
			announce = False
		time.sleep(STOP_POLL_SECONDS)


def _confirm_power_off(context):
	if not context.stdin.isatty():
		raise WnError("cannot ask before powering off because input is not a terminal. Add --yes to power off without asking")
	question = "Power off {} without shutting it down? Unsaved work is lost.".format(context.config.vm_name)
	return Prompter(context.stdin, context.stdout).ask(question, False, lambda answer: answer.lower() in YES_ANSWERS, default_text="y/N")


def _wake_display(context):
	"""Press a key so Windows turns the display on. Windows ignores the power button while the display is off."""
	try:
		with context.connect_qmp() as client:
			send_input_steps(client, combos_to_input_steps([WAKE_DISPLAY_KEY], delay_ms=0))
	except (QMPConnectionError, QMPError) as error:
		context.stderr.write("wn: warning: cannot wake the display before the shutdown request: {}\n".format(error))
		return
	time.sleep(WAKE_DISPLAY_SECONDS)


def run_status(context):
	status = context.backend.status()
	context.print("{}: {}".format(status.name, status.state))


def run_ip(context):
	address = context.backend.ip()
	if address is None:
		raise WnError("no IP address is known for VM '{}' yet".format(context.config.vm_name))
	context.print(address)


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


def run_screenshot(context):
	with context.connect_qmp() as client:
		path = save_screenshot(client, os.path.abspath(context.arguments.output))
	context.print(path)


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


def run_update_vm_args(context):
	arguments = context.arguments
	config = context.config
	bundle = arguments.bundle or os.path.join(os.path.expanduser(DEFAULT_BUNDLE_DIRECTORY), config.vm_name + BUNDLE_SUFFIX)
	status = context.backend.status()
	if status.state != STOPPED_STATE:
		raise WnError("VM '{}' is {}. Run wn stop, wait for wn status to say stopped, then run update-vm-args again".format(status.name, status.state))
	blocks = [block for block, named in (
		(BLOCK_QMP, arguments.qmp),
		(BLOCK_RAMFB, arguments.headless),
		(BLOCK_AUDIO, arguments.audio_args is not None),
		(BLOCK_BATTERY, arguments.battery or arguments.no_battery),
		(BLOCK_EXTRA, arguments.extra_args is not None),
	) if named]
	before, after = update_additional_arguments(bundle, {
		"headless": arguments.headless,
		"audio_args": arguments.audio_args,
		"extra_args": arguments.extra_args,
		"battery": arguments.battery,
		"battery_file": config.battery_file,
	}, blocks)
	if arguments.battery:
		battery_file = config.battery_file or battery_file_for_bundle(bundle)
		try:
			prepare_battery_file(battery_file)
		except WnError as error:
			raise WnError("{}. Create the folder or set battery_file in the config".format(error)) from None
		context.print("Battery file: {}".format(battery_file))
	if before == after:
		context.print("AdditionalArguments in {} did not change".format(os.path.join(bundle, "config.plist")))
		return
	context.print("Updated {}".format(os.path.join(bundle, "config.plist")))
	for line in difflib.unified_diff(_argument_lines(before), _argument_lines(after), "AdditionalArguments before", "AdditionalArguments after", lineterm=""):
		context.print(line)
	context.print("Next: quit UTM and open it again so it reads the new arguments, then run wn start")


def _argument_lines(arguments):
	return [" ".join(shlex.quote(argument) for argument in unit) for unit in argument_units(arguments)]


def _parse_software(text):
	if text.strip().lower() in NO_SOFTWARE_ANSWERS or not text.strip():
		return ()
	known = [name for name, _ in SOFTWARE_CHOICES]
	chosen = []
	for item in (part.strip().lower() for part in text.split(",")):
		if item.isdigit() and 1 <= int(item) <= len(known):
			item = known[int(item) - 1]
		if item not in known:
			raise WnError("unknown software '{}'. Choose from: {}".format(item, ", ".join(known)))
		if item not in chosen:
			chosen.append(item)
	return tuple(chosen)


def _check_edition(edition):
	if edition not in GENERIC_PRODUCT_KEYS:
		raise WnError("unknown edition '{}'. Choose from: {}".format(edition, ", ".join(GENERIC_PRODUCT_KEYS)))
	return edition


class Prompter:
	"""Asks the setup questions on stdout and reads the answers from stdin. An empty answer keeps the default."""

	def __init__(self, stdin, stdout):
		self.stdin = stdin
		self.stdout = stdout

	def _read(self, question, default_text, secret=False):
		prompt = "{} [{}]: ".format(question, default_text)
		if secret and self.stdin.isatty():
			return getpass.getpass(prompt)
		self.stdout.write(prompt)
		self.stdout.flush()
		line = self.stdin.readline()
		if not line:
			self.stdout.write("\n")
		return line.strip()

	def ask(self, question, default, convert=str, secret=False, default_text=None):
		while True:
			answer = self._read(question, default if default_text is None else default_text, secret)
			if not answer:
				return default
			try:
				return convert(answer)
			except (ValueError, WnError) as error:
				self.stdout.write("{}\n".format(error))


def _whole_number(text):
	try:
		return int(text)
	except ValueError:
		raise ValueError("Enter a whole number") from None


def _yes_or_no(text):
	answer = text.lower()
	if answer in YES_ANSWERS:
		return True
	if answer in NO_ANSWERS:
		return False
	raise ValueError("Answer yes or no")


def _software_menu():
	return "".join("  {}. {}\n".format(number, label) for number, (_, label) in enumerate(SOFTWARE_CHOICES, start=1))


def _numbers_for(software):
	names = [name for name, _ in SOFTWARE_CHOICES]
	return ",".join(str(names.index(name) + 1) for name in software) or "0"


def _setup_answers(arguments, stdin, stdout):
	answers = {
		"name": arguments.name,
		"username": arguments.username or getpass.getuser(),
		"password": arguments.password,
		"cpus": arguments.cpus,
		"memory_mib": arguments.memory_mib,
		"disk_gib": arguments.disk_gib,
		"headless": arguments.headless,
		"software": _parse_software(arguments.software),
		"edition": _check_edition(arguments.edition),
	}
	if arguments.yes:
		return answers
	prompter = Prompter(stdin, stdout)
	answers["name"] = prompter.ask("VM name", answers["name"])
	answers["username"] = prompter.ask("Windows user name", answers["username"])
	answers["password"] = prompter.ask("Windows password", answers["password"], secret=True, default_text="none" if not answers["password"] else "as given")
	answers["cpus"] = prompter.ask("CPU cores", answers["cpus"], _whole_number)
	answers["memory_mib"] = prompter.ask("Memory in MiB", answers["memory_mib"], _whole_number)
	answers["disk_gib"] = prompter.ask("Disk size in GiB", answers["disk_gib"], _whole_number)
	answers["headless"] = prompter.ask("Run without a display window (yes or no)", answers["headless"], _yes_or_no, default_text="yes" if answers["headless"] else "no")
	stdout.write("Software to install:\n" + _software_menu())
	answers["software"] = prompter.ask("Numbers separated by commas, 0 for none", answers["software"], _parse_software, default_text=_numbers_for(answers["software"]))
	answers["edition"] = prompter.ask("Windows 11 edition", answers["edition"], _check_edition)
	return answers


def run_agent(context):
	arguments = context.arguments
	has_flag = arguments.once or arguments.install or arguments.uninstall
	if has_flag == bool(arguments.agent_command):
		arguments.agent_parser.error("give one of start, stop, status, --install, --uninstall, or --once")
	return agent.run_agent_command(arguments, context.config, context.stdout, context.stderr)


def run_battery(context):
	run_battery_command(context.arguments, battery_writer(context.config), context.stdout, agent.battery_status)


def run_setup(context):
	arguments = context.arguments
	answers = _setup_answers(arguments, context.stdin, context.stdout)
	options = SetupOptions(
		bundle_dir=arguments.output,
		downloads_dir=arguments.downloads_dir,
		battery=not arguments.no_battery,
		battery_file=context.config.battery_file,
		**answers
	)
	Installer(options, context.stdout).run()


COMMANDS = {
	"start": run_start,
	"stop": run_stop,
	"status": run_status,
	"ip": run_ip,
	"key": run_key,
	"type": run_type,
	"mouse": run_mouse,
	"screenshot": run_screenshot,
	"grab": run_grab,
	"update-vm-args": run_update_vm_args,
	"setup": run_setup,
	"agent": run_agent,
	"battery": run_battery,
}


def main(argv=None, backend_factory=create_backend, qmp_client_factory=qmp_client_for, stdout=None, stderr=None, stdin=None):
	stdout = stdout or sys.stdout
	stderr = stderr or sys.stderr
	arguments = build_parser().parse_args(argv)
	try:
		context = CommandContext(arguments, load_config(), backend_factory, qmp_client_factory, stdout, stdin or sys.stdin, stderr)
		exit_code = COMMANDS[arguments.command](context)
	except WnError as error:
		stderr.write("wn: error: {}\n".format(error))
		return EXIT_FAILURE
	return EXIT_SUCCESS if exit_code is None else exit_code
