import os
import subprocess
import tempfile
import unittest

from wn.screendump import DRAWN_MINIMUM_COLORS, ScreenshotError, distinct_colors, looks_drawn, save_screenshot, take_screendump

BLACK_PIXEL_PPM = b"P6\n1 1\n255\n\x00\x00\x00"


def ppm(width, height, pixel_for):
	pixels = bytearray()
	for index in range(width * height):
		pixels += bytes(pixel_for(index))
	return b"P6\n%d %d\n255\n" % (width, height) + bytes(pixels)


class ColorTests(unittest.TestCase):
	def test_black_screen_is_not_drawn(self):
		image = ppm(100, 100, lambda index: (0, 0, 0))
		self.assertEqual(distinct_colors(image), 1)
		self.assertFalse(looks_drawn(image))

	def test_text_screen_is_not_drawn(self):
		image = ppm(100, 100, lambda index: (255, 255, 255) if index % 5 == 0 else (0, 0, 0))
		self.assertFalse(looks_drawn(image))

	def test_gradient_is_drawn(self):
		image = ppm(200, 200, lambda index: (index % 256, (index // 256) % 256, 10))
		self.assertGreaterEqual(distinct_colors(image), DRAWN_MINIMUM_COLORS)
		self.assertTrue(looks_drawn(image))

	def test_pixel_data_starting_with_whitespace_bytes(self):
		image = ppm(2, 1, lambda index: (32, 10, 9) if index == 0 else (1, 2, 3))
		self.assertEqual(distinct_colors(image), 1)

	def test_rejects_other_formats(self):
		with self.assertRaises(ValueError):
			distinct_colors(b"\x89PNG....")


class TakeScreendumpTests(unittest.TestCase):
	def test_moves_file_out_of_group_container(self):
		with tempfile.TemporaryDirectory() as container, tempfile.TemporaryDirectory() as output:
			class Client:
				def execute(self, command, arguments):
					self.call = (command, arguments)
					with open(os.path.join(container, arguments["filename"]), "wb") as dump:
						dump.write(b"P6\n1 1\n255\n\x00\x00\x00")
			client = Client()
			destination = os.path.join(output, "screen.ppm")
			take_screendump(client, destination, group_container=container)
			self.assertEqual(client.call[0], "screendump")
			self.assertNotIn(os.sep, client.call[1]["filename"])
			self.assertTrue(os.path.exists(destination))
			self.assertEqual(os.listdir(container), [])


class DumpingClient:
	def __init__(self, container):
		self.container = container

	def execute(self, command, arguments):
		with open(os.path.join(self.container, arguments["filename"]), "wb") as dump:
			dump.write(BLACK_PIXEL_PPM)


class EmptyClient:
	def execute(self, command, arguments):
		return {}


class SaveScreenshotTests(unittest.TestCase):
	def setUp(self):
		container = tempfile.TemporaryDirectory()
		self.addCleanup(container.cleanup)
		self.container = container.name
		self.commands = []

	def converter(self, returncode=0, stderr=""):
		def run(command, **_):
			self.commands.append(command)
			with open(command[4], "rb") as ppm_file:
				self.converted = ppm_file.read()
			return subprocess.CompletedProcess(command, returncode, stdout="", stderr=stderr)
		return run

	def test_converts_the_screendump_to_png(self):
		path = save_screenshot(DumpingClient(self.container), "/out/screen.png", self.container, self.converter())
		self.assertEqual(path, "/out/screen.png")
		command = self.commands[0]
		self.assertEqual(command[:4] + command[5:], ["sips", "-s", "format", "png", "--out", "/out/screen.png"])
		self.assertEqual(self.converted, BLACK_PIXEL_PPM)
		self.assertFalse(os.path.exists(command[4]))
		self.assertEqual(os.listdir(self.container), [])

	def test_conversion_failure(self):
		with self.assertRaises(ScreenshotError) as caught:
			save_screenshot(DumpingClient(self.container), "/out/screen.png", self.container, self.converter(returncode=1, stderr="Error: cannot write"))
		self.assertIn("Error: cannot write", str(caught.exception))
		self.assertFalse(os.path.exists(self.commands[0][4]))

	def test_missing_sips(self):
		def run(command, **_):
			raise FileNotFoundError("No such file or directory: 'sips'")
		with self.assertRaises(ScreenshotError) as caught:
			save_screenshot(DumpingClient(self.container), "/out/screen.png", self.container, run)
		self.assertIn("cannot run sips", str(caught.exception))

	def test_missing_screendump_file(self):
		with self.assertRaises(ScreenshotError) as caught:
			save_screenshot(EmptyClient(), "/out/screen.png", self.container, self.converter())
		self.assertIn("cannot read the screen capture", str(caught.exception))
		self.assertEqual(self.commands, [])


if __name__ == "__main__":
	unittest.main()
