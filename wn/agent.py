"""Serve the Mac's battery status to the guest over the VM network. The wn-tools tray app in Windows reads it."""

import datetime
import http.server
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from xml.sax.saxutils import escape

from wn.errors import WnError

DEFAULT_AGENT_PORT = 47100
PMSET_COMMAND = ["/usr/bin/pmset", "-g", "batt"]
IFCONFIG_COMMAND = ["/sbin/ifconfig"]
LAUNCHCTL_PATH = "/bin/launchctl"
VMNET_SUBNET = ipaddress.ip_network("192.168.64.0/24")
VMNET_FALLBACK_INTERFACE = "bridge100"
BRIDGE_PREFIX = "bridge"
AC_POWER_TEXT = "'AC Power'"
CHARGING_STATES = ("charging", "finishing charge")
CHARGED_STATE = "charged"
MINUTES_PER_HOUR = 60
BIND_POLL_SECONDS = 10
LAUNCHD_LABEL = "com.wn.agent"
LAUNCHD_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launchd", LAUNCHD_LABEL + ".plist")
LAUNCH_AGENTS_DIRECTORY = os.path.join("~", "Library", "LaunchAgents")
LOG_PATH = os.path.join("~", "Library", "Logs", "wn-agent.log")
REPOSITORY_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_CONTENT_TYPE = "application/json"
BATTERY_LINE = re.compile(r"-InternalBattery-\d+.*?\t(?P<percent>\d+)%;\s*(?P<state>[^;]+);\s*(?P<rest>.*)")
TIME_REMAINING = re.compile(r"(?P<hours>\d+):(?P<minutes>\d{2}) remaining")
INTERFACE_LINE = re.compile(r"^(?P<name>[A-Za-z0-9]+):")
INET_LINE = re.compile(r"^\s+inet (?P<address>\d+\.\d+\.\d+\.\d+)")


class AgentError(WnError):
	"""Raised when the agent cannot read the battery or manage its launch agent."""


def utc_now():
	return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_pmset(text, updated):
	"""Turn ``pmset -g batt`` output into the JSON object that wn-tools reads."""
	lines = text.splitlines()
	on_ac = bool(lines) and AC_POWER_TEXT in lines[0]
	status = {"present": False, "percent": None, "charging": False, "on_ac": on_ac, "minutes_remaining": None, "updated": updated}
	for line in lines[1:]:
		match = BATTERY_LINE.search(line)
		if not match:
			continue
		state = match.group("state").strip()
		time_match = TIME_REMAINING.search(match.group("rest"))
		minutes = None
		if time_match and state != CHARGED_STATE:
			minutes = int(time_match.group("hours")) * MINUTES_PER_HOUR + int(time_match.group("minutes"))
		status.update(present=True, percent=int(match.group("percent")), charging=state in CHARGING_STATES, minutes_remaining=minutes)
		break
	return status


def battery_status(run=None):
	run = run or subprocess.run
	result = run(PMSET_COMMAND, capture_output=True, text=True)
	if result.returncode != 0:
		raise AgentError("pmset failed with exit code {}: {}".format(result.returncode, (result.stderr or "").strip() or "no output"))
	return parse_pmset(result.stdout, utc_now())


def _bridge_addresses(ifconfig_output):
	interface = None
	for line in ifconfig_output.splitlines():
		interface_match = INTERFACE_LINE.match(line)
		if interface_match:
			interface = interface_match.group("name")
			continue
		inet_match = INET_LINE.match(line)
		if inet_match and interface and interface.startswith(BRIDGE_PREFIX):
			yield interface, inet_match.group("address")


def find_bind_address(configured, run=None):
	"""Return the Mac's address on the UTM shared network, or None while that network is down."""
	if configured:
		return configured
	run = run or subprocess.run
	addresses = list(_bridge_addresses(run(IFCONFIG_COMMAND, capture_output=True, text=True).stdout))
	for _, address in addresses:
		if ipaddress.ip_address(address) in VMNET_SUBNET:
			return address
	for interface, address in addresses:
		if interface == VMNET_FALLBACK_INTERFACE:
			return address
	return None


