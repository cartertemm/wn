"""Build the ISO image that carries the answer file, virtio drivers, and installers into Windows Setup."""

import contextlib
import dataclasses
import fnmatch
import os
import shutil
import subprocess
import tempfile

from wn.errors import WnError
from wn.unattend import (
	BATTERY_INSTALLER,
	BATTERY_NOTIFY_INSTALLER,
	BATTERY_NOTIFY_SOURCE,
	DRIVER_SUBPATH,
	EFI_FOLDER,
	FIRST_LOGON_DRIVERS,
	MIC_SPEAKER_SCRIPT,
	PAYLOAD_TAG,
	SETUP_DRIVERS,
	TOOLS_FOLDER,
	VIRTIO_FOLDER,
	WINPE_DRIVER_FOLDER,
	build_unattend,
	setup_commands,
)

VOLUME_NAME = "WNSETUP"
ANSWER_FILE_NAME = "autounattend.xml"
GUEST_TOOLS_PATTERN = "utm-guest-tools-*.exe"
GUEST_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guest")
MIC_SPEAKER_SCRIPT_PATH = os.path.join(GUEST_DIRECTORY, MIC_SPEAKER_SCRIPT)
BATTERY_DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "efi", "wn-acpi.efi")
BATTERY_FILES = (
	BATTERY_DRIVER,
	os.path.join(GUEST_DIRECTORY, BATTERY_INSTALLER),
	os.path.join(GUEST_DIRECTORY, BATTERY_NOTIFY_INSTALLER),
	os.path.join(GUEST_DIRECTORY, BATTERY_NOTIFY_SOURCE),
)


class PayloadError(WnError):
	"""Raised when the payload cannot be staged or the disk image cannot be written."""


def _hdiutil(*arguments):
	result = subprocess.run(["hdiutil"] + list(arguments), capture_output=True, text=True)
	if result.returncode != 0:
		detail = result.stderr.strip() or result.stdout.strip() or "no output"
		raise PayloadError("hdiutil {} failed: {}".format(arguments[0], detail))
	return result.stdout


@contextlib.contextmanager
def mounted(image_path):
	"""Attach a disk image read-only without showing it in Finder and yield its mount point."""
	with tempfile.TemporaryDirectory(prefix="wn-mount-") as mount_point:
		_hdiutil("attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount_point, image_path)
		try:
			yield mount_point
		finally:
			try:
				_hdiutil("detach", mount_point)
			except PayloadError:
				_hdiutil("detach", "-force", mount_point)


def _copy_tree(source, destination):
	"""Copy file contents only, so read-only modes from an ISO do not follow the files."""
	for parent, _, names in os.walk(source):
		target = os.path.join(destination, os.path.relpath(parent, source))
		os.makedirs(target, exist_ok=True)
		for name in names:
			shutil.copyfile(os.path.join(parent, name), os.path.join(target, name))


def _stage_drivers(staging, virtio_mount):
	for driver in FIRST_LOGON_DRIVERS:
		source = os.path.join(virtio_mount, driver, *DRIVER_SUBPATH)
		if not os.path.isdir(source):
			raise PayloadError("the virtio-win ISO has no {} folder".format(os.path.join(driver, *DRIVER_SUBPATH)))
		_copy_tree(source, os.path.join(staging, VIRTIO_FOLDER, driver, *DRIVER_SUBPATH))
		if driver in SETUP_DRIVERS:
			_copy_tree(source, os.path.join(staging, WINPE_DRIVER_FOLDER, driver))


def _stage_guest_tools(tools_folder, guest_tools_mount):
	installers = sorted(fnmatch.filter(os.listdir(guest_tools_mount), GUEST_TOOLS_PATTERN))
	if not installers:
		raise PayloadError("the UTM guest tools ISO has no {} installer".format(GUEST_TOOLS_PATTERN))
	installer = installers[-1]
	shutil.copyfile(os.path.join(guest_tools_mount, installer), os.path.join(tools_folder, installer))
	return installer


def _stage_battery(staging):
	efi_folder = os.path.join(staging, EFI_FOLDER)
	os.makedirs(efi_folder, exist_ok=True)
	for source in BATTERY_FILES:
		shutil.copyfile(source, os.path.join(efi_folder, os.path.basename(source)))


def stage_payload(staging, virtio_iso, guest_tools_iso, extra_tools=(), battery=True):
	"""Fill ``staging`` with the drivers, guest tools, ``extra_tools``, the mic speaker script, and the battery driver. Return the guest tools installer name.

	The caller writes ``autounattend.xml`` into ``staging`` before calling ``write_iso_image``.
	"""
	tools_folder = os.path.join(staging, TOOLS_FOLDER)
	os.makedirs(tools_folder, exist_ok=True)
	tag_path = os.path.join(staging, *PAYLOAD_TAG.split("\\"))
	os.makedirs(os.path.dirname(tag_path), exist_ok=True)
	with open(tag_path, "w") as tag:
		tag.write("wn setup payload\n")
	with mounted(virtio_iso) as virtio_mount:
		_stage_drivers(staging, virtio_mount)
	with mounted(guest_tools_iso) as guest_tools_mount:
		installer = _stage_guest_tools(tools_folder, guest_tools_mount)
	for tool in list(extra_tools) + [MIC_SPEAKER_SCRIPT_PATH]:
		shutil.copyfile(tool, os.path.join(tools_folder, os.path.basename(tool)))
	if battery:
		_stage_battery(staging)
	return installer


def write_iso_image(staging, image_path):
	"""Write the contents of ``staging`` to a new ISO 9660 and Joliet image at ``image_path``.

	Windows Setup looks for ``autounattend.xml`` on removable media. UTM attaches a USB disk image as a fixed
	disk, which Setup does not search, so the payload goes on a CD instead.
	"""
	if os.path.exists(image_path):
		raise PayloadError("{} already exists".format(image_path))
	_hdiutil("makehybrid", "-quiet", "-iso", "-joliet", "-default-volume-name", VOLUME_NAME, "-o", image_path, staging)
	return image_path


def build_payload_iso(image_path, unattend_options, virtio_iso, guest_tools_iso, nvda_installer=None, battery=True):
	"""Write the complete setup disc: drivers, installers, and an answer file whose first-logon steps install them."""
	with tempfile.TemporaryDirectory(prefix="wn-payload-") as staging:
		extra_tools = [nvda_installer] if nvda_installer else []
		guest_tools_name = stage_payload(staging, virtio_iso, guest_tools_iso, extra_tools, battery)
		nvda_name = os.path.basename(nvda_installer) if nvda_installer else None
		commands = tuple(setup_commands(guest_tools_name, nvda_name, battery))
		answer_file = build_unattend(dataclasses.replace(unattend_options, first_logon_commands=commands))
		with open(os.path.join(staging, ANSWER_FILE_NAME), "wb") as output:
			output.write(answer_file)
		return write_iso_image(staging, image_path)
