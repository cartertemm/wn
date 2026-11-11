"""Command line interface for wn."""

import argparse
import json
import sys

from wn import __version__, keygrab
from wn.attach import attach
from wn.backend import create_backend
from wn.config import load_config
from wn.errors import WnError
from wn.keys import send_combos, validate_combos
from wn.qmp import QMPClient
from wn.text_input import text_to_combos

EXIT_SUCCESS = 0
EXIT_FAILURE = 1


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
	return parser


def _add_key_options(parser):
	parser.add_argument("--delay-ms", type=int, help="pause after each key press (default: key_delay_ms from the config)")
	parser.add_argument("--hold-ms", type=int, help="how long QEMU holds the keys down (QEMU default: 100)")
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

	def connect_qmp(self):
		socket_path = self.config.qmp_socket or self.backend.qmp_socket_path()
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


def _send_or_print_combos(context, combos):
	arguments = context.arguments
	if arguments.dry_run:
		for send_key_arguments in validate_combos(combos, arguments.hold_ms):
			context.print(json.dumps({"execute": "send-key", "arguments": send_key_arguments}))
		return
	delay_ms = context.config.key_delay_ms if arguments.delay_ms is None else arguments.delay_ms
	validate_combos(combos)
	with context.connect_qmp() as client:
		send_combos(client, combos, delay_ms, arguments.hold_ms)


def run_key(context):
	_send_or_print_combos(context, context.arguments.combos)


def run_type(context):
	_send_or_print_combos(context, text_to_combos(context.arguments.text))


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
	with context.connect_qmp() as client:
		_grab_with(context, client)


def run_attach(context):
	if context.arguments.grab:
		run_grab(context)
		return
	with context.connect_qmp() as client:
		attach(client, context.config.key_delay_ms)


COMMANDS = {
	"start": run_start,
	"stop": run_stop,
	"status": run_status,
	"ip": run_ip,
	"key": run_key,
	"type": run_type,
	"attach": run_attach,
	"grab": run_grab,
	"share": run_share,
	"net": run_net,
}


def main(argv=None, backend_factory=create_backend, qmp_client_factory=QMPClient, stdout=None, stderr=None):
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
