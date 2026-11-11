import os
import tempfile
import unittest

from wn.screendump import DRAWN_MINIMUM_COLORS, distinct_colors, looks_drawn, take_screendump


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


if __name__ == "__main__":
	unittest.main()
