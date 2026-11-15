import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from wn.appbundle import (
	BUNDLE_IDENTIFIER,
	CAMERA_DIRECTORY,
	STUB_SOURCE,
	AppBundleError,
	app_path,
	build_app,
	camera_build_command,
	camera_executable_path,
	executable_path,
	remove_app,
	source_hash,
)

CAMERA_SOURCES = [os.path.join(CAMERA_DIRECTORY, name) for name in ("main.m", "capture.m", "uvc.c", "usbredir.c")]


def completed(returncode=0, stderr=""):
	return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


def build_outputs(command, **kwargs):
	if "-o" in command:
		open(command[command.index("-o") + 1], "w").close()
	return completed()


class AppBundleTests(unittest.TestCase):
	def setUp(self):
		self.home = tempfile.TemporaryDirectory()
		self.addCleanup(self.home.cleanup)
		patcher = mock.patch.dict(os.environ, {"HOME": self.home.name})
		patcher.start()
		self.addCleanup(patcher.stop)
		self.run = mock.Mock(side_effect=build_outputs)
		self.app = os.path.join(self.home.name, "Library", "Application Support", "wn", "wn-agent.app")

	def test_paths(self):
		self.assertEqual(app_path(), self.app)
		self.assertEqual(executable_path(), os.path.join(self.app, "Contents", "MacOS", "wn-agent"))
		self.assertEqual(camera_executable_path(), os.path.join(self.app, "Contents", "MacOS", "wn-camera"))

	def test_stub_source_ships_with_the_package(self):
		self.assertTrue(os.path.isfile(STUB_SOURCE))
		for path in CAMERA_SOURCES:
			self.assertTrue(os.path.isfile(path), path)

	def test_camera_build_command(self):
		self.assertEqual(camera_build_command("/out/wn-camera"), ["/usr/bin/clang", "-O2", "-fobjc-arc", "-Wall", "-o", "/out/wn-camera"] + CAMERA_SOURCES + [
			"-framework", "AVFoundation", "-framework", "CoreMedia", "-framework", "CoreVideo", "-framework", "Foundation", "-framework", "VideoToolbox",
		])

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
		self.assertEqual(info["NSCameraUsageDescription"], "wn-agent shows the Mac's camera to the Windows VM as a USB webcam.")

	def test_build_compiles_the_stub_and_the_camera_then_signs_the_bundle(self):
		build_app(run=self.run)
		self.assertEqual(self.run.call_args_list, [
			mock.call(["/usr/bin/clang", "-O2", "-o", executable_path(), STUB_SOURCE], capture_output=True, text=True),
			mock.call(camera_build_command(camera_executable_path()), capture_output=True, text=True),
			mock.call(["/usr/bin/codesign", "--sign", "-", "--force", "--deep", self.app], capture_output=True, text=True),
		])

	def test_build_reports_a_compiler_failure(self):
		self.run.side_effect = None
		self.run.return_value = completed(1, "xcrun: error: invalid active developer path")
		with self.assertRaises(AppBundleError) as caught:
			build_app(run=self.run)
		self.assertIn("invalid active developer path", str(caught.exception))
		self.assertEqual(self.run.call_count, 1)

	def test_build_reports_a_signing_failure(self):
		self.run.side_effect = [completed(), completed(), completed(1, "resource fork not allowed")]
		with self.assertRaises(AppBundleError) as caught:
			build_app(run=self.run)
		self.assertIn("codesign", str(caught.exception))
		self.assertFalse(os.path.exists(self.app))

	def test_build_removes_the_bundle_when_interrupted_while_signing(self):
		self.run.side_effect = [completed(), completed(), KeyboardInterrupt()]
		with self.assertRaises(KeyboardInterrupt):
			build_app(run=self.run)
		self.assertFalse(os.path.exists(self.app))

	def test_build_replaces_an_old_bundle(self):
		stale = os.path.join(self.app, "Contents", "Resources", "old")
		os.makedirs(os.path.dirname(stale))
		open(stale, "w").close()
		build_app(run=self.run)
		self.assertFalse(os.path.exists(stale))

	def test_build_stores_the_source_hash(self):
		build_app(run=self.run)
		with open(os.path.join(self.app, "Contents", "Resources", "wn-source.sha256")) as hash_file:
			self.assertEqual(hash_file.read(), source_hash() + "\n")

	def test_build_skips_a_bundle_built_from_the_same_sources(self):
		build_app(run=self.run)
		self.run.reset_mock()
		self.assertEqual(build_app(run=self.run), self.app)
		self.run.assert_not_called()

	def test_build_runs_again_when_an_executable_is_missing(self):
		for path in (executable_path, camera_executable_path):
			build_app(run=self.run)
			os.remove(path())
			self.run.reset_mock()
			build_app(run=self.run)
			self.assertEqual(self.run.call_count, 3)
			self.assertTrue(os.path.exists(path()))

	def test_source_hash_changes_with_info_plist(self):
		before = source_hash()
		with mock.patch("wn.appbundle.CAMERA_USAGE", "changed"):
			self.assertNotEqual(source_hash(), before)

	def test_source_hash_changes_with_the_stub_source(self):
		before = source_hash()
		sources = tempfile.TemporaryDirectory()
		self.addCleanup(sources.cleanup)
		stub = os.path.join(sources.name, os.path.basename(STUB_SOURCE))
		with open(STUB_SOURCE, "rb") as original, open(stub, "wb") as copy:
			copy.write(original.read() + b"\n")
		with mock.patch("wn.appbundle.STUB_SOURCE", stub):
			self.assertNotEqual(source_hash(), before)

	def test_source_hash_changes_with_a_build_command(self):
		before = source_hash()
		with mock.patch("wn.appbundle.CAMERA_FRAMEWORKS", ("Foundation",)):
			self.assertNotEqual(source_hash(), before)

	def test_build_runs_again_when_a_camera_source_changes(self):
		sources = tempfile.TemporaryDirectory()
		self.addCleanup(sources.cleanup)
		copy = os.path.join(sources.name, "camera")
		shutil.copytree(CAMERA_DIRECTORY, copy)
		with mock.patch("wn.appbundle.CAMERA_DIRECTORY", copy):
			build_app(run=self.run)
			with open(os.path.join(copy, "uvc.h"), "a") as header:
				header.write("\n")
			self.run.reset_mock()
			build_app(run=self.run)
		self.assertEqual(self.run.call_count, 3)

	def test_build_runs_again_after_a_failed_build(self):
		self.run.side_effect = None
		self.run.return_value = completed(1, "error")
		with self.assertRaises(AppBundleError):
			build_app(run=self.run)
		self.run.side_effect = build_outputs
		self.run.reset_mock()
		build_app(run=self.run)
		self.assertEqual(self.run.call_count, 3)

	def test_remove(self):
		build_app(run=self.run)
		self.assertTrue(remove_app())
		self.assertFalse(os.path.exists(self.app))

	def test_remove_when_absent(self):
		self.assertFalse(remove_app())


if __name__ == "__main__":
	unittest.main()
