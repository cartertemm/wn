"""Build QMP ``input-send-event`` arguments for the guest's USB tablet.

QEMU's USB tablet takes absolute positions from 0 to 32767 on each axis.
A screen size turns pixel positions into that range the same way QEMU does
for its own display window.
"""

from wn.errors import WnError

ABS_AXIS_MAX = 32767
CLICK_HOLD_MS = 50
DOUBLE_CLICK_GAP_MS = 80
SCROLL_GAP_MS = 30
DEFAULT_BUTTON = "left"
BUTTONS = ("left", "right", "middle")
WHEEL_BUTTONS = {"up": "wheel-up", "down": "wheel-down"}
SCREEN_SIZE_SEPARATOR = "x"


class MouseError(WnError):
	"""Raised for a bad position, screen size, or scroll count."""


def button_event(button, down):
	return {"type": "btn", "data": {"down": down, "button": button}}


def _button_arguments(button, down):
	return {"events": [button_event(button, down)]}


def move_steps(x, y):
	"""Return the steps that move the pointer to absolute position ``x``, ``y``."""
	return [({"events": [
		{"type": "abs", "data": {"axis": "x", "value": x}},
		{"type": "abs", "data": {"axis": "y", "value": y}},
	]}, 0)]


def press_steps(button):
	return [(_button_arguments(button, True), 0)]


def release_steps(button):
	return [(_button_arguments(button, False), 0)]


def click_steps(button):
	return [(_button_arguments(button, True), CLICK_HOLD_MS), (_button_arguments(button, False), 0)]


def double_click_steps(button):
	return [(_button_arguments(button, True), CLICK_HOLD_MS), (_button_arguments(button, False), DOUBLE_CLICK_GAP_MS)] + click_steps(button)


def scroll_steps(direction, count):
	"""Return the steps that turn the wheel ``count`` notches ``up`` or ``down``."""
	if count < 1:
		raise MouseError("scroll count must be 1 or more, not {}".format(count))
	wheel = WHEEL_BUTTONS[direction]
	return [(_button_arguments(wheel, True), 0), (_button_arguments(wheel, False), SCROLL_GAP_MS)] * count


def parse_screen_size(text):
	"""Return ``(width, height)`` from text such as ``1280x720``."""
	parts = text.lower().split(SCREEN_SIZE_SEPARATOR)
	if len(parts) != 2 or not all(part.isdigit() and int(part) > 0 for part in parts):
		raise MouseError("bad screen size {!r}. Use WIDTHxHEIGHT, for example 1280x720".format(text))
	return int(parts[0]), int(parts[1])


def _pixel_to_abs(pixel, size):
	return pixel * ABS_AXIS_MAX // size


def position_to_abs(x, y, screen_size=None):
	"""Return the tablet position for ``x``, ``y``: pixels when ``screen_size`` is given, else 0..32767 values."""
	if screen_size is None:
		if not all(0 <= value <= ABS_AXIS_MAX for value in (x, y)):
			raise MouseError("position {} {} is outside 0..{}. Give --screen WIDTHxHEIGHT to use pixels".format(x, y, ABS_AXIS_MAX))
		return x, y
	width, height = parse_screen_size(screen_size)
	if not (0 <= x < width and 0 <= y < height):
		raise MouseError("pixel {} {} is outside the {}x{} screen".format(x, y, width, height))
	return _pixel_to_abs(x, width), _pixel_to_abs(y, height)
