import io
import json
import os
import tempfile
import unittest
from unittest import mock

from fakes import FakeBackend, RecordingQMPClient
from wn.cli import main
from wn.keys import combo_to_send_key_arguments


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
		self.assertEqual(self.backend.calls, [("stop", False)])

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


class KeyCommandTests(CLITestCase):
	def test_dry_run_prints_command_without_connecting(self):
		exit_code, stdout, _ = self.run_cli("key", "ctrl+alt+del", "--dry-run")
		self.assertEqual(exit_code, 0)
		self.assertEqual(json.loads(stdout), {"execute": "send-key", "arguments": combo_to_send_key_arguments("ctrl+alt+del")})
		self.assertEqual(self.qmp_log, [])
		self.assertEqual(self.backend.calls, [])

	def test_dry_run_with_several_combos_and_hold_time(self):
		_, stdout, _ = self.run_cli("key", "win+r", "enter", "--hold-ms", "100", "--dry-run")
		commands = [json.loads(line) for line in stdout.splitlines()]
		self.assertEqual([command["arguments"] for command in commands], [
			combo_to_send_key_arguments("win+r", 100),
			combo_to_send_key_arguments("enter", 100),
		])

	def test_sends_through_backend_socket(self):
		self.assertEqual(self.run_cli("key", "win+r")[0], 0)
		self.assertEqual(self.qmp_log, [
			("connect", "/fake/qmp.sock"),
			("send-key", combo_to_send_key_arguments("win+r")),
			("close",),
		])

	def test_config_socket_overrides_backend(self):
		config_directory = os.path.join(self.home.name, ".config", "wn")
		os.makedirs(config_directory)
		with open(os.path.join(config_directory, "config.json"), "w") as config_file:
			json.dump({"qmp_socket": "/configured/qmp.sock"}, config_file)
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
		commands = [json.loads(line) for line in stdout.splitlines()]
		self.assertEqual([command["arguments"] for command in commands], [
			combo_to_send_key_arguments("shift+h"),
			combo_to_send_key_arguments("i"),
		])

	def test_sends_text(self):
		self.run_cli("type", "ok")
		self.assertEqual([entry[0] for entry in self.qmp_log], ["connect", "send-key", "send-key", "close"])

	def test_unsupported_character(self):
		exit_code, _, stderr = self.run_cli("type", "café", "--dry-run")
		self.assertEqual(exit_code, 1)
		self.assertIn("U+00E9", stderr)


class AttachCommandTests(CLITestCase):
	def test_attach_connects_and_runs_attach(self):
		with mock.patch("wn.cli.attach") as attach:
			self.assertEqual(self.run_cli("attach")[0], 0)
		self.assertEqual(self.qmp_log[0], ("connect", "/fake/qmp.sock"))
		attach.assert_called_once()


if __name__ == "__main__":
	unittest.main()
