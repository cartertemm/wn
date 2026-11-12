"""Create a Windows 11 ARM64 VM in UTM and install it with no input after the questions."""

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Optional, Tuple

from wn.agent import LOG_PATH, REPOSITORY_DIRECTORY, install_launch_agent
from wn.backends.utm import UTMBackend, UTMCtlError
from wn.battery_file import battery_file_path, prepare_battery_file
from wn.config import config_path, load_config
from wn.downloads import fetch_all
from wn.errors import WnError
from wn.payload import SECURE_BOOT_CERT, build_payload_iso
from wn.qcow2 import create_empty_qcow2
from wn.qmp import QMPConnectionError, QMPError, qmp_client_for
from wn.screendump import looks_drawn, take_screendump
from wn.unattend import SETUP_DONE_MARKER, SETUP_DONE_TEXT, SETUP_REBOOTING_TEXT, UnattendOptions, computer_name_for, mac_time_zone
from wn.utm_config import (
	AUDIO_COREAUDIO,
	BUNDLE_SUFFIX,
	DATA_DIRECTORY_NAME,
	DEFAULT_OPTIONS,
	default_battery_file,
	detach_drives,
	random_mac_address,
	set_display_hardware,
	write_bundle,
)

UTM_APP_PATH = "/Applications/UTM.app"
UTM_PROCESS_NAME = "UTM"
FINAL_AUDIO_ARGS = (
	"-audiodev coreaudio,id=ca0,timer-period=2500,out.buffer-length=5000,out.buffer-count=3,"
	"out.frequency=48000,out.fixed-settings=on -device intel-hda -device hda-output,audiodev=ca0"
)
SETUP_DISPLAY = "virtio-ramfb"
FINAL_DISPLAY = "virtio-gpu-pci"
WINDOWS_ISO_IMAGE = "windows11-arm64.iso"
PAYLOAD_IMAGE = "wnsetup.iso"
DISK_IMAGE = DEFAULT_OPTIONS["disk_image"]
SOFTWARE_NVDA = "nvda"
SOFTWARE_CHOICES = ((SOFTWARE_NVDA, "NVDA screen reader"),)
STOPPED_STATE = "stopped"

UTM_QUIT_TIMEOUT_SECONDS = 30
UTM_REGISTER_TIMEOUT_SECONDS = 60
QMP_OPEN_TIMEOUT_SECONDS = 120
BOOT_KEY_BURST_SECONDS = 15
BOOT_KEY_INTERVAL_SECONDS = 0.5
IP_POLL_SECONDS = 10
MARKER_POLL_SECONDS = 15
INSTALL_TIMEOUT_SECONDS = 45 * 60
HEARTBEAT_SECONDS = 60
STOP_TIMEOUT_SECONDS = 180
BOOT_TIMEOUT_SECONDS = 5 * 60
DESKTOP_TIMEOUT_SECONDS = 3 * 60
DESKTOP_POLL_SECONDS = 5
SHORT_POLL_SECONDS = 1
SECONDS_PER_MINUTE = 60


class SetupError(WnError):
	"""Raised when wn setup cannot create or install the VM."""


@dataclasses.dataclass(frozen=True)
class SetupOptions:
	name: str
	username: str
	bundle_dir: str
	downloads_dir: str
	password: str = ""
	cpus: int = DEFAULT_OPTIONS["cpu_count"]
	memory_mib: int = DEFAULT_OPTIONS["memory_mib"]
	disk_gib: int = 64
	headless: bool = False
	software: Tuple[str, ...] = (SOFTWARE_NVDA,)
	edition: str = "Pro"
	qmp_port: int = DEFAULT_OPTIONS["qmp_port"]
	qmp_session_port: Optional[int] = None
	mac_address: Optional[str] = None
	battery: bool = True
	battery_file: Optional[str] = None


def install_agent():
	return install_launch_agent(sys.executable, REPOSITORY_DIRECTORY)


