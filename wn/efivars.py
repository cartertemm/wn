"""Read and edit an EDK2 UEFI variable store, and enroll a certificate in the Secure Boot ``db``.

The store is the pflash image that UTM keeps as ``Data/efi_vars.fd``: a qcow2 file (UTM's templates
use compressed clusters) holding a 64 MiB flash image. The flash starts with the NvStorage firmware
volume header, then the authenticated variable store header, then the variable records.
"""

import struct
import uuid
import zlib

from wn.errors import WnError
from wn.qcow2 import CLUSTER_BITS, CLUSTER_SIZE, HEADER_FORMAT, HEADER_LENGTH, QCOW2_MAGIC, QCOW2_VERSION, REFCOUNT_ORDER

NVSTORAGE_FV_GUID = uuid.UUID("fff12b8d-7696-4c8b-a985-2747075b4f50")
AUTHENTICATED_VARIABLE_STORE_GUID = uuid.UUID("aaf32c78-947b-439a-a180-2e144ec37792")
EFI_GLOBAL_VARIABLE_GUID = uuid.UUID("8be4df61-93ca-11d2-aa0d-00e098032b8c")
EFI_IMAGE_SECURITY_DATABASE_GUID = uuid.UUID("d719b2cb-3d3a-4596-a3bc-dad00e67656f")
EFI_CERT_X509_GUID = uuid.UUID("a5c059a1-94e4-4aa7-87b5-ab155c2bf072")
WN_SIGNATURE_OWNER_GUID = uuid.UUID("499a14e3-3137-4091-89f0-a878e452d314")

FV_SIGNATURE = b"_FVH"
FV_HEADER_FORMAT = "<16s16sQ4sIHHHBB"
STORE_HEADER_FORMAT = "<16sIBBHI"
STORE_FORMATTED = 0x5A
STORE_HEALTHY = 0xFE
VARIABLE_HEADER_FORMAT = "<HBBIQ16sIII16s"
VARIABLE_HEADER_SIZE = struct.calcsize(VARIABLE_HEADER_FORMAT)
VARIABLE_START_ID = 0x55AA
VARIABLE_ALIGNMENT = 4
VAR_ADDED = 0x3F
VAR_IN_DELETED_TRANSITION = 0xFE
VAR_DELETED_FLAG = 0xFD
VAR_DELETED = VAR_ADDED & VAR_IN_DELETED_TRANSITION & VAR_DELETED_FLAG
EMPTY_TIMESTAMP = bytes(16)

EFI_VARIABLE_NON_VOLATILE = 0x01
EFI_VARIABLE_BOOTSERVICE_ACCESS = 0x02
EFI_VARIABLE_RUNTIME_ACCESS = 0x04
EFI_VARIABLE_TIME_BASED_AUTHENTICATED_WRITE_ACCESS = 0x20
DB_ATTRIBUTES = (EFI_VARIABLE_NON_VOLATILE | EFI_VARIABLE_BOOTSERVICE_ACCESS | EFI_VARIABLE_RUNTIME_ACCESS
	| EFI_VARIABLE_TIME_BASED_AUTHENTICATED_WRITE_ACCESS)

SIGNATURE_LIST_HEADER_FORMAT = "<16sIII"
SIGNATURE_LIST_HEADER_SIZE = struct.calcsize(SIGNATURE_LIST_HEADER_FORMAT)
SIGNATURE_OWNER_SIZE = 16

QCOW2_COMPRESSED_FLAG = 1 << 62
QCOW2_COPIED_FLAG = 1 << 63
QCOW2_ZERO_FLAG = 1
QCOW2_OFFSET_MASK = 0x00FFFFFFFFFFFE00
QCOW2_SECTOR_SIZE = 512
QCOW2_TABLE_ENTRY_SIZE = 8
QCOW2_COMPRESSION_TYPE_OFFSET = 104
RAW_DEFLATE_WINDOW_BITS = -15


class EfiVarsError(WnError):
	"""Raised when a file is not an EDK2 variable store that this module can edit."""


class Variable:
	"""One variable record. ``offset`` is the record's position in the flash image."""

	def __init__(self, offset, state, attributes, monotonic_count, timestamp, pubkey_index, guid, name, data):
		self.offset = offset
		self.state = state
		self.attributes = attributes
		self.monotonic_count = monotonic_count
		self.timestamp = timestamp
		self.pubkey_index = pubkey_index
		self.guid = guid
		self.name = name
		self.data = data

	def __repr__(self):
		return "Variable({!r}, {}, state={:#04x}, {} bytes)".format(self.name, self.guid, self.state, len(self.data))


