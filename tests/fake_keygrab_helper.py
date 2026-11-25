"""A stand-in for the wn-keygrab helper.

Run as a script, it reads a JSON settings file named by its first argument,
prints the scripted lines, and logs its other arguments and every stdin command
to a log file. ``write_fake_helper`` builds an executable wrapper that runs it.
"""

import json
import os
import stat
import sys
import time

IDLE_SLEEP_SECONDS = 60


def write_fake_helper(directory, lines, wait_for_commands=False, exit_code=0, ignore_quit=False):
	"""Write a helper executable to ``directory`` and return (helper_path, log_path).

	``lines`` are dicts or raw strings. The helper prints them, then either exits
	or, with ``wait_for_commands``, reads stdin until ``quit`` or end of input.
	"""
	log_path = os.path.join(directory, "helper.log")
	settings_path = os.path.join(directory, "helper.json")
	with open(settings_path, "w") as settings_file:
		json.dump({
			"lines": [line if isinstance(line, str) else json.dumps(line) for line in lines],
			"log_path": log_path,
			"wait_for_commands": wait_for_commands,
			"exit_code": exit_code,
			"ignore_quit": ignore_quit,
		}, settings_file)
	helper_path = os.path.join(directory, "wn-keygrab")
	with open(helper_path, "w") as helper_file:
		helper_file.write('#!/bin/sh\nexec "{}" "{}" "{}" "$@"\n'.format(sys.executable, os.path.abspath(__file__), settings_path))
	os.chmod(helper_path, os.stat(helper_path).st_mode | stat.S_IXUSR)
	return helper_path, log_path


def read_log(log_path):
	"""Return (helper_arguments, stdin_commands) from a fake helper log."""
	with open(log_path) as log_file:
		entries = [json.loads(line) for line in log_file]
	return entries[0], entries[1:]


def _log(log_file, entry):
	log_file.write(json.dumps(entry) + "\n")
	log_file.flush()


def _read_commands(log_file, ignore_quit):
	for line in sys.stdin:
		command = line.strip()
		_log(log_file, command)
		if command == "quit" and not ignore_quit:
			return
	while ignore_quit:
		time.sleep(IDLE_SLEEP_SECONDS)


def main():
	with open(sys.argv[1]) as settings_file:
		settings = json.load(settings_file)
	with open(settings["log_path"], "w") as log_file:
		_log(log_file, sys.argv[2:])
		for line in settings["lines"]:
			sys.stdout.write(line + "\n")
			sys.stdout.flush()
		if settings["wait_for_commands"]:
			_read_commands(log_file, settings["ignore_quit"])
	sys.exit(settings["exit_code"])


if __name__ == "__main__":
	main()
