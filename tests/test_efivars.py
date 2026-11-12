import os
import struct
import tempfile
import unittest
import uuid
import zlib

from wn.efivars import (
	AUTHENTICATED_VARIABLE_STORE_GUID,
	DB_ATTRIBUTES,
	EFI_CERT_X509_GUID,
	EFI_GLOBAL_VARIABLE_GUID,
	EFI_IMAGE_SECURITY_DATABASE_GUID,
	NVSTORAGE_FV_GUID,
	VAR_ADDED,
	VAR_DELETED,
	WN_SIGNATURE_OWNER_GUID,
	EfiVarsError,
	encode_variable,
	enroll_db_cert,
	find,
	make_vars_with_cert,
	parse_signature_lists,
	parse_store,
	qcow2_image,
	read_flash,
	set_variable_data,
	signature_list,
)

FV_HEADER_LENGTH = 0x48
STORE_HEADER_LENGTH = 28
FIRST_VARIABLE_OFFSET = 0x64
FV_LENGTH = 0x10000
FLASH_SIZE = 4 * 0x10000
NV_ATTRIBUTES = 0x07
TIMESTAMP = bytes([0xE9, 0x07, 0x08, 0x0B, 0x04, 0x2D, 0x01]) + bytes(9)
MICROSOFT_OWNER = uuid.UUID("77fa9abd-0359-4d32-bd60-28f4e78f784b")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTM_SECURE_VARS = "/Applications/UTM.app/Contents/Resources/qemu/edk2-arm-secure-vars.fd"


def fv_header(fv_length=FV_LENGTH):
	header = bytearray(FV_HEADER_LENGTH)
	header[16:32] = NVSTORAGE_FV_GUID.bytes_le
	struct.pack_into("<Q4sIHHHBB", header, 32, fv_length, b"_FVH", 0x4FEFF, FV_HEADER_LENGTH, 0, 0, 0, 2)
	struct.pack_into("<IIII", header, 56, 1, fv_length, 0, 0)
	return bytes(header)


def store_header(store_size):
	return AUTHENTICATED_VARIABLE_STORE_GUID.bytes_le + struct.pack("<IBBHI", store_size, 0x5A, 0xFE, 0, 0)


def padded(record):
	return record + b"\xff" * (-len(record) % 4)


def synthetic_flash(records):
	store_size = FV_LENGTH - FV_HEADER_LENGTH
	used = fv_header() + store_header(store_size) + b"\xff" * (FIRST_VARIABLE_OFFSET - FV_HEADER_LENGTH - STORE_HEADER_LENGTH)
	used += b"".join(padded(record) for record in records)
	flash = used + b"\xff" * (FV_LENGTH - len(used))
	return flash + bytes(FLASH_SIZE - len(flash))


def x509_list(owner, cert):
	return signature_list(EFI_CERT_X509_GUID, owner, cert)


MICROSOFT_DB = x509_list(MICROSOFT_OWNER, b"microsoft cert") + x509_list(MICROSOFT_OWNER, b"other cert!")


def secure_flash():
	return synthetic_flash([
		encode_variable("Boot0000", EFI_GLOBAL_VARIABLE_GUID, NV_ATTRIBUTES, b"old boot", state=VAR_DELETED),
		encode_variable("db", EFI_IMAGE_SECURITY_DATABASE_GUID, DB_ATTRIBUTES, MICROSOFT_DB, timestamp=TIMESTAMP),
		encode_variable("Boot0000", EFI_GLOBAL_VARIABLE_GUID, NV_ATTRIBUTES, b"new boot"),
	])


