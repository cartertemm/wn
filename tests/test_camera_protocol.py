import os
import queue
import signal
import struct
import subprocess
import tempfile
import threading
import time
import unittest

from fake_usbredir_guest import (
	BULK_PACKET,
	CANCEL_DATA_PACKET,
	CONFIGURATION_STATUS,
	DEVICE_CONNECT,
	DEVICE_DISCONNECT,
	EP_INFO,
	HELLO,
	INTERFACE_INFO,
	RESET,
	SET_CONFIGURATION,
	STATUS_CANCELLED,
	STATUS_STALL,
	STATUS_SUCCESS,
	FakeUsbredirGuest,
)
from fakes import clang_works, short_temporary_directory
from wn.appbundle import camera_build_command

DEVICE_DESCRIPTOR = bytes.fromhex("12 01 00 02 ef 02 01 40 09 12 01 00 00 01 01 02 00 01")
CONFIGURATION_DESCRIPTOR = bytes.fromhex(
	"09 02 bd 00 02 01 00 80 fa"
	" 08 0b 00 02 0e 03 00 02"
	" 09 04 00 00 00 0e 01 00 02"
	" 0d 24 01 10 01 28 00 00 6c dc 02 01 01"
	" 12 24 02 01 01 02 00 00 00 00 00 00 00 00 03 00 00 00"
	" 09 24 03 02 01 01 00 01 00"
	" 09 04 01 00 01 0e 02 00 00"
	" 0e 24 01 01 6b 00 81 00 02 00 00 00 01 00"
	" 1b 24 04 01 02 59 55 59 32 00 00 10 00 80 00 00 aa 00 38 9b 71 10 01 00 00 00 00"
	" 1e 24 05 01 00 00 05 d0 02 00 00 5e 1a 00 00 5e 1a 00 20 1c 00 15 16 05 00 01 15 16 05 00"
	" 1e 24 05 02 00 80 02 e0 01 00 00 ca 08 00 00 ca 08 00 60 09 00 15 16 05 00 01 15 16 05 00"
	" 06 24 0d 01 01 01"
	" 07 05 81 02 00 02 00"
)
PRODUCT_STRING = bytes([22, 3]) + "Mac Camera".encode("utf-16-le")
FRAME_INTERVAL = 333333
REQUEST_LENGTH = 65536
SOCKET_TIMEOUT_SECONDS = 10
CONNECT_TIMEOUT_SECONDS = 10
IDLE_SECONDS = 3
CAPTURE_RETRY_SECONDS = 2
PATTERN_REFUSALS = 2
EXIT_TIMEOUT_SECONDS = 5
HELLO_TIMEOUT_SECONDS = 5
RECONNECT_SECONDS = 2
EXIT_USAGE = 64
SET_FEATURE = 0x03
CLEAR_FEATURE = 0x01
ENDPOINT_HALT = 0x0000
STREAMING_ENDPOINT = 0x0081
GET_DESCRIPTOR = 0x06
SET_CUR = 0x01
GET_CUR = 0x81
PROBE = 0x0100
COMMIT = 0x0200
STREAMING_INTERFACE = 0x0001
PROBE_SIZE = 34
FRAME_SIZES = {1: (1280, 720), 2: (640, 480)}
LAST_PAYLOAD_SIZES = {1: 8306, 2: 24614}
FULL_PAYLOAD = 65534
END_OF_FRAME = 0x02
FRAME_ID = 0x01


def probe_bytes(frame_index):
	return struct.pack("<HBBI", 0, 1, frame_index, FRAME_INTERVAL).ljust(PROBE_SIZE, b"\0")