def _align(value):
	return value + (-value % VARIABLE_ALIGNMENT)


def encode_variable(name, guid, attributes, data, state=VAR_ADDED, timestamp=EMPTY_TIMESTAMP, monotonic_count=0, pubkey_index=0):
	"""Return one AUTHENTICATED_VARIABLE_HEADER record with its name and data, without alignment padding."""
	encoded_name = (name + "\0").encode("utf-16-le")
	header = struct.pack(VARIABLE_HEADER_FORMAT, VARIABLE_START_ID, state, 0, attributes, monotonic_count, timestamp,
		pubkey_index, len(encoded_name), len(data), guid.bytes_le)
	return header + encoded_name + data


def _store_bounds(flash):
	"""Return the offset of the first record and the end of the variable store."""
	if len(flash) < struct.calcsize(FV_HEADER_FORMAT):
		raise EfiVarsError("the file is too small to be a UEFI variable store")
	_, fv_guid, _, signature, _, fv_header_length, _, _, _, _ = struct.unpack_from(FV_HEADER_FORMAT, flash)
	if signature != FV_SIGNATURE or uuid.UUID(bytes_le=fv_guid) != NVSTORAGE_FV_GUID:
		raise EfiVarsError("the file does not start with an EDK2 NvStorage firmware volume")
	store_guid, store_size, store_format, store_state, _, _ = struct.unpack_from(STORE_HEADER_FORMAT, flash, fv_header_length)
	if uuid.UUID(bytes_le=store_guid) != AUTHENTICATED_VARIABLE_STORE_GUID:
		raise EfiVarsError("the variable store is not an authenticated variable store")
	if store_format != STORE_FORMATTED or store_state != STORE_HEALTHY:
		raise EfiVarsError("the variable store is not formatted and healthy")
	first = _align(fv_header_length + struct.calcsize(STORE_HEADER_FORMAT))
	return first, fv_header_length + store_size


def _records(flash):
	first, end = _store_bounds(flash)
	offset = first
	variables = []
	while offset + VARIABLE_HEADER_SIZE <= end:
		(start_id, state, _, attributes, monotonic_count, timestamp, pubkey_index, name_size, data_size,
			guid) = struct.unpack_from(VARIABLE_HEADER_FORMAT, flash, offset)
		if start_id != VARIABLE_START_ID:
			break
		name_start = offset + VARIABLE_HEADER_SIZE
		data_start = name_start + name_size
		if data_start + data_size > end:
			raise EfiVarsError("variable record at {:#x} runs past the end of the store".format(offset))
		name = flash[name_start:data_start].decode("utf-16-le").rstrip("\0")
		variables.append(Variable(offset, state, attributes, monotonic_count, timestamp, pubkey_index,
			uuid.UUID(bytes_le=guid), name, flash[data_start:data_start + data_size]))
		offset = _align(data_start + data_size)
	return variables, offset, end


def parse_store(flash):
	"""Return every variable record in the flash image, deleted ones included, in store order."""
	return _records(flash)[0]


def find(flash, name, guid):
	"""Return the live (VAR_ADDED) record of the variable ``name`` in vendor ``guid``, or None."""
	for variable in reversed(parse_store(flash)):
		if variable.name == name and variable.guid == guid and variable.state == VAR_ADDED:
			return variable
	return None


def set_variable_data(flash, name, guid, data, attributes=None):
	"""Return a copy of ``flash`` where the variable has ``data``, the way EDK2 updates a variable.

	The old record is marked deleted and a new record goes at the end of the used area, with the old
	record's attributes and timestamp. A missing variable is added with ``attributes`` and a zero timestamp.
	"""
	variables, free, end = _records(flash)
	old = find(flash, name, guid)
	if old is None and attributes is None:
		raise EfiVarsError("variable {} does not exist, so it needs attributes".format(name))
	if old is None:
		record = encode_variable(name, guid, attributes, data)
	else:
		record = encode_variable(name, guid, old.attributes, data, timestamp=old.timestamp,
			monotonic_count=old.monotonic_count, pubkey_index=old.pubkey_index)
	if free + len(record) > end:
		raise EfiVarsError("the variable store has no room for {} bytes of {}".format(len(data), name))
	updated = bytearray(flash)
	if old is not None:
		updated[old.offset + 2] = VAR_DELETED
	updated[free:free + len(record)] = record
	return bytes(updated)