class EncodeVariableTests(unittest.TestCase):
	def test_header_layout(self):
		record = encode_variable("db", EFI_IMAGE_SECURITY_DATABASE_GUID, DB_ATTRIBUTES, b"abc", timestamp=TIMESTAMP)
		start_id, state, reserved, attributes, monotonic_count = struct.unpack_from("<HBBIQ", record)
		self.assertEqual((start_id, state, reserved, attributes, monotonic_count), (0x55AA, VAR_ADDED, 0, DB_ATTRIBUTES, 0))
		self.assertEqual(record[16:32], TIMESTAMP)
		self.assertEqual(struct.unpack_from("<III", record, 32), (0, 6, 3))
		self.assertEqual(record[44:60], EFI_IMAGE_SECURITY_DATABASE_GUID.bytes_le)
		self.assertEqual(record[60:], "db\0".encode("utf-16-le") + b"abc")


class ParseStoreTests(unittest.TestCase):
	def test_lists_every_record(self):
		variables = parse_store(secure_flash())
		self.assertEqual([variable.name for variable in variables], ["Boot0000", "db", "Boot0000"])
		self.assertEqual([variable.state for variable in variables], [VAR_DELETED, VAR_ADDED, VAR_ADDED])
		self.assertEqual(variables[1].guid, EFI_IMAGE_SECURITY_DATABASE_GUID)
		self.assertEqual(variables[1].attributes, DB_ATTRIBUTES)
		self.assertEqual(variables[1].timestamp, TIMESTAMP)
		self.assertEqual(variables[1].data, MICROSOFT_DB)
		self.assertEqual(variables[0].offset, FIRST_VARIABLE_OFFSET)

	def test_empty_store(self):
		self.assertEqual(parse_store(synthetic_flash([])), [])

	def test_rejects_other_firmware_volume(self):
		flash = bytearray(secure_flash())
		flash[16:32] = uuid.uuid4().bytes_le
		with self.assertRaises(EfiVarsError):
			parse_store(bytes(flash))

	def test_rejects_unauthenticated_store(self):
		flash = bytearray(secure_flash())
		flash[FV_HEADER_LENGTH:FV_HEADER_LENGTH + 16] = uuid.uuid4().bytes_le
		with self.assertRaises(EfiVarsError):
			parse_store(bytes(flash))


class FindTests(unittest.TestCase):
	def test_skips_deleted_records(self):
		self.assertEqual(find(secure_flash(), "Boot0000", EFI_GLOBAL_VARIABLE_GUID).data, b"new boot")

	def test_needs_matching_guid(self):
		self.assertIsNone(find(secure_flash(), "db", EFI_GLOBAL_VARIABLE_GUID))

	def test_missing_name(self):
		self.assertIsNone(find(secure_flash(), "KEK", EFI_GLOBAL_VARIABLE_GUID))


class SetVariableDataTests(unittest.TestCase):
	def setUp(self):
		self.flash = secure_flash()
		self.old = find(self.flash, "db", EFI_IMAGE_SECURITY_DATABASE_GUID)
		self.updated = set_variable_data(self.flash, "db", EFI_IMAGE_SECURITY_DATABASE_GUID, b"new data")

	def test_size_is_unchanged(self):
		self.assertEqual(len(self.updated), len(self.flash))

	def test_old_record_is_deleted(self):
		self.assertEqual(parse_store(self.updated)[1].state, VAR_DELETED)
		self.assertEqual(parse_store(self.updated)[1].data, MICROSOFT_DB)

	def test_new_record_is_appended(self):
		variables = parse_store(self.updated)
		self.assertEqual(len(variables), 4)
		new = variables[-1]
		self.assertEqual((new.name, new.guid, new.state, new.data), ("db", EFI_IMAGE_SECURITY_DATABASE_GUID, VAR_ADDED, b"new data"))
		self.assertEqual(new.attributes, DB_ATTRIBUTES)
		self.assertEqual(new.timestamp, TIMESTAMP)
		self.assertEqual(new.offset % 4, 0)

	def test_find_returns_the_new_data(self):
		self.assertEqual(find(self.updated, "db", EFI_IMAGE_SECURITY_DATABASE_GUID).data, b"new data")

	def test_only_the_state_byte_and_free_space_change(self):
		end_of_used = parse_store(self.updated)[-1].offset
		changed = [index for index in range(end_of_used) if self.updated[index] != self.flash[index]]
		self.assertEqual(changed, [self.old.offset + 2])
		self.assertEqual(self.updated[FV_LENGTH:], self.flash[FV_LENGTH:])

	def test_missing_variable_needs_attributes(self):
		with self.assertRaises(EfiVarsError):
			set_variable_data(self.flash, "KEK", EFI_GLOBAL_VARIABLE_GUID, b"kek")

	def test_missing_variable_is_added(self):
		updated = set_variable_data(self.flash, "KEK", EFI_GLOBAL_VARIABLE_GUID, b"kek", attributes=DB_ATTRIBUTES)
		variable = find(updated, "KEK", EFI_GLOBAL_VARIABLE_GUID)
		self.assertEqual((variable.data, variable.attributes, variable.timestamp), (b"kek", DB_ATTRIBUTES, bytes(16)))
		self.assertEqual(len(parse_store(updated)), 4)

	def test_refuses_data_that_does_not_fit(self):
		with self.assertRaises(EfiVarsError):
			set_variable_data(self.flash, "db", EFI_IMAGE_SECURITY_DATABASE_GUID, bytes(FV_LENGTH))


