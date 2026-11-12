"""Write the Mac's battery status to the native battery in the guest, and manage the launch agent that runs it."""

import datetime
import json
import os
import re
import subprocess
import sys
import time
from xml.sax.saxutils import escape

from wn.backend import create_backend
from wn.battery_file import battery_writer
from wn.battery_sync import LOG_PREFIX, battery_reading, run_sync_loop, sync_message
from wn.errors import WnError

PMSET_COMMAND = ["/usr/bin/pmset", "-g", "batt"]
LAUNCHCTL_PATH = "/bin/launchctl"
AC_POWER_TEXT = "'AC Power'"
CHARGING_STATES = ("charging", "finishing charge")
CHARGED_STATE = "charged"
MINUTES_PER_HOUR = 60
LAUNCHD_LABEL = "com.wn.agent"
LAUNCHD_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launchd", LAUNCHD_LABEL + ".plist")
LAUNCH_AGENTS_DIRECTORY = os.path.join("~", "Library", "LaunchAgents")
LOG_PATH = os.path.join("~", "Library", "Logs", "wn-agent.log")
REPOSITORY_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BATTERY_LINE = re.compile(r"-InternalBattery-\d+.*?\t(?P<percent>\d+)%;\s*(?P<state>[^;]+);\s*(?P<rest>.*)")
TIME_REMAINING = re.compile(r"(?P<hours>\d+):(?P<minutes>\d{2}) remaining")


class AgentError(WnError):
	"""Raised when the agent cannot read the battery or manage its launch agent."""


def utc_now():
	return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_pmset(text, updated):
	"""Turn ``pmset -g batt`` output into the battery status object that the battery sync reads."""
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


def qmp_address(config, backend_factory=create_backend):
	return config.qmp_socket or backend_factory(config).qmp_socket_path()


def sync_once(sync, status):
	"""Write one reading to the guest and return a line that says what happened."""
	try:
		return sync_message(sync.write(battery_reading(status)))
	except WnError as error:
		return LOG_PREFIX + str(error)


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


def run_agent_command(arguments, config, output, messages=None, wait=time.sleep):
	messages = messages or sys.stderr
	if arguments.once:
		status = battery_status()
		output.write(json.dumps(status, indent="\t") + "\n")
		output.flush()
		if config.battery_sync:
			messages.write(sync_once(battery_writer(config, lambda: qmp_address(config)), status) + "\n")
	elif arguments.install:
		path = install_launch_agent(sys.executable, REPOSITORY_DIRECTORY)
		output.write("Installed {}. The agent now starts when you log in. Log: {}\n".format(path, os.path.expanduser(LOG_PATH)))
	elif arguments.uninstall:
		if uninstall_launch_agent():
			output.write("Removed {}\n".format(launch_agent_path()))
		else:
			output.write("The launch agent is not installed\n")
	else:
		if not config.battery_sync:
			raise AgentError("battery_sync is off in the config, so the agent has nothing to do")
		try:
			run_sync_loop(battery_writer(config, lambda: qmp_address(config)), battery_status, output, wait)
		except KeyboardInterrupt:
			pass
