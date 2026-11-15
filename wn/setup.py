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

from wn.agent import LOG_PATH, REPOSITORY_DIRECTORY, install_launch_agent, launch_agent_path
from wn.backends.utm import DEFAULT_VM_NAME, UTMBackend, UTMCtlError
from wn.battery_file import battery_file_path, prepare_battery_file
from wn.config import config_path, load_config, update_config_file
from wn.downloads import BYTES_PER_GB, fetch_all
from wn.errors import WnError
from wn.payload import BATTERY_DRIVER, build_payload_iso
from wn.qcow2 import create_empty_qcow2
from wn.qmp import QMPConnectionError, QMPError, qmp_client_for
from wn.screendump import looks_drawn, take_screendump
from wn.unattend import DEFAULT_EDITION, SETUP_DONE_MARKER, SETUP_DONE_TEXT, SETUP_REBOOTING_TEXT, UnattendOptions, computer_name_for, mac_time_zone
from wn.utm_config import (
	BUNDLE_SUFFIX,
	DATA_DIRECTORY_NAME,
	DEFAULT_OPTIONS,
	camera_socket_file,
	check_qmp_socket_files,
	default_battery_file,
	detach_drives,
	random_mac_address,
	set_display_hardware,
	write_bundle,
)

UTM_APP_PATH = "/Applications/UTM.app"
UTM_PROCESS_NAME = "UTM"
SETUP_DISPLAY = "virtio-ramfb"
FINAL_DISPLAY = "virtio-gpu-pci"
FALLBACK_DISPLAY = "ramfb"
WINDOWS_ISO_IMAGE = "windows11-arm64.iso"
PAYLOAD_IMAGE = "wnsetup.iso"
DISK_IMAGE = DEFAULT_OPTIONS["disk_image"]
SOFTWARE_NVDA = "nvda"
SOFTWARE_CHOICES = ((SOFTWARE_NVDA, "NVDA screen reader"),)
STOPPED_STATE = "stopped"
DEFAULT_DISK_GIB = 64
# Measured peak near 40 GB: an 8.1 GB download cache, the 8.5 GB ISO copy, and the disk image during install.
DOWNLOAD_SPACE_BYTES = 9 * BYTES_PER_GB
BUNDLE_SPACE_BYTES = 41 * BYTES_PER_GB

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
CAMERA_PROMPT_TEXT = "macOS will ask whether wn-agent may use the camera. Click Allow."


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
	disk_gib: int = DEFAULT_DISK_GIB
	headless: bool = False
	software: Tuple[str, ...] = (SOFTWARE_NVDA,)
	edition: str = DEFAULT_EDITION
	mac_address: Optional[str] = None
	battery: bool = True
	battery_file: Optional[str] = None
	camera: bool = True


def nearest_existing_directory(path):
	path = os.path.abspath(os.path.expanduser(path))
	while not os.path.exists(path):
		path = os.path.dirname(path)
	return path


def directory_file_bytes(path):
	if not os.path.isdir(path):
		return 0
	with os.scandir(path) as entries:
		return sum(entry.stat().st_size for entry in entries if entry.is_file())