class SignatureListTests(unittest.TestCase):
	def test_layout(self):
		owner = uuid.UUID("499a14e3-3137-4091-89f0-a878e452d314")
		data = signature_list(EFI_CERT_X509_GUID, owner, b"cert")
		self.assertEqual(data[:16], EFI_CERT_X509_GUID.bytes_le)
		self.assertEqual(struct.unpack_from("<III", data, 16), (28 + 16 + 4, 0, 16 + 4))
		self.assertEqual(data[28:44], owner.bytes_le)
		self.assertEqual(data[44:], b"cert")

	def test_parse(self):
		lists = parse_signature_lists(MICROSOFT_DB)
		self.assertEqual(lists, [
			(EFI_CERT_X509_GUID, [(MICROSOFT_OWNER, b"microsoft cert")]),
			(EFI_CERT_X509_GUID, [(MICROSOFT_OWNER, b"other cert!")]),
		])

	def test_parse_rejects_truncated_list(self):
		with self.assertRaises(EfiVarsError):
			parse_signature_lists(MICROSOFT_DB[:-1])


class EnrollTests(unittest.TestCase):
	def test_appends_one_x509_list_to_db(self):
		updated = enroll_db_cert(secure_flash(), b"our cert", WN_SIGNATURE_OWNER_GUID)
		db = find(updated, "db", EFI_IMAGE_SECURITY_DATABASE_GUID)
		self.assertEqual(db.data, MICROSOFT_DB + x509_list(WN_SIGNATURE_OWNER_GUID, b"our cert"))
		self.assertEqual(db.attributes, DB_ATTRIBUTES)

	def test_creates_db_in_a_blank_store(self):
		updated = enroll_db_cert(synthetic_flash([]), b"our cert", WN_SIGNATURE_OWNER_GUID)
		db = find(updated, "db", EFI_IMAGE_SECURITY_DATABASE_GUID)
		self.assertEqual(db.data, x509_list(WN_SIGNATURE_OWNER_GUID, b"our cert"))
		self.assertEqual(db.attributes, DB_ATTRIBUTES)


