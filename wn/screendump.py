"""Take a QMP screendump under UTM and judge whether the guest has drawn something on screen."""

import os
import re
import shutil

UTM_GROUP_CONTAINER = os.path.join("~", "Library", "Group Containers", "WDNLXAD4W8.com.utmapp.UTM")
PPM_HEADER_PATTERN = re.compile(rb"P6\s+(\d+)\s+(\d+)\s+\d+\s")
BYTES_PER_PIXEL = 3
SAMPLE_STEP_PIXELS = 7
DRAWN_MINIMUM_COLORS = 200


def take_screendump(client, destination, group_container=UTM_GROUP_CONTAINER):
	"""Save the guest screen as a PPM file at ``destination``.

	UTM's sandbox lets QEMU write only into its working directory, the UTM group container, so QEMU gets a
	bare file name and the file is moved out afterwards.
	"""
	file_name = "wn-screendump-{}.ppm".format(os.getpid())
	client.execute("screendump", {"filename": file_name})
	shutil.move(os.path.join(os.path.expanduser(group_container), file_name), destination)
	return destination


def _ppm_pixels(data):
	header = PPM_HEADER_PATTERN.match(data)
	if header is None:
		raise ValueError("not a binary PPM image")
	return int(header.group(1)), int(header.group(2)), data[header.end():]


def distinct_colors(ppm_data):
	"""Count the distinct colors in a sample of the pixels of a binary PPM image."""
	width, height, pixels = _ppm_pixels(ppm_data)
	pixel_count = min(width * height, len(pixels) // BYTES_PER_PIXEL)
	colors = set()
	for index in range(0, pixel_count, SAMPLE_STEP_PIXELS):
		offset = index * BYTES_PER_PIXEL
		colors.add(pixels[offset:offset + BYTES_PER_PIXEL])
	return len(colors)


def looks_drawn(ppm_data):
	"""True when the screen shows a picture, such as the Windows desktop, and not a blank or text-only screen."""
	return distinct_colors(ppm_data) >= DRAWN_MINIMUM_COLORS
