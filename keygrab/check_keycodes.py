#!/usr/bin/env python3
"""Check keycodes.json against QEMU's QKeyCodeEnum.

Downloads qapi/ui.json from the QEMU repository (or reads a local copy passed
as the first argument) and verifies that every qcode name in keycodes.json
exists in the enum.
"""

import json
import os
import re
import sys
import urllib.request

UI_JSON_URL = "https://raw.githubusercontent.com/qemu/qemu/master/qapi/ui.json"
TABLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keycodes.json")
MIN_EXPECTED_QCODES = 100


def load_ui_json(source):
	if source:
		with open(source, "r", encoding="utf-8") as handle:
			return handle.read()
	with urllib.request.urlopen(UI_JSON_URL, timeout=30) as response:
		return response.read().decode("utf-8")


def parse_qkeycode_enum(text):
	start = text.index("QKeyCode")
	data_at = text.index("'data'", start)
	open_at = text.index("[", data_at)
	depth = 0
	for index in range(open_at, len(text)):
		if text[index] == "[":
			depth += 1
		elif text[index] == "]":
			depth -= 1
			if depth == 0:
				return set(re.findall(r"'([a-z0-9_]+)'", text[open_at:index + 1]))
	raise ValueError("unterminated QKeyCode data list in ui.json")


def main():
	source = sys.argv[1] if len(sys.argv) > 1 else None
	qcodes = parse_qkeycode_enum(load_ui_json(source))
	if len(qcodes) < MIN_EXPECTED_QCODES:
		print("parsed only %d qcodes from ui.json, expected more" % len(qcodes))
		return 1

	with open(TABLE_PATH, "r", encoding="utf-8") as handle:
		table = json.load(handle)

	keycodes = table["keycodes"]
	problems = []
	for keycode, qcode in sorted(keycodes.items(), key=lambda item: int(item[0])):
		if qcode not in qcodes:
			problems.append("keycode %s maps to unknown qcode %r" % (keycode, qcode))
		if not keycode.isdigit():
			problems.append("keycode key %r is not a decimal integer" % keycode)

	for keycode in table["_unmapped"]:
		if str(keycode) in keycodes:
			problems.append("keycode %d is listed as unmapped but also mapped" % keycode)

	seen = {}
	for keycode, qcode in keycodes.items():
		seen.setdefault(qcode, []).append(keycode)
	for qcode, owners in sorted(seen.items()):
		if len(owners) > 1:
			problems.append("qcode %r is used by keycodes %s" % (qcode, ", ".join(sorted(owners))))

	if problems:
		for problem in problems:
			print("FAIL: " + problem)
		return 1

	print("OK: %d mappings, all qcodes exist in QKeyCodeEnum (%d names), %d unmapped" %
		(len(keycodes), len(qcodes), len(table["_unmapped"])))
	return 0


if __name__ == "__main__":
	sys.exit(main())
