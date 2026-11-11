import unittest

from wn.mouse import (
	ABS_AXIS_MAX,
	CLICK_HOLD_MS,
	MouseError,
	button_event,
	click_steps,
	double_click_steps,
	move_steps,
	parse_screen_size,
	position_to_abs,
	press_steps,
	release_steps,
	scroll_steps,
)


def button(name, down):
	return {"events": [button_event(name, down)]}


class EventBuilderTests(unittest.TestCase):
	def test_button_event(self):
		self.assertEqual(button_event("left", True), {"type": "btn", "data": {"down": True, "button": "left"}})

	def test_move(self):
		self.assertEqual(move_steps(100, 200), [({"events": [
			{"type": "abs", "data": {"axis": "x", "value": 100}},
			{"type": "abs", "data": {"axis": "y", "value": 200}},
		]}, 0)])

	def test_click_holds_then_releases(self):
		self.assertEqual(click_steps("right"), [(button("right", True), CLICK_HOLD_MS), (button("right", False), 0)])

	def test_double_click_is_two_clicks(self):
		arguments = [arguments for arguments, _ in double_click_steps("left")]
		self.assertEqual(arguments, [button("left", True), button("left", False)] * 2)

	def test_press_and_release(self):
		self.assertEqual(press_steps("middle"), [(button("middle", True), 0)])
		self.assertEqual(release_steps("middle"), [(button("middle", False), 0)])

	def test_scroll_presses_wheel_once_per_notch(self):
		arguments = [arguments for arguments, _ in scroll_steps("up", 3)]
		self.assertEqual(arguments, [button("wheel-up", True), button("wheel-up", False)] * 3)

	def test_scroll_needs_a_positive_count(self):
		with self.assertRaises(MouseError):
			scroll_steps("down", 0)


class PositionTests(unittest.TestCase):
	def test_parse_screen_size(self):
		self.assertEqual(parse_screen_size("1280x720"), (1280, 720))

	def test_bad_screen_size(self):
		for text in ("1280", "0x720", "axb", "1280x720x1"):
			with self.assertRaises(MouseError, msg=text):
				parse_screen_size(text)

	def test_absolute_position_passes_through(self):
		self.assertEqual(position_to_abs(0, ABS_AXIS_MAX), (0, ABS_AXIS_MAX))

	def test_absolute_position_out_of_range(self):
		for x, y in ((-1, 0), (0, ABS_AXIS_MAX + 1)):
			with self.assertRaises(MouseError):
				position_to_abs(x, y)

	def test_pixels_scale_like_qemu(self):
		self.assertEqual(position_to_abs(640, 360, "1280x720"), (16383, 16383))
		self.assertEqual(position_to_abs(0, 0, "1280x720"), (0, 0))
		self.assertEqual(position_to_abs(1279, 719, "1280x720"), (32741, 32721))

	def test_pixels_outside_screen(self):
		with self.assertRaises(MouseError):
			position_to_abs(1280, 0, "1280x720")


if __name__ == "__main__":
	unittest.main()