class AgentRequestHandler(http.server.BaseHTTPRequestHandler):
	server_version = "wn-agent"

	def do_GET(self):
		if self.path == "/ping":
			self._send_json(200, {"ok": True})
		elif self.path == "/battery":
			try:
				self._send_json(200, self.server.read_status())
			except WnError as error:
				self._send_json(500, {"error": str(error)})
		else:
			self._send_json(404, {"error": "not found"})

	def _send_json(self, code, value):
		body = json.dumps(value).encode("utf-8")
		self.send_response(code)
		self.send_header("Content-Type", JSON_CONTENT_TYPE)
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def log_message(self, format, *arguments):
		pass


def make_server(address, port, read_status=battery_status):
	server = http.server.ThreadingHTTPServer((address, port), AgentRequestHandler)
	server.daemon_threads = True
	server.read_status = read_status
	return server


def serve(configured_bind, port, output, find=find_bind_address, sleep=time.sleep):
	"""Wait until the VM network is up, then answer requests on it until interrupted."""
	address = find(configured_bind)
	if address is None:
		output.write("Waiting for the UTM shared network (bridge100). Start the VM, or set agent_bind\n")
		output.flush()
	while address is None:
		sleep(BIND_POLL_SECONDS)
		address = find(configured_bind)
	try:
		server = make_server(address, port)
	except OSError as error:
		raise AgentError("cannot listen on {}:{}: {}".format(address, port, error)) from None
	output.write("Serving battery status on http://{}:{}/battery\n".format(address, port))
	output.flush()
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		server.server_close()


def launch_agent_path():
	return os.path.join(os.path.expanduser(LAUNCH_AGENTS_DIRECTORY), LAUNCHD_LABEL + ".plist")


def _launchctl(run, *arguments):
	return run([LAUNCHCTL_PATH] + list(arguments), capture_output=True, text=True)


def render_launch_agent(python, working_directory):
	with open(LAUNCHD_TEMPLATE) as template_file:
		text = template_file.read()
	for placeholder, value in (
		("@LABEL@", LAUNCHD_LABEL),
		("@PYTHON@", python),
		("@WORKING_DIRECTORY@", working_directory),
		("@LOG@", os.path.expanduser(LOG_PATH)),
	):
		text = text.replace(placeholder, escape(value))
	return text


def install_launch_agent(python, working_directory, run=None, uid=None):
	"""Write the launch agent plist and load it, so the agent starts at every login. Return the plist path."""
	run = run or subprocess.run
	uid = os.getuid() if uid is None else uid
	path = launch_agent_path()
	if os.path.exists(path):
		_launchctl(run, "bootout", "gui/{}/{}".format(uid, LAUNCHD_LABEL))
	os.makedirs(os.path.dirname(path), exist_ok=True)
	os.makedirs(os.path.dirname(os.path.expanduser(LOG_PATH)), exist_ok=True)
	with open(path, "w") as plist_file:
		plist_file.write(render_launch_agent(python, working_directory))
	result = _launchctl(run, "bootstrap", "gui/{}".format(uid), path)
	if result.returncode != 0:
		detail = (result.stderr or "").strip() or (result.stdout or "").strip() or "no output"
		raise AgentError("launchctl bootstrap failed with exit code {}: {}".format(result.returncode, detail))
	return path


def uninstall_launch_agent(run=None, uid=None):
	"""Unload and delete the launch agent. Return False if it was not installed."""
	run = run or subprocess.run
	uid = os.getuid() if uid is None else uid
	path = launch_agent_path()
	if not os.path.exists(path):
		return False
	_launchctl(run, "bootout", "gui/{}/{}".format(uid, LAUNCHD_LABEL))
	os.remove(path)
	return True


def run_agent_command(arguments, config, output):
	if arguments.once:
		output.write(json.dumps(battery_status(), indent="\t") + "\n")
	elif arguments.install:
		path = install_launch_agent(sys.executable, REPOSITORY_DIRECTORY)
		output.write("Installed {}. The agent now starts when you log in. Log: {}\n".format(path, os.path.expanduser(LOG_PATH)))
	elif arguments.uninstall:
		if uninstall_launch_agent():
			output.write("Removed {}\n".format(launch_agent_path()))
		else:
			output.write("The launch agent is not installed\n")
	else:
		serve(config.agent_bind, config.agent_port, output)