def restart_utm(run=subprocess.run, sleep=time.sleep, clock=time.monotonic):
	"""Quit UTM by PID if it runs, then open it in the background so it rereads every bundle."""
	result = run(["pgrep", "-x", UTM_PROCESS_NAME], capture_output=True, text=True)
	for pid in result.stdout.split():
		run(["kill", pid], capture_output=True)
		deadline = clock() + UTM_QUIT_TIMEOUT_SECONDS
		while run(["kill", "-0", pid], capture_output=True).returncode == 0:
			if clock() > deadline:
				raise SetupError("UTM (PID {}) did not quit within {} s. Quit it and run wn setup again".format(pid, UTM_QUIT_TIMEOUT_SECONDS))
			sleep(SHORT_POLL_SECONDS / 4)
	run(["open", "-g", UTM_APP_PATH], check=True)


class Installer:
	"""Runs every step of ``wn setup``. The collaborators are parameters so tests can replace them."""

	def __init__(self, options, output, backend=None, fetch=fetch_all, build_payload=build_payload_iso, restart=restart_utm,
			qmp_client_factory=qmp_client_for, clock=time.monotonic, sleep=time.sleep, install_agent=install_agent):
		self.options = options
		self.output = output
		self.backend = backend or UTMBackend({"vm_name": options.name, "qmp_port": options.qmp_port, "qmp_session_port": options.qmp_session_port})
		self.fetch = fetch
		self.build_payload = build_payload
		self.restart = restart
		self.qmp_client_factory = qmp_client_factory
		self.clock = clock
		self.sleep = sleep
		self.install_agent = install_agent
		self.started_at = clock()
		self.bundle = os.path.join(os.path.expanduser(options.bundle_dir), options.name + BUNDLE_SUFFIX)
		self.battery_file = options.battery_file or default_battery_file(options.name)

	def report(self, message):
		elapsed = int(self.clock() - self.started_at)
		self.output.write("[{:02d}:{:02d}] {}\n".format(elapsed // SECONDS_PER_MINUTE, elapsed % SECONDS_PER_MINUTE, message))
		self.output.flush()

	def run(self):
		self.check_ready()
		downloads = self.fetch(self.options.downloads_dir, include_nvda=SOFTWARE_NVDA in self.options.software, report=self.report)
		self.create_bundle(downloads)
		self.report("Restarting UTM so it finds the new VM")
		self.restart_utm_and_wait()
		self.report("Starting the VM. Windows Setup runs on its own from here")
		self.backend.start()
		self.press_keys_at_boot_prompt()
		self.wait_for_install()
		if not self.options.headless:
			self.switch_to_final_display()
		else:
			self.finish_bundle(display=None)
		address = self.wait_for_ip()
		self.write_config_if_absent()
		if self.options.battery:
			self.start_battery_agent()
		self.print_summary(address)
		return address

	def check_ready(self):
		if not os.path.isdir(UTM_APP_PATH):
			raise SetupError("UTM is not installed. Get it from https://mac.getutm.app and put it in /Applications")
		if self.backend.exists():
			raise SetupError("UTM already has a VM named '{}'. Pick another name, or delete that VM first".format(self.options.name))
		if os.path.exists(self.bundle):
			raise SetupError("{} already exists. Remove it first or pick another name".format(self.bundle))
		running = [name for _, state, name in self.backend.machines() if state != "stopped"]
		if running:
			raise SetupError("wn setup restarts UTM, which would stop these VMs: {}. Stop them first".format(", ".join(running)))

	def create_bundle(self, downloads):
		self.report("Creating the VM bundle {}".format(self.bundle))
		options = self.options
		bundle_options = {
			"name": options.name,
			"mac_address": options.mac_address or random_mac_address(),
			"cpu_count": options.cpus,
			"memory_mib": options.memory_mib,
			"iso_image": WINDOWS_ISO_IMAGE,
			"driver_iso_image": PAYLOAD_IMAGE,
			"headless": options.headless,
			"display": SETUP_DISPLAY,
			"audio": AUDIO_COREAUDIO,
			"audio_args": FINAL_AUDIO_ARGS,
			"qmp_port": options.qmp_port,
			"qmp_session_port": options.qmp_session_port,
			"battery": options.battery,
			"battery_file": self.battery_file if options.battery else None,
			"secure_boot_cert": SECURE_BOOT_CERT if options.battery else None,
		}
		try:
			write_bundle(os.path.dirname(self.bundle), bundle_options)
			if options.battery:
				prepare_battery_file(self.battery_file)
			data = os.path.join(self.bundle, DATA_DIRECTORY_NAME)
			create_empty_qcow2(os.path.join(data, DISK_IMAGE), options.disk_gib)
			shutil.copyfile(downloads.windows_iso, os.path.join(data, WINDOWS_ISO_IMAGE))
			self.report("Building the setup disc (answer file, drivers, guest tools{})".format(", NVDA" if downloads.nvda_installer else ""))
			self.build_payload(os.path.join(data, PAYLOAD_IMAGE), self.unattend_options(), downloads.virtio_iso, downloads.guest_tools_iso, downloads.nvda_installer, battery=options.battery)
		except BaseException as error:
			shutil.rmtree(self.bundle, ignore_errors=True)
			if isinstance(error, OSError):
				raise SetupError("cannot create {}: {}".format(self.bundle, error)) from None
			raise

	def unattend_options(self):
		options = self.options
		return UnattendOptions(
			username=options.username,
			password=options.password,
			computer_name=computer_name_for(options.name),
			edition=options.edition,
			time_zone=mac_time_zone(),
		)

	def restart_utm_and_wait(self):
		self.restart()
		deadline = self.clock() + UTM_REGISTER_TIMEOUT_SECONDS
		while True:
			try:
				if self.backend.exists():
					return
			except UTMCtlError:
				pass
			if self.clock() > deadline:
				raise SetupError("UTM did not list the VM '{}' within {} s. Open UTM and check it".format(self.options.name, UTM_REGISTER_TIMEOUT_SECONDS))
			self.sleep(SHORT_POLL_SECONDS)

	def _connect_qmp(self, timeout_seconds):
		deadline = self.clock() + timeout_seconds
		address = self.backend.qmp_socket_path()
		while True:
			client = self.qmp_client_factory(address)
			try:
				client.connect()
				return client
			except QMPConnectionError:
				if self.clock() > deadline:
					raise SetupError("QEMU's QMP server at {} did not open within {} s".format(address, timeout_seconds)) from None
				self.sleep(BOOT_KEY_INTERVAL_SECONDS)

	def press_keys_at_boot_prompt(self):
		"""Answer "Press any key to boot from CD or DVD" by pressing Return every half second for a while."""
		client = self._connect_qmp(QMP_OPEN_TIMEOUT_SECONDS)
		try:
			burst_end = self.clock() + BOOT_KEY_BURST_SECONDS
			while self.clock() < burst_end:
				client.execute("send-key", {"keys": [{"type": "qcode", "data": "ret"}]})
				self.sleep(BOOT_KEY_INTERVAL_SECONDS)
		finally:
			client.close()

	def _guest_ip(self):
		try:
			return self.backend.ip()
		except UTMCtlError:
			return None

	def _setup_marker(self):
		try:
			return self.backend.pull_file(SETUP_DONE_MARKER).strip()
		except UTMCtlError:
			return None

	def _check_still_running(self):
		state = self.backend.status().state
		if state == STOPPED_STATE:
			raise SetupError("the VM stopped during setup. Open it in UTM to see the screen")

	def wait_for_install(self):
		deadline = self.clock() + INSTALL_TIMEOUT_SECONDS
		phase = None
		last_report = self.clock()
		while True:
			address = self._guest_ip()
			marker = self._setup_marker() if address else None
			if marker == SETUP_DONE_TEXT:
				self.report("Windows is installed and has restarted once")
				return
			if marker == SETUP_REBOOTING_TEXT:
				new_phase = "Restarting into the finished system"
			elif address:
				new_phase = "Guest tools are running (IP {}). Finishing the first sign-in steps".format(address)
			else:
				new_phase = "Windows Setup is installing. This takes 15 to 25 minutes"
			if new_phase != phase or self.clock() - last_report >= HEARTBEAT_SECONDS:
				self.report(new_phase)
				phase = new_phase
				last_report = self.clock()
			if self.clock() > deadline:
				raise SetupError("setup did not finish within {} minutes. Open the VM in UTM to see the screen".format(INSTALL_TIMEOUT_SECONDS // SECONDS_PER_MINUTE))
			self._check_still_running()
			self.sleep(MARKER_POLL_SECONDS if address else IP_POLL_SECONDS)

	def stop_and_wait(self):
		self.backend.stop()
		deadline = self.clock() + STOP_TIMEOUT_SECONDS
		forced = False
		while self.backend.status().state != STOPPED_STATE:
			if self.clock() > deadline and not forced:
				self.report("Windows did not shut down within {} s. Powering off".format(STOP_TIMEOUT_SECONDS))
				self.backend.stop(force=True)
				forced = True
			self.sleep(SHORT_POLL_SECONDS)

	def finish_bundle(self, display):
		self.report("Shutting down to remove the install discs{}".format(" and switch the display to {}".format(display) if display else ""))
		self.stop_and_wait()
		detach_drives(self.bundle, [WINDOWS_ISO_IMAGE, PAYLOAD_IMAGE])
		if display:
			set_display_hardware(self.bundle, display)
		self.restart_utm_and_wait()
		self.report("Starting the finished VM")
		self.backend.start()

	def wait_for_ip(self):
		deadline = self.clock() + BOOT_TIMEOUT_SECONDS
		while True:
			address = self._guest_ip()
			if address:
				return address
			if self.clock() > deadline:
				raise SetupError("the VM did not report an IP address within {} s after starting".format(BOOT_TIMEOUT_SECONDS))
			self.sleep(SHORT_POLL_SECONDS)

	def desktop_drawn(self):
		deadline = self.clock() + DESKTOP_TIMEOUT_SECONDS
		with tempfile.TemporaryDirectory(prefix="wn-setup-") as directory:
			destination = os.path.join(directory, "screen.ppm")
			while True:
				try:
					with self.qmp_client_factory(self.backend.qmp_socket_path()) as client:
						take_screendump(client, destination)
					with open(destination, "rb") as screen:
						if looks_drawn(screen.read()):
							return True
				except (QMPConnectionError, QMPError, OSError, ValueError):
					pass
				if self.clock() > deadline:
					return False
				self.sleep(DESKTOP_POLL_SECONDS)

	def switch_to_final_display(self):
		"""Drop ramfb so Windows has one display, which removes the invisible second screen after viogpudo."""
		self.finish_bundle(FINAL_DISPLAY)
		self.wait_for_ip()
		if self.desktop_drawn():
			self.report("The desktop draws on {}".format(FINAL_DISPLAY))
			return
		self.report("The screen stayed blank on {}. Going back to {}".format(FINAL_DISPLAY, SETUP_DISPLAY))
		self.stop_and_wait()
		set_display_hardware(self.bundle, SETUP_DISPLAY)
		self.restart_utm_and_wait()
		self.backend.start()

	def write_config_if_absent(self):
		path = config_path()
		if os.path.exists(path):
			return
		os.makedirs(os.path.dirname(path), exist_ok=True)
		values = {"backend": "utm", "vm_name": self.options.name, "qmp_port": self.options.qmp_port}
		with open(path, "w") as config_file:
			json.dump(values, config_file, indent="\t")
			config_file.write("\n")
		self.report("Wrote {}".format(path))

	def start_battery_agent(self):
		"""Install and start the launch agent that writes the Mac's battery to the battery file."""
		path = self.install_agent()
		agent_file = battery_file_path(load_config())
		for line in (
			"Installed the wn agent and started it: {}".format(path),
			"It writes the Mac's battery status to {} for the battery in Windows.".format(agent_file),
			"Log: {}".format(os.path.expanduser(LOG_PATH)),
			"To remove it: python3 -m wn agent --uninstall",
		):
			self.output.write(line + "\n")
		if agent_file != self.battery_file:
			self.output.write("Warning: the VM reads {}, but the agent writes {}. Set vm_name or battery_file in {}\n".format(self.battery_file, agent_file, config_path()))

	def print_summary(self, address):
		self.report("Done")
		name_option = "" if self.options.name == "Windows" else " (vm_name in {} is '{}')".format(config_path(), self.options.name)
		for line in (
			"",
			"VM name: {}".format(self.options.name),
			"IP address: {}".format(address),
			"User: {}".format(self.options.username),
			"",
			"Use it with wn{}:".format(name_option),
			"  python3 -m wn status",
			"  python3 -m wn stop",
			"  python3 -m wn start",
			"  python3 -m wn key win+r",
			"  python3 -m wn type notepad",
			"The UTM window shows the screen. Command acts as the Windows key there.",
		):
			self.output.write(line + "\n")