def clone_file(source, destination, run=subprocess.run):
	"""Copy with an APFS clone, which takes no space until either file changes. Fall back to a plain copy."""
	if run(["cp", "-c", source, destination], capture_output=True).returncode != 0:
		shutil.copyfile(source, destination)


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
		self.backend = backend or UTMBackend({"vm_name": options.name})
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
		self.update_agent()
		self.print_summary(address)
		return address

	def check_ready(self):
		if not os.path.isdir(UTM_APP_PATH):
			raise SetupError("UTM is not installed. Get it from https://mac.getutm.app and put it in /Applications")
		check_qmp_socket_files(self.options.name)
		if self.backend.exists():
			raise SetupError("UTM already has a VM named '{}'. Pick another name, or delete that VM first".format(self.options.name))
		if os.path.exists(self.bundle):
			raise SetupError("{} already exists. Remove it first or pick another name".format(self.bundle))
		self.check_no_running_vms("Stop them first")
		self.check_free_space()

	def check_no_running_vms(self, advice):
		running = [name for _, state, name in self.backend.machines() if state != STOPPED_STATE]
		if running:
			raise SetupError("wn setup restarts UTM, which would stop these VMs: {}. {}".format(", ".join(running), advice))

	def check_free_space(self):
		downloads = nearest_existing_directory(self.options.downloads_dir)
		bundles = nearest_existing_directory(self.options.bundle_dir)
		cached = directory_file_bytes(os.path.abspath(os.path.expanduser(self.options.downloads_dir)))
		needed = {downloads: max(0, DOWNLOAD_SPACE_BYTES - cached)}
		if os.stat(downloads).st_dev == os.stat(bundles).st_dev:
			needed[downloads] += BUNDLE_SPACE_BYTES
		else:
			needed[bundles] = BUNDLE_SPACE_BYTES
		for path, needed_bytes in needed.items():
			free = shutil.disk_usage(path).free
			if free < needed_bytes:
				raise SetupError("wn setup needs {:.1f} GB free on the volume that holds {}, and it has {:.1f} GB. Free some space first".format(
					needed_bytes / BYTES_PER_GB, path, free / BYTES_PER_GB))

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
			"battery": options.battery,
			"battery_file": self.battery_file if options.battery else None,
			"secure_boot_driver": BATTERY_DRIVER if options.battery else None,
			"camera": options.camera,
		}
		try:
			write_bundle(os.path.dirname(self.bundle), bundle_options)
			if options.battery:
				prepare_battery_file(self.battery_file)
			data = os.path.join(self.bundle, DATA_DIRECTORY_NAME)
			create_empty_qcow2(os.path.join(data, DISK_IMAGE), options.disk_gib)
			clone_file(downloads.windows_iso, os.path.join(data, WINDOWS_ISO_IMAGE))
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
		self.check_no_running_vms("Setup stopped before it restarted UTM, so {} is not finished. Stop them, remove it, and run wn setup again".format(self.bundle))
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
		self.report("The screen stayed blank on {}. Switching to {}, which has one display at a fixed resolution".format(FINAL_DISPLAY, FALLBACK_DISPLAY))
		self.stop_and_wait()
		set_display_hardware(self.bundle, FALLBACK_DISPLAY)
		self.restart_utm_and_wait()
		self.backend.start()

	def write_config_if_absent(self):
		path = config_path()
		if os.path.exists(path):
			return
		os.makedirs(os.path.dirname(path), exist_ok=True)
		values = {"backend": "utm", "vm_name": self.options.name}
		with open(path, "w") as config_file:
			json.dump(values, config_file, indent="\t")
			config_file.write("\n")
		self.report("Wrote {}".format(path))

	def update_agent(self):
		vm_name = load_config().vm_name
		if not self.options.battery and not self.options.camera:
			if vm_name == self.options.name and os.path.exists(launch_agent_path()):
				self.output.write("If you no longer want the wn agent, run python3 -m wn agent --uninstall\n")
			return
		if vm_name == self.options.name:
			update_config_file({"camera": self.options.camera, "battery_sync": self.options.battery})
		else:
			self.output.write("Warning: the agent serves VM '{}', so wn setup left camera and battery_sync in {} as they were\n".format(vm_name, config_path()))
		self.start_agent()

	def start_agent(self):
		"""Install and start the launch agent that writes the Mac's battery to the battery file and runs wn-camera."""
		path = self.install_agent()
		config = load_config()
		agent_file = battery_file_path(config)
		lines = ["Installed the wn agent and started it: {}".format(path)]
		if self.options.battery:
			lines.append("It writes the Mac's battery status to {} for the battery in Windows.".format(agent_file))
		if self.options.camera:
			lines.append(CAMERA_PROMPT_TEXT)
		lines += [
			"Log: {}".format(os.path.expanduser(LOG_PATH)),
			"To remove it: python3 -m wn agent --uninstall",
		]
		for line in lines:
			self.output.write(line + "\n")
		if self.options.battery and agent_file != self.battery_file:
			self.output.write("Warning: the VM reads {}, but the agent writes {}. Set vm_name or battery_file in {}\n".format(self.battery_file, agent_file, config_path()))
		if self.options.camera and config.vm_name != self.options.name:
			self.output.write("Warning: the VM listens on {}, but the agent connects to {}. Set vm_name in {}\n".format(camera_socket_file(self.options.name), camera_socket_file(config.vm_name), config_path()))

	def print_summary(self, address):
		self.report("Done")
		name_option = "" if self.options.name == DEFAULT_VM_NAME else " (vm_name in {} is '{}')".format(config_path(), self.options.name)
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
