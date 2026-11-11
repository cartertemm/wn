"""Shared parts of the latency tools: the guest agent socket, the guest IP, and summaries."""

import json
import os
import socket
import subprocess
import sys

REPOSITORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPOSITORY)

AGENT_PORT = 47000
REPLY_PORT = 47001
DEFAULT_QMP_ADDRESS = "tcp:127.0.0.1:4444"
NANOSECONDS_PER_MILLISECOND = 1000000
PERCENTILE_90 = 0.9


def guest_ip():
	return subprocess.run([sys.executable, "-m", "wn", "ip"], cwd=REPOSITORY, capture_output=True, text=True, check=True).stdout.strip()


class Agent:
	"""Talks to latency-agent.ps1 in the guest over UDP."""

	def __init__(self, address, timeout=5.0):
		self.address = (address, AGENT_PORT)
		self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		self.socket.bind(("0.0.0.0", REPLY_PORT))
		self.socket.settimeout(timeout)

	def send(self, text):
		self.socket.sendto(text.encode(), self.address)

	def receive(self):
		"""Return the next reply as text, or None after the timeout."""
		try:
			data, _ = self.socket.recvfrom(256)
		except socket.timeout:
			return None
		return data.decode(errors="replace")

	def expect(self, prefix):
		"""Return the next reply that starts with ``prefix``, skipping others, or None after a timeout."""
		while True:
			reply = self.receive()
			if reply is None or reply.startswith(prefix):
				return reply

	def drain(self):
		timeout = self.socket.gettimeout()
		self.socket.setblocking(False)
		try:
			while True:
				self.socket.recvfrom(256)
		except OSError:
			pass
		finally:
			self.socket.settimeout(timeout)

	def close(self):
		self.socket.close()


def percentile(sorted_values, fraction):
	index = min(len(sorted_values) - 1, int(round(fraction * (len(sorted_values) - 1))))
	return sorted_values[index]


def summarize(values):
	"""Return min, median, p90, max and count of ``values`` in ms, rounded to 0.01."""
	if not values:
		return {"count": 0}
	ordered = sorted(values)
	middle = len(ordered) // 2
	median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
	return {
		"count": len(ordered),
		"min": round(ordered[0], 2),
		"median": round(median, 2),
		"p90": round(percentile(ordered, PERCENTILE_90), 2),
		"max": round(ordered[-1], 2),
	}


def write_result(path, result):
	if path:
		os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
		with open(path, "w") as result_file:
			json.dump(result, result_file, indent=1)
