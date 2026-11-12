"""Measure trigger to sound at the Mac mic: python3 tools/latency/measure_audio.py [--count N] [--trigger beep|ding|key|mac]

Each trial starts bin/record_beep, waits for the mic to settle and for the
noise floor window, notes t0, triggers a sound, and reads the onset from
record_beep. Both use the mach clock: CLOCK_UPTIME_RAW. Python's
host_now_ns() counts from process start on 3.9, so it does not match.

Triggers:
  beep  UDP "beep HZ" to latency-agent.ps1, which calls [console]::Beep(HZ, 150).
  ding  UDP "ding", the agent plays Windows Ding.wav.
  key   QMP press of --key (default left) to the focused guest window. In a text field NVDA reads the
        character it lands on.
  mac   no guest: record_beep plays its own 1 kHz tone on the Mac (Mac speaker plus mic latency).

With --load, each trial first presses ctrl+home and insert+down through QMP so NVDA
reads the focused document (say all), waits --load-lead seconds, and stops speech
with ctrl afterwards. Use a --freq where speech is quiet, such as 5000.

A trial whose noise floor is above --max-floor is retried, up to --retries
times. A dropout is a valid trial with no onset, or an onset later than 2x the median.
"""

import argparse
import json
import os
import subprocess
import time

from common import DEFAULT_QMP_ADDRESS, NANOSECONDS_PER_MILLISECOND, REPOSITORY, Agent, guest_ip, summarize, write_result
from wn.keys import combo_to_input_events, key_event
from wn.qmp import qmp_client_for

RECORDER = os.path.join(REPOSITORY, "tools", "latency", "bin", "record_beep")
RECORD_SECONDS = 2.0
SETTLE_MS = 250
FLOOR_MS = 300
TRIGGER_MARGIN_MS = 100
MAC_TONE_AT_MS = 800
DEFAULT_MAX_FLOOR = 0.01
LATE_FACTOR = 2
PAUSE_BETWEEN_TRIALS_SECONDS = 0.5
MAC_VOLUME = 50
LOAD_START_COMBOS = ("ctrl+home", "insert+down")
LOAD_STOP_COMBO = "ctrl"
COMBO_HOLD_SECONDS = 0.05
COMBO_GAP_SECONDS = 0.2


def host_now_ns():
	return time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)


def get_volume():
	return int(subprocess.run(["osascript", "-e", "output volume of (get volume settings)"], capture_output=True, text=True, check=True).stdout.strip())


def set_volume(volume):
	subprocess.run(["osascript", "-e", "set volume output volume {}".format(volume)], check=True)


class Trigger:
	def __init__(self, kind, freq, agent, qmp_client, key):
		self.kind = kind
		self.key = key
		self.freq = freq
		self.agent = agent
		self.qmp_client = qmp_client

	def fire(self):
		"""Trigger the sound. Return the agent ack time in ms after the call, or None."""
		start = host_now_ns()
		if self.kind == "beep":
			self.agent.send("beep {}".format(self.freq))
			reply = self.agent.expect("ack beep")
		elif self.kind == "ding":
			self.agent.send("ding")
			reply = self.agent.expect("ack ding")
		elif self.kind == "key":
			self.qmp_client.execute("input-send-event", {"events": [key_event(self.key, True)]})
			self.qmp_client.execute("input-send-event", {"events": [key_event(self.key, False)]})
			return None
		else:
			return None
		return None if reply is None else (host_now_ns() - start) / NANOSECONDS_PER_MILLISECOND


def press_combo(client, combo):
	press, release = combo_to_input_events(combo)
	client.execute("input-send-event", press)
	time.sleep(COMBO_HOLD_SECONDS)
	client.execute("input-send-event", release)
	time.sleep(COMBO_GAP_SECONDS)


def start_load(client, lead_seconds):
	for combo in LOAD_START_COMBOS:
		press_combo(client, combo)
	time.sleep(lead_seconds)


def record_trial(trigger, detect_freq, factor):
	command = [RECORDER, "--seconds", str(RECORD_SECONDS), "--freq", str(detect_freq), "--factor", str(factor), "--settle-ms", str(SETTLE_MS), "--floor-ms", str(FLOOR_MS)]
	if trigger.kind == "mac":
		command += ["--play-tone-at-ms", str(MAC_TONE_AT_MS)]
	recorder = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
	try:
		ready = recorder.stdout.readline().split()
		if len(ready) != 2 or ready[0] != "ready":
			raise SystemExit("record_beep did not start: {} (exit code {})".format(" ".join(ready), recorder.wait()))
		start_ns = int(ready[1])
		ack_ms = None
		if trigger.kind == "mac":
			t0 = start_ns + MAC_TONE_AT_MS * NANOSECONDS_PER_MILLISECOND
		else:
			wait_ns = start_ns + (SETTLE_MS + FLOOR_MS + TRIGGER_MARGIN_MS) * NANOSECONDS_PER_MILLISECOND - host_now_ns()
			time.sleep(max(0, wait_ns) / 1e9)
			t0 = host_now_ns()
			ack_ms = trigger.fire()
		result = json.loads(recorder.stdout.read())
	finally:
		recorder.wait()
	result["t0_host_ns"] = t0
	result["ack_ms"] = ack_ms
	result["latency_ms"] = None if result.get("onset_host_ns") is None else (result["onset_host_ns"] - t0) / NANOSECONDS_PER_MILLISECOND
	return result


