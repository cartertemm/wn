"""Compute the Authenticode SHA-256 digest of a PE/COFF image, the hash that UEFI Secure Boot looks up in ``db``.

The digest follows the steps in EDK2's ``HashPeImage``: it covers the headers without the checksum and the
certificate table entry, then each section in file order, then any data after the sections other than the
attached signatures. So a signed and an unsigned copy of the same build have the same digest.
"""

import hashlib
import struct

from wn.errors import WnError

DOS_MAGIC = b"MZ"
PE_SIGNATURE = b"PE\0\0"
PE_HEADER_OFFSET_POSITION = 0x3C
COFF_HEADER_FORMAT = "<HHIIIHH"
COFF_HEADER_SIZE = struct.calcsize(COFF_HEADER_FORMAT)
PE32_MAGIC = 0x10B
PE32_PLUS_MAGIC = 0x20B
CHECKSUM_OFFSET = 64
SIZE_OF_HEADERS_OFFSET = 60
NUMBER_OF_RVA_AND_SIZES_OFFSET = {PE32_MAGIC: 92, PE32_PLUS_MAGIC: 108}
DATA_DIRECTORY_ENTRY_SIZE = 8
SECURITY_DIRECTORY_INDEX = 4
SECTION_HEADER_SIZE = 40
SECTION_RAW_SIZE_OFFSET = 16


class AuthenticodeError(WnError):
	"""Raised when a file is not a PE/COFF image that this module can hash."""


def authenticode_sha256(image):
	"""Return the 32-byte Authenticode SHA-256 digest of the PE/COFF ``image`` bytes."""
	if image[:2] != DOS_MAGIC or len(image) < PE_HEADER_OFFSET_POSITION + 4:
		raise AuthenticodeError("the file is not a PE/COFF image")
	pe_offset = struct.unpack_from("<I", image, PE_HEADER_OFFSET_POSITION)[0]
	if image[pe_offset:pe_offset + 4] != PE_SIGNATURE:
		raise AuthenticodeError("the file has no PE signature")
	coff = pe_offset + 4
	_, section_count, _, _, _, optional_size, _ = struct.unpack_from(COFF_HEADER_FORMAT, image, coff)
	optional = coff + COFF_HEADER_SIZE
	magic = struct.unpack_from("<H", image, optional)[0]
	if magic not in NUMBER_OF_RVA_AND_SIZES_OFFSET:
		raise AuthenticodeError("unknown optional header magic {:#x}".format(magic))
	checksum = optional + CHECKSUM_OFFSET
	headers_size = struct.unpack_from("<I", image, optional + SIZE_OF_HEADERS_OFFSET)[0]
	directory_count_offset = optional + NUMBER_OF_RVA_AND_SIZES_OFFSET[magic]
	directory_count = struct.unpack_from("<I", image, directory_count_offset)[0]
	digest = hashlib.sha256(image[:checksum])
	cert_size = 0
	if directory_count > SECURITY_DIRECTORY_INDEX:
		security = directory_count_offset + 4 + SECURITY_DIRECTORY_INDEX * DATA_DIRECTORY_ENTRY_SIZE
		cert_size = struct.unpack_from("<I", image, security + 4)[0]
		digest.update(image[checksum + 4:security])
		digest.update(image[security + DATA_DIRECTORY_ENTRY_SIZE:headers_size])
	else:
		digest.update(image[checksum + 4:headers_size])
	sections = []
	for index in range(section_count):
		header = optional + optional_size + index * SECTION_HEADER_SIZE
		raw_size, raw_offset = struct.unpack_from("<II", image, header + SECTION_RAW_SIZE_OFFSET)
		if raw_size:
			sections.append((raw_offset, raw_size))
	hashed = headers_size
	for raw_offset, raw_size in sorted(sections):
		digest.update(image[raw_offset:raw_offset + raw_size])
		hashed += raw_size
	if len(image) > hashed + cert_size:
		digest.update(image[hashed:len(image) - cert_size])
	return digest.digest()


def file_authenticode_sha256(path):
	"""Return the Authenticode SHA-256 digest of the PE/COFF file at ``path``."""
	with open(path, "rb") as image_file:
		return authenticode_sha256(image_file.read())
