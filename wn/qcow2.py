"""Write an empty qcow2 version 3 disk image without qemu-img."""

import struct

QCOW2_MAGIC = b"QFI\xfb"
QCOW2_VERSION = 3
CLUSTER_BITS = 16
CLUSTER_SIZE = 1 << CLUSTER_BITS
REFCOUNT_ORDER = 4
REFCOUNT_ENTRY_FORMAT = ">H"
TABLE_ENTRY_SIZE = 8
HEADER_LENGTH = 104
HEADER_FORMAT = ">4sIQIIQIIQQIIQQQQII"
REFCOUNT_TABLE_CLUSTER = 1
REFCOUNT_BLOCK_CLUSTER = 2
L1_TABLE_CLUSTER = 3
GIB = 1024 ** 3


def _clusters_for(byte_count):
	return -(-byte_count // CLUSTER_SIZE)


def create_empty_qcow2(path, size_gib):
	"""Create an empty, sparse qcow2 image of ``size_gib`` GiB at ``path``. Refuse to overwrite a file."""
	virtual_size = size_gib * GIB
	bytes_per_l2_table = CLUSTER_SIZE * (CLUSTER_SIZE // TABLE_ENTRY_SIZE)
	l1_entries = -(-virtual_size // bytes_per_l2_table)
	l1_clusters = max(1, _clusters_for(l1_entries * TABLE_ENTRY_SIZE))
	used_clusters = L1_TABLE_CLUSTER + l1_clusters
	header = struct.pack(
		HEADER_FORMAT,
		QCOW2_MAGIC, QCOW2_VERSION,
		0, 0,
		CLUSTER_BITS, virtual_size,
		0,
		l1_entries, L1_TABLE_CLUSTER * CLUSTER_SIZE,
		REFCOUNT_TABLE_CLUSTER * CLUSTER_SIZE, 1,
		0, 0,
		0, 0, 0,
		REFCOUNT_ORDER, HEADER_LENGTH,
	)
	image = bytearray(used_clusters * CLUSTER_SIZE)
	image[:len(header)] = header
	refcount_table = REFCOUNT_TABLE_CLUSTER * CLUSTER_SIZE
	image[refcount_table:refcount_table + TABLE_ENTRY_SIZE] = struct.pack(">Q", REFCOUNT_BLOCK_CLUSTER * CLUSTER_SIZE)
	refcount_block = REFCOUNT_BLOCK_CLUSTER * CLUSTER_SIZE
	entry_size = struct.calcsize(REFCOUNT_ENTRY_FORMAT)
	for cluster in range(used_clusters):
		offset = refcount_block + cluster * entry_size
		image[offset:offset + entry_size] = struct.pack(REFCOUNT_ENTRY_FORMAT, 1)
	with open(path, "xb") as image_file:
		image_file.write(image)