def signature_list(signature_type, owner, signature):
	"""Return one EFI_SIGNATURE_LIST with one EFI_SIGNATURE_DATA entry."""
	signature_size = SIGNATURE_OWNER_SIZE + len(signature)
	header = struct.pack(SIGNATURE_LIST_HEADER_FORMAT, signature_type.bytes_le,
		SIGNATURE_LIST_HEADER_SIZE + signature_size, 0, signature_size)
	return header + owner.bytes_le + signature


def parse_signature_lists(data):
	"""Return ``[(signature_type, [(owner, signature), ...]), ...]`` for a ``db``-style variable."""
	lists = []
	offset = 0
	while offset < len(data):
		if offset + SIGNATURE_LIST_HEADER_SIZE > len(data):
			raise EfiVarsError("truncated EFI_SIGNATURE_LIST header at {:#x}".format(offset))
		signature_type, list_size, header_size, signature_size = struct.unpack_from(SIGNATURE_LIST_HEADER_FORMAT, data, offset)
		body = offset + SIGNATURE_LIST_HEADER_SIZE + header_size
		if offset + list_size > len(data) or signature_size < SIGNATURE_OWNER_SIZE or (offset + list_size - body) % signature_size:
			raise EfiVarsError("invalid EFI_SIGNATURE_LIST at {:#x}".format(offset))
		signatures = []
		for start in range(body, offset + list_size, signature_size):
			owner = uuid.UUID(bytes_le=data[start:start + SIGNATURE_OWNER_SIZE])
			signatures.append((owner, data[start + SIGNATURE_OWNER_SIZE:start + signature_size]))
		lists.append((uuid.UUID(bytes_le=signature_type), signatures))
		offset += list_size
	return lists


def enroll_db_cert(flash, cert_der, owner_guid=WN_SIGNATURE_OWNER_GUID):
	"""Return a copy of ``flash`` whose ``db`` ends with one X.509 signature list holding ``cert_der``."""
	db = find(flash, "db", EFI_IMAGE_SECURITY_DATABASE_GUID)
	existing = db.data if db is not None else b""
	new_list = signature_list(EFI_CERT_X509_GUID, owner_guid, cert_der)
	return set_variable_data(flash, "db", EFI_IMAGE_SECURITY_DATABASE_GUID, existing + new_list, attributes=DB_ATTRIBUTES)


def _qcow2_cluster(image, descriptor, cluster_bits):
	cluster_size = 1 << cluster_bits
	if descriptor & QCOW2_COMPRESSED_FLAG:
		offset_bits = 62 - (cluster_bits - 8)
		host_offset = descriptor & ((1 << offset_bits) - 1)
		extra_sectors = (descriptor >> offset_bits) & ((1 << (62 - offset_bits)) - 1)
		end = (host_offset & ~(QCOW2_SECTOR_SIZE - 1)) + (extra_sectors + 1) * QCOW2_SECTOR_SIZE
		try:
			return zlib.decompressobj(RAW_DEFLATE_WINDOW_BITS).decompress(image[host_offset:end], cluster_size)
		except zlib.error as error:
			raise EfiVarsError("cannot decompress a qcow2 cluster: {}".format(error)) from None
	host_offset = descriptor & QCOW2_OFFSET_MASK
	if descriptor & QCOW2_ZERO_FLAG or not host_offset:
		return b""
	return image[host_offset:host_offset + cluster_size]