class Qcow2Tests(unittest.TestCase):
	def test_round_trip(self):
		flash = secure_flash()
		image = qcow2_image(flash)
		self.assertEqual(image[:4], b"QFI\xfb")
		self.assertEqual(struct.unpack_from(">Q", image, 24)[0], len(flash))
		self.assertEqual(read_with_temp_file(image), flash)

	def test_skips_zero_clusters(self):
		flash = secure_flash().ljust(64 << 20, b"\0")
		self.assertEqual(len(qcow2_image(flash)), 6 * 0x10000)

	def test_reads_raw_files(self):
		self.assertEqual(read_with_temp_file(secure_flash()), secure_flash())

	def test_reads_compressed_clusters(self):
		flash = secure_flash()
		image = bytearray(qcow2_image(flash))
		l1_offset = struct.unpack_from(">Q", image, 40)[0]
		l2_offset = struct.unpack_from(">Q", image, l1_offset)[0] & 0x00FFFFFFFFFFFE00
		compressor = zlib.compressobj(9, zlib.DEFLATED, -12)
		compressed = compressor.compress(flash[:0x10000]) + compressor.flush()
		host_offset = len(image)
		image += compressed + bytes(-len(compressed) % 512)
		extra_sectors = (len(compressed) + 511) // 512 - 1
		struct.pack_into(">Q", image, l2_offset, (1 << 62) | (extra_sectors << 54) | host_offset)
		self.assertEqual(read_with_temp_file(bytes(image)), flash)

	def test_rejects_backing_files(self):
		image = bytearray(qcow2_image(secure_flash()))
		struct.pack_into(">Q", image, 8, 0x200)
		with self.assertRaises(EfiVarsError):
			read_with_temp_file(bytes(image))


def read_with_temp_file(content):
	with tempfile.TemporaryDirectory() as directory:
		path = os.path.join(directory, "vars.fd")
		with open(path, "wb") as image_file:
			image_file.write(content)
		return read_flash(path)


class MakeVarsTests(unittest.TestCase):
	def setUp(self):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		self.directory = directory.name
		self.template = os.path.join(self.directory, "template.fd")
		self.cert = os.path.join(self.directory, "wn.cer")
		self.out = os.path.join(self.directory, "efi_vars.fd")
		with open(self.template, "wb") as template_file:
			template_file.write(qcow2_image(secure_flash()))
		with open(self.cert, "wb") as cert_file:
			cert_file.write(b"our cert")

	def test_writes_qcow2_with_the_cert(self):
		make_vars_with_cert(self.template, self.cert, self.out)
		with open(self.out, "rb") as out_file:
			self.assertEqual(out_file.read(4), b"QFI\xfb")
		db = find(read_flash(self.out), "db", EFI_IMAGE_SECURITY_DATABASE_GUID)
		self.assertEqual(parse_signature_lists(db.data)[-1], (EFI_CERT_X509_GUID, [(WN_SIGNATURE_OWNER_GUID, b"our cert")]))

	def test_refuses_to_overwrite(self):
		open(self.out, "w").close()
		with self.assertRaises(FileExistsError):
			make_vars_with_cert(self.template, self.cert, self.out)


@unittest.skipUnless(os.path.exists(UTM_SECURE_VARS), "UTM is not installed")
class UtmTemplateTests(unittest.TestCase):
	def test_template_has_microsoft_keys(self):
		flash = read_flash(UTM_SECURE_VARS)
		for name, guid in (("PK", EFI_GLOBAL_VARIABLE_GUID), ("KEK", EFI_GLOBAL_VARIABLE_GUID), ("db", EFI_IMAGE_SECURITY_DATABASE_GUID)):
			self.assertIsNotNone(find(flash, name, guid), name)

	def test_enroll_into_template(self):
		flash = read_flash(UTM_SECURE_VARS)
		with open(os.path.join(REPO, "efi", "dist", "wn-sb.cer"), "rb") as cert_file:
			cert = cert_file.read()
		before = find(flash, "db", EFI_IMAGE_SECURITY_DATABASE_GUID).data
		after = find(enroll_db_cert(flash, cert, WN_SIGNATURE_OWNER_GUID), "db", EFI_IMAGE_SECURITY_DATABASE_GUID).data
		self.assertEqual(after, before + x509_list(WN_SIGNATURE_OWNER_GUID, cert))


if __name__ == "__main__":
	unittest.main()
