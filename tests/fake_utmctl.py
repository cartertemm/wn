"""Stands in for utmctl in tests. State lives in the JSON file named by FAKE_UTMCTL_STATE.

State format: {"vms": [{"uuid", "name", "status", "ip_addresses"}], "fail": {command: stderr}, "calls": [argv]}.
A VM with "request_stop_lists": N goes to "stopping" on ``stop --request`` and to "stopped" after N more ``list``
calls. N = -1 means Windows never shuts down, so the VM stays "started".
``file push <vm> <path>`` stores stdin as UTF-8 text in the VM's "files" dict, and ``file pull`` prints it.
``exec <vm> --cmd ...`` only checks the VM; the call is recorded in "calls".
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
		if "stop_countdown" in vm:
			vm["stop_countdown"] -= 1
			if vm["stop_countdown"] <= 0:
				vm["status"] = "stopped"
				del vm["stop_countdown"]
		print("{} {} {}".format(vm["uuid"], vm["status"].ljust(STATUS_COLUMN_WIDTH), vm["name"]))


def show_status(state, arguments):
	print(find_vm(state, arguments[0])["status"])


def start(state, arguments):
	find_vm(state, arguments[0])["status"] = "started"


def stop(state, arguments):
	vm = find_vm(state, arguments[0])
	vm["last_stop_flags"] = arguments[1:]
	lists = vm.get("request_stop_lists")
	if lists is None or "--request" not in arguments:
		vm["status"] = "stopped"
	elif lists >= 0:
		vm["status"] = "stopping"
		vm["stop_countdown"] = lists


def ip_address(state, arguments):
	for address in find_vm(state, arguments[0]).get("ip_addresses", []):
		print(address)


def file_operation(state, arguments):
	operation, name, guest_path = arguments
	vm = find_vm(state, name)
	if operation == "push":
		vm.setdefault("files", {})[guest_path] = sys.stdin.buffer.read().decode("utf-8")
		return
	files = vm.get("files", {})
	if operation != "pull" or guest_path not in files:
		print("Error: cannot open file", file=sys.stderr)
		sys.exit(1)
	sys.stdout.write(files[guest_path])


def exec_command(state, arguments):
	find_vm(state, arguments[0])


def delete(state, arguments):
	vm = find_vm(state, arguments[0])
	state["vms"].remove(vm)


COMMANDS = {
	"list": list_vms,
	"status": show_status,
	"start": start,
	"stop": stop,
	"ip-address": ip_address,
	"file": file_operation,
	"exec": exec_command,
	"delete": delete,
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