def _flash_from_qcow2(image):
	(_, version, backing_offset, _, cluster_bits, virtual_size, encryption, l1_entries, l1_offset, _, _, _, _,
		incompatible, _, _, _, header_length) = struct.unpack_from(HEADER_FORMAT, image)
	if version not in (2, QCOW2_VERSION) or backing_offset or encryption or incompatible:
		raise EfiVarsError("unsupported qcow2 file: backing files, encryption, and incompatible features are not handled")
	if header_length > QCOW2_COMPRESSION_TYPE_OFFSET and image[QCOW2_COMPRESSION_TYPE_OFFSET]:
		raise EfiVarsError("unsupported qcow2 compression type {}".format(image[QCOW2_COMPRESSION_TYPE_OFFSET]))
	cluster_size = 1 << cluster_bits
	l2_entries = cluster_size // QCOW2_TABLE_ENTRY_SIZE
	flash = bytearray(virtual_size)
	for l1_index in range(l1_entries):
		l2_offset = struct.unpack_from(">Q", image, l1_offset + l1_index * QCOW2_TABLE_ENTRY_SIZE)[0] & QCOW2_OFFSET_MASK
		if not l2_offset:
			continue
		for l2_index in range(l2_entries):
			guest_offset = (l1_index * l2_entries + l2_index) * cluster_size
			if guest_offset >= virtual_size:
				break
			descriptor = struct.unpack_from(">Q", image, l2_offset + l2_index * QCOW2_TABLE_ENTRY_SIZE)[0]
			cluster = _qcow2_cluster(image, descriptor, cluster_bits)
			flash[guest_offset:guest_offset + len(cluster)] = cluster
	return bytes(flash)


def read_flash(path):
	"""Return the flash image in ``path``, a qcow2 file or a raw image."""
	with open(path, "rb") as image_file:
		image = image_file.read()
	if image[:len(QCOW2_MAGIC)] == QCOW2_MAGIC:
		return _flash_from_qcow2(image)
	return image


def qcow2_image(flash):
	"""Return a qcow2 version 3 file that holds ``flash``, with one uncompressed cluster per non-zero cluster."""
	l2_entries = CLUSTER_SIZE // QCOW2_TABLE_ENTRY_SIZE
	cluster_count = -(-len(flash) // CLUSTER_SIZE)
	l1_entries = -(-cluster_count // l2_entries)
	data_clusters = [index for index in range(cluster_count) if any(flash[index * CLUSTER_SIZE:(index + 1) * CLUSTER_SIZE])]
	refcount_table, refcount_block, l1_table = 1, 2, 3
	first_l2 = l1_table + 1
	first_data = first_l2 + l1_entries
	used_clusters = first_data + len(data_clusters)
	image = bytearray(used_clusters * CLUSTER_SIZE)
	struct.pack_into(HEADER_FORMAT, image, 0, QCOW2_MAGIC, QCOW2_VERSION, 0, 0, CLUSTER_BITS, len(flash), 0,
		l1_entries, l1_table * CLUSTER_SIZE, refcount_table * CLUSTER_SIZE, 1, 0, 0, 0, 0, 0, REFCOUNT_ORDER, HEADER_LENGTH)
	struct.pack_into(">Q", image, refcount_table * CLUSTER_SIZE, refcount_block * CLUSTER_SIZE)
	for cluster in range(used_clusters):
		struct.pack_into(">H", image, refcount_block * CLUSTER_SIZE + cluster * 2, 1)
	for l1_index in range(l1_entries):
		l2_offset = (first_l2 + l1_index) * CLUSTER_SIZE
		struct.pack_into(">Q", image, l1_table * CLUSTER_SIZE + l1_index * QCOW2_TABLE_ENTRY_SIZE, QCOW2_COPIED_FLAG | l2_offset)
	for position, guest_cluster in enumerate(data_clusters):
		host_offset = (first_data + position) * CLUSTER_SIZE
		l2_offset = (first_l2 + guest_cluster // l2_entries) * CLUSTER_SIZE
		struct.pack_into(">Q", image, l2_offset + (guest_cluster % l2_entries) * QCOW2_TABLE_ENTRY_SIZE, QCOW2_COPIED_FLAG | host_offset)
		image[host_offset:host_offset + CLUSTER_SIZE] = flash[guest_cluster * CLUSTER_SIZE:(guest_cluster + 1) * CLUSTER_SIZE].ljust(CLUSTER_SIZE, b"\0")
	return bytes(image)


def make_vars_with_cert(template_path, cert_path, out_path, owner_guid=WN_SIGNATURE_OWNER_GUID):
	"""Write ``out_path`` as a qcow2 copy of the template with the DER certificate added to ``db``. Refuse to overwrite."""
	with open(cert_path, "rb") as cert_file:
		cert = cert_file.read()
	flash = enroll_db_cert(read_flash(template_path), cert, owner_guid)
	with open(out_path, "xb") as out_file:
		out_file.write(qcow2_image(flash))
