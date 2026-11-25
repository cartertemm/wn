import os
import struct
import unittest

from wn.authenticode import AuthenticodeError, authenticode_sha256

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(REPO, "wn", "efi", "wn-acpi.efi")
FAKE_SIGNATURE = b"\x10\x00\x00\x00\x00\x02\x02\x00fake signature"


def read_driver():
	with open(DRIVER, "rb") as driver_file:
		return driver_file.read()


def header_offsets(image):
	optional = struct.unpack_from("<I", image, 0x3C)[0] + 4 + 20
	checksum = optional + 64
	security = optional + 112 + 4 * 8
	return checksum, security


def with_signature(image):
	"""Return ``image`` as a signing tool leaves it: a new checksum and a certificate table at the end."""
	checksum, security = header_offsets(image)
	signed = bytearray(image + FAKE_SIGNATURE)
	struct.pack_into("<I", signed, checksum, 0x12345678)
	struct.pack_into("<II", signed, security, len(image), len(FAKE_SIGNATURE))
	return bytes(signed)


class AuthenticodeTests(unittest.TestCase):
	def test_digest_is_32_bytes(self):
		self.assertEqual(len(authenticode_sha256(read_driver())), 32)

	def test_signing_does_not_change_the_digest(self):
		image = read_driver()
		self.assertEqual(authenticode_sha256(with_signature(image)), authenticode_sha256(image))

	def test_changed_code_changes_the_digest(self):
		image = bytearray(read_driver())
		image[-1] ^= 0xFF
		self.assertNotEqual(authenticode_sha256(bytes(image)), authenticode_sha256(read_driver()))

	def test_rejects_a_file_that_is_not_pe(self):
		with self.assertRaises(AuthenticodeError):
			authenticode_sha256(b"not a driver")


if __name__ == "__main__":
	unittest.main()
