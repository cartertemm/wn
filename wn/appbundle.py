"""Build the small app bundle that runs the agent, so macOS asks once for access to UTM's data."""

import os
import plistlib
import shutil
import subprocess

from wn.errors import WnError

BUNDLE_IDENTIFIER = "com.wn.agent"
BUNDLE_NAME = "wn-agent"
APP_PATH = os.path.join("~", "Library", "Application Support", "wn", BUNDLE_NAME + ".app")
PACKAGE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
STUB_SOURCE = os.path.join(PACKAGE_DIRECTORY, "launchd", BUNDLE_NAME + ".c")
CAMERA_DIRECTORY = os.path.join(PACKAGE_DIRECTORY, "camera")
CAMERA_SOURCE_NAMES = ("main.m", "capture.m", "uvc.c", "usbredir.c")
CAMERA_FRAMEWORKS = ("AVFoundation", "CoreMedia", "CoreVideo", "Foundation", "VideoToolbox")
CLANG_PATH = "/usr/bin/clang"
CODESIGN_PATH = "/usr/bin/codesign"
AD_HOC_IDENTITY = "-"
APP_DATA_USAGE = "wn-agent writes the Mac's battery status to the battery file that UTM shares with the Windows VM."


class AppBundleError(WnError):
	"""Raised when the agent app bundle cannot be built."""


def app_path():
	return os.path.expanduser(APP_PATH)


def executable_path():
	return os.path.join(app_path(), "Contents", "MacOS", BUNDLE_NAME)


def info_plist():
	return {
		"CFBundleIdentifier": BUNDLE_IDENTIFIER,
		"CFBundleName": BUNDLE_NAME,
		"CFBundleExecutable": BUNDLE_NAME,
		"CFBundlePackageType": "APPL",
		"CFBundleInfoDictionaryVersion": "6.0",
		"CFBundleVersion": "1",
		"LSUIElement": True,
		"LSBackgroundOnly": True,
		"NSAppDataUsageDescription": APP_DATA_USAGE,
	}


def camera_build_command(output):
	"""Return the clang command that builds wn-camera at ``output``."""
	command = [CLANG_PATH, "-O2", "-fobjc-arc", "-Wall", "-o", output]
	command += [os.path.join(CAMERA_DIRECTORY, name) for name in CAMERA_SOURCE_NAMES]
	for framework in CAMERA_FRAMEWORKS:
		command += ["-framework", framework]
	return command


def _check(result, tool):
	if result.returncode != 0:
		detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "no output"
		raise AppBundleError("{} failed with exit code {}: {}".format(tool, result.returncode, detail))


def build_app(run=None):
	"""Write the bundle, compile its launcher stub, and sign it ad hoc. Return the bundle path."""
	run = run or subprocess.run
	path = app_path()
	remove_app()
	os.makedirs(os.path.dirname(executable_path()))
	with open(os.path.join(path, "Contents", "Info.plist"), "wb") as plist_file:
		plistlib.dump(info_plist(), plist_file)
	_check(run([CLANG_PATH, "-O2", "-o", executable_path(), STUB_SOURCE], capture_output=True, text=True), "clang")
	_check(run([CODESIGN_PATH, "--sign", AD_HOC_IDENTITY, "--force", "--deep", path], capture_output=True, text=True), "codesign")
	return path


def remove_app():
	"""Delete the bundle. Return False if it was not there."""
	path = app_path()
	if not os.path.exists(path):
		return False
	shutil.rmtree(path)
	return True
