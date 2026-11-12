import os
import shutil
import subprocess
import tempfile
import unittest

from wn.payload import (
	PayloadError,
	VOLUME_NAME,
	build_payload_iso,
	mounted,
	stage_payload,
	write_iso_image,
)
import xml.etree.ElementTree as ET

from wn.unattend import FIRST_LOGON_DRIVERS, PAYLOAD_TAG, SETUP_DRIVERS, WINPE_DRIVER_FOLDER, UnattendOptions

ISO_SECTOR_SIZE = 2048
ISO_PRIMARY_DESCRIPTOR_OFFSET = 16 * ISO_SECTOR_SIZE
HAS_HDIUTIL = shutil.which("hdiutil") is not None


def write_file(path, content=b"x"):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "wb") as output:
		output.write(content)


def make_iso(folder, iso_path):
	for parent, directories, _ in os.walk(folder):
		for directory in directories:
			os.chmod(os.path.join(parent, directory), 0o555)
	subprocess.run(["hdiutil", "makehybrid", "-quiet", "-udf", "-iso", "-joliet", "-o", iso_path, folder], check=True)
	subprocess.run(["chmod", "-R", "u+w", folder], check=True)


@unittest.skipUnless(HAS_HDIUTIL, "needs macOS hdiutil")
class PayloadImageTests(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		root = self.directory.name
		virtio = os.path.join(root, "virtio-src")
		for driver in FIRST_LOGON_DRIVERS + ("viosock",):
			write_file(os.path.join(virtio, driver, "w11", "ARM64", driver.lower() + ".inf"), b"inf")
			write_file(os.path.join(virtio, driver, "w10", "ARM64", "old.inf"), b"old")
		self.virtio_iso = os.path.join(root, "virtio.iso")
		make_iso(virtio, self.virtio_iso)
		tools = os.path.join(root, "tools-src")
		write_file(os.path.join(tools, "utm-guest-tools-0.1.271.exe"), b"guest tools")
		write_file(os.path.join(tools, "Drivers", "big.sys"), b"ignored")
		self.guest_tools_iso = os.path.join(root, "tools.iso")
		make_iso(tools, self.guest_tools_iso)
		self.nvda = os.path.join(root, "nvda_2026.3.1.exe")
		write_file(self.nvda, b"nvda")
		self.staging = os.path.join(root, "staging")

	def test_stage_layout(self):
		guest_tools_name = stage_payload(self.staging, self.virtio_iso, self.guest_tools_iso, [self.nvda])
		self.assertEqual(guest_tools_name, "utm-guest-tools-0.1.271.exe")
		listing = sorted(os.path.relpath(os.path.join(folder, name), self.staging) for folder, _, names in os.walk(self.staging) for name in names)
		expected = [os.path.join("tools", "nvda_2026.3.1.exe"), os.path.join("tools", "utm-guest-tools-0.1.271.exe"), PAYLOAD_TAG.replace("\\", os.sep)]
		expected += [os.path.join("virtio", driver, "w11", "ARM64", driver.lower() + ".inf") for driver in FIRST_LOGON_DRIVERS]
		expected += [os.path.join(WINPE_DRIVER_FOLDER, driver, driver.lower() + ".inf") for driver in SETUP_DRIVERS]
		self.assertEqual(listing, sorted(expected))

	def test_staging_can_be_deleted(self):
		stage_payload(self.staging, self.virtio_iso, self.guest_tools_iso, [self.nvda])
		shutil.rmtree(self.staging)
		self.assertFalse(os.path.exists(self.staging))

	def test_missing_guest_tools_installer(self):
		with self.assertRaises(PayloadError):
			stage_payload(self.staging, self.virtio_iso, self.virtio_iso, [])

	def test_iso_round_trip(self):
		stage_payload(self.staging, self.virtio_iso, self.guest_tools_iso, [self.nvda])
		write_file(os.path.join(self.staging, "autounattend.xml"), b"<unattend/>")
		image = os.path.join(self.directory.name, "payload.iso")
		write_iso_image(self.staging, image)
		with open(image, "rb") as raw:
			raw.seek(ISO_PRIMARY_DESCRIPTOR_OFFSET)
			descriptor = raw.read(ISO_SECTOR_SIZE)
		self.assertEqual(descriptor[1:6], b"CD001")
		self.assertEqual(descriptor[40:72].rstrip(b"\x00 "), VOLUME_NAME.encode())
		with mounted(image) as mount_point:
			with open(os.path.join(mount_point, "autounattend.xml"), "rb") as answer_file:
				self.assertEqual(answer_file.read(), b"<unattend/>")
			with open(os.path.join(mount_point, "tools", "nvda_2026.3.1.exe"), "rb") as nvda:
				self.assertEqual(nvda.read(), b"nvda")
			self.assertTrue(os.path.isdir(os.path.join(mount_point, WINPE_DRIVER_FOLDER, "NetKVM")))

	def test_build_payload_iso(self):
		image = os.path.join(self.directory.name, "wnsetup.iso")
		build_payload_iso(image, UnattendOptions(username="carter"), self.virtio_iso, self.guest_tools_iso, self.nvda)
		with mounted(image) as mount_point:
			with open(os.path.join(mount_point, "autounattend.xml"), "rb") as answer_file:
				root = ET.fromstring(answer_file.read())
			self.assertTrue(os.path.exists(os.path.join(mount_point, "tools", "nvda_2026.3.1.exe")))
		command_lines = [element.text for element in root.iter("{urn:schemas-microsoft-com:unattend}CommandLine")]
		self.assertTrue(any("utm-guest-tools-0.1.271.exe /S" in line for line in command_lines))
		self.assertTrue(any("nvda_2026.3.1.exe --install-silent" in line for line in command_lines))

	def test_build_payload_iso_without_nvda(self):
		image = os.path.join(self.directory.name, "wnsetup.iso")
		build_payload_iso(image, UnattendOptions(username="carter"), self.virtio_iso, self.guest_tools_iso, None)
		with mounted(image) as mount_point:
			self.assertEqual(os.listdir(os.path.join(mount_point, "tools")), ["utm-guest-tools-0.1.271.exe"])

	def test_refuses_existing_image(self):
		image = os.path.join(self.directory.name, "payload.iso")
		write_file(image)
		os.makedirs(self.staging)
		with self.assertRaises(PayloadError):
			write_iso_image(self.staging, image)


if __name__ == "__main__":
	unittest.main()
