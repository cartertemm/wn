"""Measure host-side costs of wn grab without touching the guest: python3 tools/latency/measure_grab_path.py

1. 200 synthetic wn-keygrab JSON lines go through the same parse, translate, and
   QMP execute steps as wn.keygrab, to the fake QMP server from tests/.
2. 50 query-status round trips to the real VM's QMP port.
"""

import argparse
import json
import os
import sys
import time

from common import DEFAULT_QMP_ADDRESS, NANOSECONDS_PER_MILLISECOND, REPOSITORY, summarize, write_result

sys.path.insert(0, os.path.join(REPOSITORY, "tests"))

from fake_qmp_server import FakeQMPServer, reply
from wn.keygrab import KeyEventTranslator, _parse_line, default_keycodes_path, load_keycode_table
from wn.qmp import QMPClient, qmp_client_for

KEYCODE_A = 0
LINE_COUNT = 200
QUERY_COUNT = 50


def synthetic_lines(count):
	lines = []
	for index in range(count):
		line_type = "down" if index % 2 == 0 else "up"
		lines.append(json.dumps({"type": line_type, "keycode": KEYCODE_A, "flags": 0, "ts": float(index)}))
	return lines


def measure_fake_path(count):
	translator = KeyEventTranslator(load_keycode_table(default_keycodes_path()))
	samples = []
	with FakeQMPServer({"input-send-event": lambda request: [reply(request, {})]}) as server:
		with QMPClient(server.socket_path) as client:
			for text in synthetic_lines(count):
				start = time.perf_counter_ns()
				for arguments in translator.translate(_parse_line(text)):
					client.execute("input-send-event", arguments)
				samples.append((time.perf_counter_ns() - start) / NANOSECONDS_PER_MILLISECOND)
	return samples


def measure_real_qmp(address, count):
	samples = []
	with qmp_client_for(address) as client:
		for _ in range(count):
			start = time.perf_counter_ns()
			client.execute("query-status")
			samples.append((time.perf_counter_ns() - start) / NANOSECONDS_PER_MILLISECOND)
	return samples


def main():
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--qmp", default=DEFAULT_QMP_ADDRESS)
	parser.add_argument("--out", help="write the result as JSON here")
	arguments = parser.parse_args()
	fake = measure_fake_path(LINE_COUNT)
	real = measure_real_qmp(arguments.qmp, QUERY_COUNT)
	result = {"fake_path_ms": summarize(fake), "real_query_status_ms": summarize(real)}
	write_result(arguments.out, result)
	print("grab path per line to fake QMP, ms: {}".format(result["fake_path_ms"]))
	print("query-status round trip to the VM, ms: {}".format(result["real_query_status_ms"]))


if __name__ == "__main__":
	main()
