"""Measure QMP key press to guest ReadKey: python3 tools/latency/measure_input.py [--count N] [--device ID]

Needs latency-agent.ps1 running in a focused guest window. Each trial sends
"key" to the agent, waits for "ack key", then presses and releases ``a``
through QMP input-send-event and times the agent's "key a" reply. The result
is the input path plus guest wakeup plus the UDP reply (under 1 ms).
"""

import argparse
import time

from common import DEFAULT_QMP_ADDRESS, NANOSECONDS_PER_MILLISECOND, Agent, guest_ip, summarize, write_result
from wn.keys import key_event
from wn.qmp import qmp_client_for

KEY = "a"
PAUSE_BETWEEN_TRIALS_SECONDS = 0.3


def key_arguments(down, device):
	arguments = {"events": [key_event(KEY, down)]}
	if device:
		arguments["device"] = device
	return arguments


def run(count, device, qmp_address, address):
	agent = Agent(address)
	samples = []
	qmp_samples = []
	misses = 0
	try:
		with qmp_client_for(qmp_address) as client:
			for _ in range(count):
				agent.drain()
				agent.send("key")
				if agent.expect("ack key") is None:
					raise SystemExit("no 'ack key' from the agent at {}. Is latency-agent.ps1 running?".format(address))
				start = time.perf_counter_ns()
				client.execute("input-send-event", key_arguments(True, device))
				qmp_samples.append((time.perf_counter_ns() - start) / NANOSECONDS_PER_MILLISECOND)
				client.execute("input-send-event", key_arguments(False, device))
				reply = agent.expect("key ")
				end = time.perf_counter_ns()
				if reply is None or reply.strip() != "key " + KEY:
					misses += 1
				else:
					samples.append((end - start) / NANOSECONDS_PER_MILLISECOND)
				time.sleep(PAUSE_BETWEEN_TRIALS_SECONDS)
	finally:
		agent.close()
	return {"device": device, "samples_ms": [round(sample, 3) for sample in samples], "misses": misses, "summary": summarize(samples), "qmp_key_down_ms": summarize(qmp_samples)}


def main():
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--count", type=int, default=30)
	parser.add_argument("--device", help="QOM id of the keyboard to send to, such as vkbd. Default: QEMU picks")
	parser.add_argument("--qmp", default=DEFAULT_QMP_ADDRESS)
	parser.add_argument("--guest", help="guest IP (default: wn ip)")
	parser.add_argument("--out", help="write the result as JSON here")
	arguments = parser.parse_args()
	result = run(arguments.count, arguments.device, arguments.qmp, arguments.guest or guest_ip())
	write_result(arguments.out, result)
	print("input ms: {} misses {}".format(result["summary"], result["misses"]))
	print("QMP key down execute ms: {}".format(result["qmp_key_down_ms"]))


if __name__ == "__main__":
	main()
