"""Take a QMP screendump under UTM, save it as a PNG screenshot, and judge whether the guest has drawn something on screen."""

import os
import re
import shutil
import subprocess
import tempfile

from wn.errors import WnError
from wn.utm_config import UTM_GROUP_CONTAINER

PPM_HEADER_PATTERN = re.compile(rb"P6\s+(\d+)\s+(\d+)\s+\d+\s")
BYTES_PER_PIXEL = 3
SAMPLE_STEP_PIXELS = 7
DRAWN_MINIMUM_COLORS = 200
SCREENSHOT_FORMAT = "png"
SCREENDUMP_FILE_NAME = "screen.ppm"
SCREENSHOT_DIRECTORY_PREFIX = "wn-screenshot-"


class ScreenshotError(WnError):
	"""Raised when the screen capture cannot be read or converted to a PNG file."""


def take_screendump(client, destination, group_container=UTM_GROUP_CONTAINER):
	"""Save the guest screen as a PPM file at ``destination``.

	UTM's sandbox lets QEMU write only into its working directory, the UTM group container, so QEMU gets a
	bare file name and the file is moved out afterwards.
	"""
	file_name = "wn-screendump-{}.ppm".format(os.getpid())
	client.execute("screendump", {"filename": file_name})
	shutil.move(os.path.join(os.path.expanduser(group_container), file_name), destination)
	return destination


def _convert_to_png(ppm_path, output_path, run):
	command = ["sips", "-s", "format", SCREENSHOT_FORMAT, ppm_path, "--out", output_path]
	try:
		result = run(command, capture_output=True, text=True)
	except OSError as error:
		raise ScreenshotError("cannot run sips: {}".format(error)) from None
	if result.returncode != 0:
		detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "no output"
		raise ScreenshotError("sips could not write {}: {}".format(output_path, detail))


def save_screenshot(client, output_path, group_container=UTM_GROUP_CONTAINER, run=None):
	"""Save the guest screen as a PNG file at ``output_path`` and return the path.

	QEMU under UTM writes only PPM, so the screendump goes to a temporary folder and sips converts it.
	"""
	run = run or subprocess.run
	with tempfile.TemporaryDirectory(prefix=SCREENSHOT_DIRECTORY_PREFIX) as directory:
		try:
			ppm_path = take_screendump(client, os.path.join(directory, SCREENDUMP_FILE_NAME), group_container)
		except OSError as error:
			raise ScreenshotError("cannot read the screen capture: {}".format(error)) from None
		_convert_to_png(ppm_path, output_path, run)
	return output_path


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
