import os
import plistlib
import subprocess
import tempfile
import unittest
from unittest import mock

from wn.appbundle import (
	BUNDLE_IDENTIFIER,
	STUB_SOURCE,
	AppBundleError,
	app_path,
	build_app,
	executable_path,
	remove_app,
)


def completed(returncode=0, stderr=""):
	return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


class AppBundleTests(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		self.addCleanup(self.home.cleanup)
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.run = mock.Mock(return_value=completed())
		self.app = os.path.join(self.home.name, "Library", "Application Support", "wn", "wn-agent.app")

	def test_paths(self):
		self.assertEqual(app_path(), self.app)
		self.assertEqual(executable_path(), os.path.join(self.app, "Contents", "MacOS", "wn-agent"))

	def test_stub_source_ships_with_the_package(self):
		self.assertTrue(os.path.isfile(STUB_SOURCE))

	def test_build_writes_info_plist(self):
		self.assertEqual(build_app(run=self.run), self.app)
		with open(os.path.join(self.app, "Contents", "Info.plist"), "rb") as plist_file:
			info = plistlib.load(plist_file)
		self.assertEqual(info["CFBundleIdentifier"], BUNDLE_IDENTIFIER)
		self.assertEqual(info["CFBundleIdentifier"], "com.wn.agent")
		self.assertEqual(info["CFBundleName"], "wn-agent")
		self.assertEqual(info["CFBundleExecutable"], "wn-agent")
		self.assertEqual(info["CFBundlePackageType"], "APPL")
		self.assertTrue(info["LSUIElement"])
		self.assertTrue(info["LSBackgroundOnly"])
		self.assertIn("battery", info["NSAppDataUsageDescription"])

	def test_build_compiles_the_stub_then_signs_the_bundle(self):
		build_app(run=self.run)
		self.assertEqual(self.run.call_args_list, [
			mock.call(["/usr/bin/clang", "-O2", "-o", executable_path(), STUB_SOURCE], capture_output=True, text=True),
			mock.call(["/usr/bin/codesign", "--sign", "-", "--force", "--deep", self.app], capture_output=True, text=True),
		])

	def test_build_reports_a_compiler_failure(self):
		self.run.return_value = completed(1, "xcrun: error: invalid active developer path")
		with self.assertRaises(AppBundleError) as caught:
			build_app(run=self.run)
		self.assertIn("invalid active developer path", str(caught.exception))
		self.assertEqual(self.run.call_count, 1)

	def test_build_reports_a_signing_failure(self):
		self.run.side_effect = [completed(), completed(1, "resource fork not allowed")]
		with self.assertRaises(AppBundleError) as caught:
			build_app(run=self.run)
		self.assertIn("codesign", str(caught.exception))

	def test_build_replaces_an_old_bundle(self):
		stale = os.path.join(self.app, "Contents", "Resources", "old")
		os.makedirs(os.path.dirname(stale))
		open(stale, "w").close()
		build_app(run=self.run)
		self.assertFalse(os.path.exists(stale))

	def test_remove(self):
		build_app(run=self.run)
		self.assertTrue(remove_app())
		self.assertFalse(os.path.exists(self.app))

	def test_remove_when_absent(self):
		self.assertFalse(remove_app())


if __name__ == "__main__":
	unittest.main()