def run(kind, count, freq, detect_freq, factor, max_floor, retries, address, qmp_address, key, load_lead):
	agent = Agent(address) if kind in ("beep", "ding") else None
	qmp_client = qmp_client_for(qmp_address) if kind == "key" or load_lead is not None else None
	if qmp_client:
		qmp_client.connect()
	trigger = Trigger(kind, freq, agent, qmp_client, key)
	trials = []
	try:
		for _ in range(count):
			for _ in range(retries + 1):
				if load_lead is not None:
					start_load(qmp_client, load_lead)
				trial = record_trial(trigger, detect_freq, factor)
				if load_lead is not None:
					press_combo(qmp_client, LOAD_STOP_COMBO)
				trial["valid"] = trial["floor_energy"] <= max_floor
				trials.append(trial)
				time.sleep(PAUSE_BETWEEN_TRIALS_SECONDS)
				if trial["valid"]:
					break
	finally:
		if agent:
			agent.close()
		if qmp_client:
			qmp_client.close()
	result = analyse(kind, freq, detect_freq, trials)
	result["load_lead_seconds"] = load_lead
	return result


def analyse(kind, freq, detect_freq, trials):
	valid = [trial for trial in trials if trial["valid"]]
	latencies = [trial["latency_ms"] for trial in valid if trial["latency_ms"] is not None]
	summary = summarize(latencies)
	late_limit = summary.get("median", 0) * LATE_FACTOR
	missing = sum(1 for trial in valid if trial["latency_ms"] is None)
	late = sum(1 for latency in latencies if latency > late_limit)
	acks = [trial["ack_ms"] for trial in valid if trial["ack_ms"] is not None]
	gaps = [trial["max_gap_ms"] for trial in valid if trial["latency_ms"] is not None]
	above = [trial["above_ms"] for trial in valid if trial["latency_ms"] is not None]
	return {
		"trigger": kind,
		"freq": freq,
		"detect_freq": detect_freq,
		"summary": summary,
		"valid_trials": len(valid),
		"invalid_trials": len(trials) - len(valid),
		"missing": missing,
		"late": late,
		"dropouts": missing + late,
		"ack_ms": summarize(acks),
		"max_gap_ms": summarize(gaps),
		"above_ms": summarize(above),
		"trials": trials,
	}


def main():
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--count", type=int, default=20)
	parser.add_argument("--trigger", choices=("beep", "ding", "key", "mac"), default="beep")
	parser.add_argument("--freq", type=int, default=1000, help="beep frequency in Hz (default: %(default)s)")
	parser.add_argument("--key", default="left", help="qcode the key trigger presses (default: %(default)s)")
	parser.add_argument("--detect-freq", type=int, help="frequency record_beep listens at, 0 for broadband (default: --freq for beep and mac, else 0)")
	parser.add_argument("--factor", type=float, default=3.0, help="onset threshold over the noise floor (default: %(default)s)")
	parser.add_argument("--max-floor", type=float, default=DEFAULT_MAX_FLOOR, help="retry trials with a noisier floor (default: %(default)s)")
	parser.add_argument("--load", action="store_true", help="make NVDA say all during each trial")
	parser.add_argument("--load-lead", type=float, default=1.0, help="seconds of speech before recording starts (default: %(default)s)")
	parser.add_argument("--retries", type=int, default=3)
	parser.add_argument("--guest", help="guest IP (default: wn ip)")
	parser.add_argument("--qmp", default=DEFAULT_QMP_ADDRESS)
	parser.add_argument("--volume", type=int, default=MAC_VOLUME, help="Mac output volume during the test, restored after (default: %(default)s)")
	parser.add_argument("--out", help="write the result as JSON here")
	arguments = parser.parse_args()
	detect_freq = arguments.detect_freq
	if detect_freq is None:
		detect_freq = arguments.freq if arguments.trigger in ("beep", "mac") else 0
	address = None
	if arguments.trigger in ("beep", "ding"):
		address = arguments.guest or guest_ip()
	previous_volume = get_volume()
	set_volume(arguments.volume)
	try:
		result = run(arguments.trigger, arguments.count, arguments.freq, detect_freq, arguments.factor, arguments.max_floor, arguments.retries, address, arguments.qmp, arguments.key, arguments.load_lead if arguments.load else None)
	finally:
		set_volume(previous_volume)
	write_result(arguments.out, result)
	print("{} ms: {}".format(arguments.trigger, result["summary"]))
	print("valid {} invalid {} missing {} late {} dropouts {}".format(result["valid_trials"], result["invalid_trials"], result["missing"], result["late"], result["dropouts"]))
	print("ack ms {} | max gap ms {} | above ms {}".format(result["ack_ms"], result["max_gap_ms"], result["above_ms"]))


if __name__ == "__main__":
	main()
