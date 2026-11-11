"""Stands in for utmctl in tests. State lives in the JSON file named by FAKE_UTMCTL_STATE.

State format: {"vms": [{"uuid", "name", "status", "ip_addresses"}], "fail": {command: stderr}, "calls": [argv]}.
"""

import json
import os
import sys

STATE_ENVIRONMENT_VARIABLE = "FAKE_UTMCTL_STATE"
LIST_HEADER = "UUID                                 Status   Name"
STATUS_COLUMN_WIDTH = 8
NOT_FOUND_MESSAGE = "Error: Virtual machine not found."


def load_state(path):
	with open(path) as state_file:
		return json.load(state_file)


def save_state(path, state):
	with open(path, "w") as state_file:
		json.dump(state, state_file)


def find_vm(state, name):
	for vm in state["vms"]:
		if vm["name"] == name or vm["uuid"] == name:
			return vm
	print(NOT_FOUND_MESSAGE, file=sys.stderr)
	sys.exit(1)


def list_vms(state, arguments):
	print(LIST_HEADER)
	for vm in state["vms"]:
		print("{} {} {}".format(vm["uuid"], vm["status"].ljust(STATUS_COLUMN_WIDTH), vm["name"]))


def show_status(state, arguments):
	print(find_vm(state, arguments[0])["status"])


def start(state, arguments):
	find_vm(state, arguments[0])["status"] = "started"


def stop(state, arguments):
	vm = find_vm(state, arguments[0])
	vm["status"] = "stopped"
	vm["last_stop_flags"] = arguments[1:]


def ip_address(state, arguments):
	for address in find_vm(state, arguments[0]).get("ip_addresses", []):
		print(address)


COMMANDS = {
	"list": list_vms,
	"status": show_status,
	"start": start,
	"stop": stop,
	"ip-address": ip_address,
}


def main(argv):
	path = os.environ[STATE_ENVIRONMENT_VARIABLE]
	state = load_state(path)
	state.setdefault("calls", []).append(argv)
	save_state(path, state)
	command, arguments = argv[0], argv[1:]
	failure = state.get("fail", {}).get(command)
	if failure is not None:
		print(failure, file=sys.stderr)
		sys.exit(1)
	if command not in COMMANDS:
		print("Error: unknown command '{}'".format(command), file=sys.stderr)
		sys.exit(64)
	COMMANDS[command](state, arguments)
	save_state(path, state)


if __name__ == "__main__":
	main(sys.argv[1:])
