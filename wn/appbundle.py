"""Build the small app bundle that runs the agent, so macOS asks once for access to UTM's data and the camera."""

import hashlib
import json
import os
import plistlib
import shutil
import subprocess

from wn.errors import WnError

BUNDLE_IDENTIFIER = "com.wn.agent"
BUNDLE_NAME = "wn-agent"
CAMERA_NAME = "wn-camera"
APP_PATH = os.path.join("~", "Library", "Application Support", "wn", BUNDLE_NAME + ".app")
PACKAGE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
STUB_SOURCE = os.path.join(PACKAGE_DIRECTORY, "launchd", BUNDLE_NAME + ".c")
CAMERA_DIRECTORY = os.path.join(PACKAGE_DIRECTORY, "camera")
CAMERA_SOURCE_NAMES = ("main.m", "capture.m", "uvc.c", "usbredir.c")
CAMERA_SOURCE_SUFFIXES = (".c", ".h", ".m")
CAMERA_FRAMEWORKS = ("AVFoundation", "CoreMedia", "CoreVideo", "Foundation", "VideoToolbox")
SOURCE_HASH_FILE = "wn-source.sha256"
CLANG_PATH = "/usr/bin/clang"
CODESIGN_PATH = "/usr/bin/codesign"
AD_HOC_IDENTITY = "-"
APP_DATA_USAGE = "wn-agent writes the Mac's battery status to the battery file that UTM shares with the Windows VM."
CAMERA_USAGE = "wn-agent shows the Mac's camera to the Windows VM as a USB webcam."


class AppBundleError(WnError):
	"""Raised when the agent app bundle cannot be built."""


def app_path():
	return os.path.expanduser(APP_PATH)


def executable_path():
	return os.path.join(app_path(), "Contents", "MacOS", BUNDLE_NAME)


def camera_executable_path():
	return os.path.join(app_path(), "Contents", "MacOS", CAMERA_NAME)


def source_hash_path():
	return os.path.join(app_path(), "Contents", "Resources", SOURCE_HASH_FILE)


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
		"NSCameraUsageDescription": CAMERA_USAGE,
	}


def camera_build_command(output):
	"""Return the clang command that builds wn-camera at ``output``."""
	command = [CLANG_PATH, "-O2", "-fobjc-arc", "-Wall", "-o", output]
	command += [os.path.join(CAMERA_DIRECTORY, name) for name in CAMERA_SOURCE_NAMES]
	for framework in CAMERA_FRAMEWORKS:
		command += ["-framework", framework]
	return command


def _build_commands():
	return [
		[CLANG_PATH, "-O2", "-o", executable_path(), STUB_SOURCE],
		camera_build_command(camera_executable_path()),
	]


def _source_files():
	names = sorted(name for name in os.listdir(CAMERA_DIRECTORY) if name.endswith(CAMERA_SOURCE_SUFFIXES))
	return [STUB_SOURCE] + [os.path.join(CAMERA_DIRECTORY, name) for name in names]


def source_hash():
	"""Hash everything that goes into the bundle: Info.plist, the build commands, and every source file."""
	digest = hashlib.sha256()
	digest.update(plistlib.dumps(info_plist()))
	digest.update(json.dumps(_build_commands()).encode())
	for path in _source_files():
		digest.update(os.path.basename(path).encode() + b"\0")
		with open(path, "rb") as source_file:
			digest.update(source_file.read())
	return digest.hexdigest()


def installed_source_hash():
	try:
		with open(source_hash_path()) as hash_file:
			return hash_file.read().strip()
	except OSError:
		return None


def _check(result, tool):
	if result.returncode != 0:
		detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "no output"
		raise AppBundleError("{} failed with exit code {}: {}".format(tool, result.returncode, detail))


def build_app(run=None):
	"""Write the bundle, compile the launcher stub and wn-camera, and sign it ad hoc. Return the bundle path.

	macOS ties the camera grant to the bundle's code hash, so the bundle is rebuilt only when its sources change.
	"""
	run = run or subprocess.run
	path = app_path()
	expected = source_hash()
	if installed_source_hash() == expected:
		return path
	remove_app()
	os.makedirs(os.path.dirname(executable_path()))
	os.makedirs(os.path.dirname(source_hash_path()))
	with open(os.path.join(path, "Contents", "Info.plist"), "wb") as plist_file:
		plistlib.dump(info_plist(), plist_file)
	try:
		for command in _build_commands():
			_check(run(command, capture_output=True, text=True), "clang")
		with open(source_hash_path(), "w") as hash_file:
			hash_file.write(expected + "\n")
		_check(run([CODESIGN_PATH, "--sign", AD_HOC_IDENTITY, "--force", "--deep", path], capture_output=True, text=True), "codesign")
	except AppBundleError:
		remove_app()
		raise
	return path


def remove_app():
	"""Delete the bundle. Return False if it was not there."""
	path = app_path()
	if not os.path.exists(path):
		return False
	shutil.rmtree(path)
	return True
