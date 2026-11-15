import glob
import os
import subprocess
import tempfile
import unittest

from fakes import clang_works
from wn.appbundle import CAMERA_DIRECTORY, CLANG_PATH

TESTS_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
UNIT_SOURCE = os.path.join(TESTS_DIRECTORY, "camera_units.c")


@unittest.skipUnless(clang_works(), "clang is not available")
class CameraUnitTests(unittest.TestCase):
	def test_c_units(self):
		with tempfile.TemporaryDirectory() as directory:
			binary = os.path.join(directory, "camera_units")
			sources = sorted(glob.glob(os.path.join(CAMERA_DIRECTORY, "*.c")))
			build = subprocess.run([CLANG_PATH, "-std=c11", "-Wall", "-Werror", "-I", CAMERA_DIRECTORY, "-o", binary, UNIT_SOURCE] + sources, capture_output=True, text=True)
			self.assertEqual(build.returncode, 0, build.stderr)
			result = subprocess.run([binary], capture_output=True, text=True, timeout=60)
		self.assertEqual(result.returncode, 0, result.stderr)
		self.assertEqual(result.stdout, "ok\n")


if __name__ == "__main__":
	unittest.main()
