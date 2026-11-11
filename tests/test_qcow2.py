import os
import shutil
import struct
import subprocess
import tempfile
import unittest

from wn.qcow2 import QCOW2_MAGIC, create_empty_qcow2

GIB = 1024 ** 3
HOMEBREW_QEMU_IMG = "/opt/homebrew/bin/qemu-img"


class Qcow2Tests(unittest.TestCase):
	def setUp(self):
		self.directory = tempfile.TemporaryDirectory()
		self.addCleanup(self.directory.cleanup)
		self.path = os.path.join(self.directory.name, "disk.qcow2")

	def test_header(self):
		create_empty_qcow2(self.path, 64)
		with open(self.path, "rb") as image:
			header = image.read(32)
		self.assertEqual(header[:4], QCOW2_MAGIC)
		version, = struct.unpack(">I", header[4:8])
		size, = struct.unpack(">Q", header[24:32])
		self.assertEqual(version, 3)
		self.assertEqual(size, 64 * GIB)

	def test_file_is_small(self):
		create_empty_qcow2(self.path, 64)
		self.assertLess(os.path.getsize(self.path), 1024 * 1024)

	def test_refuses_existing_file(self):
		open(self.path, "w").close()
		with self.assertRaises(FileExistsError):
			create_empty_qcow2(self.path, 1)

	@unittest.skipUnless(os.path.exists(HOMEBREW_QEMU_IMG) or shutil.which("qemu-img"), "qemu-img is not installed")
	def test_qemu_img_accepts_it(self):
		qemu_img = HOMEBREW_QEMU_IMG if os.path.exists(HOMEBREW_QEMU_IMG) else shutil.which("qemu-img")
		for size_gib in (1, 64, 500):
			path = os.path.join(self.directory.name, "{}.qcow2".format(size_gib))
			create_empty_qcow2(path, size_gib)
			check = subprocess.run([qemu_img, "check", path], capture_output=True, text=True)
			self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
			info = subprocess.run([qemu_img, "info", "--output=json", path], capture_output=True, text=True)
			self.assertIn('"virtual-size": {}'.format(size_gib * GIB), info.stdout)
			self.assertIn('"format": "qcow2"', info.stdout)


if __name__ == "__main__":
	unittest.main()