@unittest.skipUnless(clang_works(), "clang is not available")
class CameraProtocolTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.build_directory = tempfile.TemporaryDirectory()
		cls.binary = os.path.join(cls.build_directory.name, "wn-camera")
		build = subprocess.run(camera_build_command(cls.binary), capture_output=True, text=True)
		if build.returncode != 0 or build.stderr:
			cls.build_directory.cleanup()
			raise AssertionError("wn-camera did not build cleanly:\n" + build.stderr)

	@classmethod
	def tearDownClass(cls):
		cls.build_directory.cleanup()

	def setUp(self):
		directory = short_temporary_directory()
		self.addCleanup(directory.cleanup)
		path = os.path.join(directory.name, "c.sock")
		self.guest = FakeUsbredirGuest(path)
		self.addCleanup(self.guest.close)
		self.process = subprocess.Popen([self.binary, "--socket", path, "--test-pattern"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
		self.addCleanup(self.stop_process)
		self.log_lines = queue.Queue()
		threading.Thread(target=self.read_log, daemon=True).start()
		self.guest.accept(CONNECT_TIMEOUT_SECONDS)

	def read_log(self):
		for line in self.process.stderr:
			self.log_lines.put((time.monotonic(), line.rstrip("\n")))

	def stop_process(self):
		if self.process.poll() is None:
			self.process.kill()
		self.process.wait()
		self.process.stderr.close()

	def wait_for_log(self, text, timeout):
		deadline = time.monotonic() + timeout
		while True:
			try:
				when, line = self.log_lines.get(timeout=max(0, deadline - time.monotonic()))
			except queue.Empty:
				self.fail("wn-camera did not log '{}' within {} s".format(text, timeout))
			if text in line:
				return when

	def plug(self):
		hello = self.guest.read_packet()
		self.guest.send_hello()
		return hello, [self.guest.read_packet() for _ in range(3)]

	def configure(self):
		self.plug()
		self.guest.send(SET_CONFIGURATION, 77, bytes([1]))
		types = [self.guest.read_packet()[0] for _ in range(3)]
		self.assertEqual(types, [EP_INFO, INTERFACE_INFO, CONFIGURATION_STATUS])

	def commit(self, frame_index):
		self.assertEqual(self.guest.control(0x21, SET_CUR, PROBE, STREAMING_INTERFACE, PROBE_SIZE, probe_bytes(frame_index))[0], STATUS_SUCCESS)
		status, probe = self.guest.control(0xA1, GET_CUR, PROBE, STREAMING_INTERFACE, PROBE_SIZE)
		self.assertEqual(status, STATUS_SUCCESS)
		self.assertEqual(probe[3], frame_index)
		width, height = FRAME_SIZES[frame_index]
		self.assertEqual(struct.unpack_from("<I", probe, 18)[0], width * height * 2)
		self.assertEqual(struct.unpack_from("<I", probe, 22)[0], REQUEST_LENGTH)
		self.assertEqual(self.guest.control(0x21, SET_CUR, COMMIT, STREAMING_INTERFACE, PROBE_SIZE, probe_bytes(frame_index))[0], STATUS_SUCCESS)

	def read_frame(self, first_id):
		"""Request payloads one at a time until a payload with EOF arrives. Return the payloads."""
		payloads = []
		packet_id = first_id
		while not payloads or not payloads[-1][1] & END_OF_FRAME:
			self.guest.bulk_request(packet_id, REQUEST_LENGTH)
			answer_id, status, data = self.guest.read_bulk()
			self.assertEqual((answer_id, status), (packet_id, STATUS_SUCCESS))
			payloads.append(data)
			packet_id += 1
		return payloads

	def check_frame(self, payloads, frame_index):
		width, height = FRAME_SIZES[frame_index]
		self.assertEqual([len(payload) for payload in payloads[:-1]], [FULL_PAYLOAD] * (len(payloads) - 1))
		self.assertEqual(len(payloads[-1]), LAST_PAYLOAD_SIZES[frame_index])
		self.assertEqual(sum(len(payload) - 2 for payload in payloads), width * height * 2)
		frame_id = payloads[0][1] & FRAME_ID
		for payload in payloads:
			self.assertEqual(payload[0], 2)
			self.assertEqual(payload[1] & ~END_OF_FRAME, 0x80 | frame_id)
		self.assertEqual([payload[1] & END_OF_FRAME for payload in payloads], [0] * (len(payloads) - 1) + [END_OF_FRAME])
		self.assertEqual(len(set(b"".join(payload[2:] for payload in payloads))), 1)
		return frame_id

	def test_handshake_order_and_caps(self):
		(hello_type, _, hello), packets = self.plug()
		self.assertEqual(hello_type, HELLO)
		self.assertTrue(hello[:64].startswith(b"wn-camera"))
		self.assertEqual(struct.unpack_from("<I", hello, 64)[0], 0x7E)
		self.assertEqual([packet[0] for packet in packets], [INTERFACE_INFO, EP_INFO, DEVICE_CONNECT])
		interface_info, ep_info, connect = (packet[2] for packet in packets)
		self.assertEqual(struct.unpack_from("<I", interface_info)[0], 2)
		self.assertEqual((interface_info[4:6], interface_info[36:38], interface_info[68:70]), (bytes([0, 1]), bytes([0x0E, 0x0E]), bytes([1, 2])))
		self.assertEqual(len(ep_info), 160)
		self.assertEqual((ep_info[0], ep_info[16], ep_info[17]), (0, 0, 2))
		self.assertEqual(set(ep_info[index] for index in range(32) if index not in (0, 16, 17)), {255})
		self.assertEqual(ep_info[64 + 17], 1)
		self.assertEqual(struct.unpack_from("<H", ep_info, 96 + 2 * 17)[0], 512)
		self.assertEqual(connect, struct.pack("<BBBBHHH", 2, 0xEF, 0x02, 0x01, 0x1209, 0x0001, 0x0100))

	def test_descriptors_match_the_requirements(self):
		self.plug()
		self.assertEqual(len(CONFIGURATION_DESCRIPTOR), 189)
		self.assertEqual(self.guest.control(0x80, GET_DESCRIPTOR, 0x0100, 0, 64), (STATUS_SUCCESS, DEVICE_DESCRIPTOR))
		self.assertEqual(self.guest.control(0x80, GET_DESCRIPTOR, 0x0200, 0, 255), (STATUS_SUCCESS, CONFIGURATION_DESCRIPTOR))
		self.assertEqual(self.guest.control(0x80, GET_DESCRIPTOR, 0x0200, 0, 9), (STATUS_SUCCESS, CONFIGURATION_DESCRIPTOR[:9]))
		self.assertEqual(self.guest.control(0x80, GET_DESCRIPTOR, 0x0300, 0, 255), (STATUS_SUCCESS, bytes([4, 3, 9, 4])))
		self.assertEqual(self.guest.control(0x80, GET_DESCRIPTOR, 0x0302, 0x0409, 255), (STATUS_SUCCESS, PRODUCT_STRING))
		self.assertEqual(self.guest.control(0x80, GET_DESCRIPTOR, 0x03EE, 0, 18), (STATUS_STALL, b""))

	def test_streams_both_sizes_with_fid_toggle(self):
		self.configure()
		next_id = 1000
		for frame_index in (2, 1):
			self.commit(frame_index)
			first = self.read_frame(next_id)
			next_id += len(first)
			second = self.read_frame(next_id)
			next_id += len(second)
			self.assertNotEqual(self.check_frame(first, frame_index), self.check_frame(second, frame_index))

	def test_held_requests_are_answered_in_order(self):
		self.configure()
		self.commit(2)
		ids = list(range(500, 508))
		for packet_id in ids:
			self.guest.bulk_request(packet_id, REQUEST_LENGTH)
		answers = [self.guest.read_bulk() for _ in ids]
		self.assertEqual([answer[0] for answer in answers], ids)
		self.assertTrue(all(status == STATUS_SUCCESS and len(data) > 2 for _, status, data in answers))

	def test_short_request_is_answered_and_does_not_block_the_queue(self):
		self.configure()
		self.commit(2)
		self.guest.bulk_request(599, 1)
		self.guest.bulk_request(600, 4)
		self.guest.bulk_request(601, REQUEST_LENGTH)
		self.assertEqual(self.guest.read_bulk(), (599, STATUS_SUCCESS, b""))
		answer_id, status, data = self.guest.read_bulk()
		self.assertEqual((answer_id, status, len(data), data[0]), (600, STATUS_SUCCESS, 2, 2))
		self.assertEqual(data[1] & ~FRAME_ID, 0x80)
		answer_id, status, data = self.guest.read_bulk()
		self.assertEqual((answer_id, status), (601, STATUS_SUCCESS))
		self.assertGreater(len(data), 2)

	def test_cancel_is_answered_once(self):
		self.configure()
		self.guest.bulk_request(0x5000, REQUEST_LENGTH)
		self.guest.send(CANCEL_DATA_PACKET, 0x5000)
		self.assertEqual(self.guest.read_bulk(), (0x5000, STATUS_CANCELLED, b""))
		self.commit(2)
		self.guest.bulk_request(0x5001, REQUEST_LENGTH)
		self.assertEqual(self.guest.read_bulk()[:2], (0x5001, STATUS_SUCCESS))

	def test_camera_turns_off_3_s_after_the_last_read(self):
		self.configure()
		self.commit(2)
		self.read_frame(1)
		last_read = time.monotonic()
		off = self.wait_for_log("camera off", IDLE_SECONDS + 3)
		self.assertGreaterEqual(off - last_read, IDLE_SECONDS - 0.2)
		self.assertLess(off - last_read, IDLE_SECONDS + 1.5)

	def drain_log(self):
		lines = []
		while not self.log_lines.empty():
			lines.append(self.log_lines.get()[1])
		return lines

	def test_stream_recovers_after_the_source_stops(self):
		self.configure()
		self.commit(2)
		next_id = 700
		next_id += len(self.read_frame(next_id))
		self.process.send_signal(signal.SIGUSR1)
		self.wait_for_log("camera stopped", EXIT_TIMEOUT_SECONDS)
		held = time.monotonic()
		self.check_frame(self.read_frame(next_id), 2)
		recovered = time.monotonic() - held
		self.assertGreater(recovered, IDLE_SECONDS + 0.5)
		self.assertLess(recovered, PATTERN_REFUSALS * CAPTURE_RETRY_SECONDS + 1.5)
		self.wait_for_log("camera recovered", EXIT_TIMEOUT_SECONDS)
		time.sleep(0.5)
		lines = self.drain_log()
		self.assertFalse([line for line in lines if "camera off" in line or "camera on" in line], lines)

	def test_held_requests_are_cancelled_by_set_configuration(self):
		self.configure()
		self.guest.bulk_request(800, REQUEST_LENGTH)
		self.guest.bulk_request(801, REQUEST_LENGTH)
		self.guest.send(SET_CONFIGURATION, 78, bytes([1]))
		self.assertEqual([self.guest.read_bulk() for _ in range(2)], [(800, STATUS_CANCELLED, b""), (801, STATUS_CANCELLED, b"")])
		self.assertEqual([self.guest.read_packet()[0] for _ in range(3)], [EP_INFO, INTERFACE_INFO, CONFIGURATION_STATUS])

	def test_held_requests_are_cancelled_by_reset(self):
		self.configure()
		self.guest.bulk_request(810, REQUEST_LENGTH)
		self.guest.bulk_request(811, REQUEST_LENGTH)
		self.guest.send(RESET)
		self.assertEqual([self.guest.read_bulk() for _ in range(2)], [(810, STATUS_CANCELLED, b""), (811, STATUS_CANCELLED, b"")])

	def test_bulk_stalls_while_the_endpoint_is_halted(self):
		self.configure()
		self.commit(2)
		self.assertEqual(self.guest.control(0x02, SET_FEATURE, ENDPOINT_HALT, STREAMING_ENDPOINT, 0), (STATUS_SUCCESS, b""))
		self.guest.bulk_request(820, REQUEST_LENGTH)
		self.assertEqual(self.guest.read_bulk(), (820, STATUS_STALL, b""))
		self.assertEqual(self.guest.control(0x02, CLEAR_FEATURE, ENDPOINT_HALT, STREAMING_ENDPOINT, 0), (STATUS_SUCCESS, b""))
		self.guest.bulk_request(821, REQUEST_LENGTH)
		self.assertEqual(self.guest.read_bulk()[:2], (821, STATUS_SUCCESS))

	def test_connects_again_when_qemu_sends_no_hello(self):
		self.assertEqual(self.guest.read_packet()[0], HELLO)
		started = time.monotonic()
		with self.assertRaises(EOFError):
			self.guest.read_packet()
		self.assertGreaterEqual(time.monotonic() - started, HELLO_TIMEOUT_SECONDS - 0.5)
		self.wait_for_log("no hello from QEMU", EXIT_TIMEOUT_SECONDS)
		self.guest.accept(CONNECT_TIMEOUT_SECONDS)
		self.assertGreaterEqual(time.monotonic() - started, HELLO_TIMEOUT_SECONDS + RECONNECT_SECONDS - 0.5)
		self.assertEqual(self.guest.read_packet()[0], HELLO)

	def test_bad_arguments_exit_64(self):
		for arguments in ([], ["--socket"], ["--socket", "x", "--bogus"]):
			result = subprocess.run([self.binary] + arguments, capture_output=True, text=True, timeout=EXIT_TIMEOUT_SECONDS)
			self.assertEqual(result.returncode, EXIT_USAGE, arguments)
			self.assertIn("usage:", result.stderr)

	def test_sigterm_unplugs_and_exits_0(self):
		self.plug()
		self.process.send_signal(signal.SIGTERM)
		self.assertEqual(self.guest.read_packet()[0], DEVICE_DISCONNECT)
		self.assertEqual(self.process.wait(timeout=EXIT_TIMEOUT_SECONDS), 0)

	def test_connects_again_after_qemu_closes_the_socket(self):
		self.plug()
		self.guest.connection.close()
		self.guest.connection = None
		self.guest.accept(CONNECT_TIMEOUT_SECONDS)
		self.assertEqual(self.guest.read_packet()[0], HELLO)


if __name__ == "__main__":
	unittest.main()
