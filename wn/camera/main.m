#import <AVFoundation/AVFoundation.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "capture.h"
#include "usbredir.h"
#include "uvc.h"

#define EXIT_USAGE 64
#define VERSION_TEXT "wn-camera 1"
#define RECONNECT_SECONDS 2.0
#define HELLO_TIMEOUT_SECONDS 5.0
#define ACCESS_RETRY_SECONDS 10.0
#define ACCESS_POLL_SECONDS 1.0
#define POLL_MILLISECONDS 100
#define FLUSH_MILLISECONDS 1000
#define OUTPUT_BACKLOG_LIMIT (256 * 1024)
#define USB_TYPE_MASK 0x60
#define USB_TYPE_CLASS 0x20

enum access_state {
	ACCESS_ALLOWED,
	ACCESS_ASKING,
	ACCESS_DENIED,
	ACCESS_RESTRICTED,
	ACCESS_NO_CAMERA,
	ACCESS_UNKNOWN,
};

static const char *const access_messages[] = {
	[ACCESS_ALLOWED] = "allowed",
	[ACCESS_ASKING] = "not decided yet. Click Allow when macOS asks whether wn-agent may use the camera",
	[ACCESS_DENIED] = "denied. Allow wn-agent in System Settings > Privacy & Security > Camera",
	[ACCESS_RESTRICTED] = "restricted by a profile or Screen Time. Check System Settings > Privacy & Security > Camera",
	[ACCESS_NO_CAMERA] = "no camera found. Connect a camera or open the lid",
};

static const struct usbredir_interface camera_interfaces[] = {
	{UVC_CONTROL_INTERFACE, UVC_VIDEO_CLASS, UVC_VIDEOCONTROL_SUBCLASS, 0},
	{UVC_STREAMING_INTERFACE, UVC_VIDEO_CLASS, UVC_VIDEOSTREAMING_SUBCLASS, 0},
};

static const struct usbredir_endpoint camera_endpoints[] = {
	{0x00, USBREDIR_ENDPOINT_CONTROL, 0, UVC_CONTROL_INTERFACE, UVC_CONTROL_MAX_PACKET},
	{0x80, USBREDIR_ENDPOINT_CONTROL, 0, UVC_CONTROL_INTERFACE, UVC_CONTROL_MAX_PACKET},
	{UVC_STREAMING_ENDPOINT, USBREDIR_ENDPOINT_BULK, 0, UVC_STREAMING_INTERFACE, UVC_STREAMING_MAX_PACKET},
};

static const struct usbredir_device_info camera_info = {
	USBREDIR_SPEED_HIGH, UVC_DEVICE_CLASS, UVC_DEVICE_SUBCLASS, UVC_DEVICE_PROTOCOL, UVC_VENDOR_ID, UVC_PRODUCT_ID, UVC_DEVICE_BCD,
};

struct camera {
	const char *socket_path;
	bool test_pattern;
	struct usbredir_connection connection;
	struct uvc_device device;
	struct uvc_stream stream;
	struct uvc_request_queue queue;
	bool plugged;
	bool capture_on;
	uint8_t capture_frame_index;
	uint64_t frame_sequence;
	uint64_t frames_sent;
	double connected_at;
	double capture_started_at;
	double last_bulk_arrival;
	double last_access_check;
};

static volatile sig_atomic_t terminate_requested;
static int wake_pipe[2] = {-1, -1};
static enum access_state logged_access = ACCESS_UNKNOWN;

void camera_log(const char *format, ...)
{
	va_list arguments;

	va_start(arguments, format);
	fputs("wn-camera: ", stderr);
	vfprintf(stderr, format, arguments);
	fputc('\n', stderr);
	fflush(stderr);
	va_end(arguments);
}

static double now_seconds(void)
{
	struct timespec time;

	clock_gettime(CLOCK_MONOTONIC, &time);
	return time.tv_sec + time.tv_nsec / 1e9;
}

static void wake(void)
{
	char byte = 0;

	(void)write(wake_pipe[1], &byte, 1);
}

static void handle_terminate(int signal_number)
{
	(void)signal_number;
	terminate_requested = 1;
	wake();
}

static void drain_wake_pipe(void)
{
	char bytes[256];

	while (read(wake_pipe[0], bytes, sizeof bytes) > 0)
		;
}

static bool wait_seconds(double seconds)
{
	double deadline = now_seconds() + seconds;
	struct pollfd wake_fd = {wake_pipe[0], POLLIN, 0};

	while (!terminate_requested && now_seconds() < deadline) {
		poll(&wake_fd, 1, (int)((deadline - now_seconds()) * 1000) + 1);
		drain_wake_pipe();
	}
	return !terminate_requested;
}

static enum access_state camera_access(void)
{
	switch ([AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo]) {
	case AVAuthorizationStatusNotDetermined:
		return ACCESS_ASKING;
	case AVAuthorizationStatusDenied:
		return ACCESS_DENIED;
	case AVAuthorizationStatusRestricted:
		return ACCESS_RESTRICTED;
	default:
		break;
	}
	return [AVCaptureDevice defaultDeviceWithMediaType:AVMediaTypeVideo] ? ACCESS_ALLOWED : ACCESS_NO_CAMERA;
}

static void log_access(enum access_state state)
{
	if (state == logged_access)
		return;
	camera_log("access: %s", access_messages[state]);
	logged_access = state;
}

static bool wait_for_access(void)
{
	static bool asked;
	enum access_state state;

	for (;;) {
		@autoreleasepool {
			state = camera_access();
			log_access(state);
			if (state == ACCESS_ALLOWED)
				return true;
			if (state == ACCESS_ASKING && !asked) {
				asked = true;
				[AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL granted) {
					(void)granted;
					wake();
				}];
			}
		}
		if (!wait_seconds(state == ACCESS_ASKING ? ACCESS_POLL_SECONDS : ACCESS_RETRY_SECONDS))
			return false;
	}
}

static void queue_endpoint_info(struct camera *camera, bool configured)
{
	struct usbredir_connection *connection = &camera->connection;
	size_t endpoint_count = configured ? 3 : 2;
	uint32_t interface_count = configured ? 2 : 0;

	usbredir_commit(connection, usbredir_encode_ep_info(usbredir_reserve(connection, USBREDIR_EP_INFO_SIZE), camera_endpoints, endpoint_count));
	usbredir_commit(connection, usbredir_encode_interface_info(usbredir_reserve(connection, USBREDIR_INTERFACE_INFO_SIZE), camera_interfaces, interface_count));
}

static void answer_bulk(struct camera *camera, uint64_t id, uint8_t endpoint, uint8_t status)
{
	struct usbredir_connection *connection = &camera->connection;

	usbredir_commit(connection, usbredir_encode_bulk_reply(usbredir_reserve(connection, USBREDIR_BULK_REPLY_SIZE), id, endpoint, status, 0));
}

static void stop_stream(struct camera *camera)
{
	struct uvc_request request;

	while (uvc_queue_pop(&camera->queue, &request))
		answer_bulk(camera, request.id, UVC_STREAMING_ENDPOINT, USBREDIR_STATUS_CANCELLED);
	uvc_stream_drop_frame(&camera->stream);
}

static void start_capture(struct camera *camera)
{
	uint8_t frame_index = camera->device.commit.frame_index;

	if (camera->capture_on && camera->capture_frame_index == frame_index && capture_active())
		return;
	capture_start(uvc_frame_width(frame_index), uvc_frame_height(frame_index));
	if (!camera->capture_on) {
		camera->capture_started_at = now_seconds();
		camera->frames_sent = 0;
	}
	camera->capture_on = true;
	camera->capture_frame_index = frame_index;
	camera_log("camera on %ux%u", uvc_frame_width(frame_index), uvc_frame_height(frame_index));
}

static void stop_capture(struct camera *camera)
{
	if (!camera->capture_on)
		return;
	capture_stop();
	camera->capture_on = false;
	camera_log("camera off, sent %llu frames in %.1f s", (unsigned long long)camera->frames_sent, now_seconds() - camera->capture_started_at);
}

static void reset_device(struct camera *camera)
{
	stop_stream(camera);
	stop_capture(camera);
	uvc_device_reset(&camera->device);
}

static bool on_hello(struct camera *camera, const struct usbredir_header *header, const uint8_t *body)
{
	struct usbredir_connection *connection = &camera->connection;
	uint32_t caps = usbredir_decode_hello_caps(body, header->length);

	if ((caps & USBREDIR_REQUIRED_PEER_CAPS) != USBREDIR_REQUIRED_PEER_CAPS) {
		camera_log("QEMU's usb-redir has capabilities 0x%x and needs 0x%x", caps, USBREDIR_REQUIRED_PEER_CAPS);
		return false;
	}
	connection->peer_hello = true;
	usbredir_commit(connection, usbredir_encode_interface_info(usbredir_reserve(connection, USBREDIR_INTERFACE_INFO_SIZE), camera_interfaces, 2));
	usbredir_commit(connection, usbredir_encode_ep_info(usbredir_reserve(connection, USBREDIR_EP_INFO_SIZE), camera_endpoints, 3));
	usbredir_commit(connection, usbredir_encode_device_connect(usbredir_reserve(connection, USBREDIR_DEVICE_CONNECT_SIZE), &camera_info));
	camera->plugged = true;
	camera_log("plugged into the VM as USB %04x:%04x", UVC_VENDOR_ID, UVC_PRODUCT_ID);
	return true;
}

static void on_control(struct camera *camera, const struct usbredir_header *header, const uint8_t *body)
{
	struct usbredir_connection *connection = &camera->connection;
	struct usbredir_control control;
	struct uvc_reply reply;

	usbredir_decode_control(body, &control);
	uvc_handle_control(&camera->device, control.requesttype, control.request, control.value, control.index, control.length,
		body + USBREDIR_CONTROL_HEADER_SIZE, header->length - USBREDIR_CONTROL_HEADER_SIZE, &reply);
	if ((control.requesttype & USB_TYPE_MASK) == USB_TYPE_CLASS || reply.stall)
		camera_log("request %02x %02x value %04x index %04x length %u%s", control.requesttype, control.request, control.value, control.index, control.length, reply.stall ? " stalled" : "");
	control.status = reply.stall ? USBREDIR_STATUS_STALL : USBREDIR_STATUS_SUCCESS;
	if (control.endpoint & USBREDIR_DIRECTION_IN)
		control.length = reply.length;
	else if (reply.stall)
		control.length = 0;
	usbredir_commit(connection, usbredir_encode_control_reply(usbredir_reserve(connection, USBREDIR_CONTROL_REPLY_SIZE + reply.length), header->id, &control, reply.data));
	if (reply.stream_stop)
		stop_stream(camera);
	if (reply.committed) {
		camera_log("format %ux%u committed", uvc_frame_width(camera->device.commit.frame_index), uvc_frame_height(camera->device.commit.frame_index));
		if (camera->capture_on) {
			uvc_stream_drop_frame(&camera->stream);
			start_capture(camera);
		}
	}
}

static void on_bulk(struct camera *camera, const struct usbredir_header *header, const uint8_t *body)
{
	struct usbredir_bulk bulk;

	usbredir_decode_bulk(body, &bulk);
	if (bulk.endpoint != UVC_STREAMING_ENDPOINT || camera->device.configuration != UVC_CONFIGURATION_VALUE || camera->device.halted) {
		answer_bulk(camera, header->id, bulk.endpoint, USBREDIR_STATUS_STALL);
		return;
	}
	uvc_queue_push(&camera->queue, header->id, bulk.length);
	camera->last_bulk_arrival = now_seconds();
	if (camera->device.has_commit)
		start_capture(camera);
}

static void on_set_configuration(struct camera *camera, const struct usbredir_header *header, const uint8_t *body)
{
	struct usbredir_connection *connection = &camera->connection;
	uint8_t value = body[0];
	bool known = value == 0 || value == UVC_CONFIGURATION_VALUE;

	if (known) {
		stop_stream(camera);
		camera->device.configuration = value;
		camera->device.halted = false;
		queue_endpoint_info(camera, value == UVC_CONFIGURATION_VALUE);
	}
	usbredir_commit(connection, usbredir_encode_configuration_status(usbredir_reserve(connection, USBREDIR_STATUS_PACKET_SIZE), header->id,
		known ? USBREDIR_STATUS_SUCCESS : USBREDIR_STATUS_STALL, camera->device.configuration));
}

static void on_get_configuration(struct camera *camera, const struct usbredir_header *header)
{
	struct usbredir_connection *connection = &camera->connection;

	usbredir_commit(connection, usbredir_encode_configuration_status(usbredir_reserve(connection, USBREDIR_STATUS_PACKET_SIZE), header->id,
		USBREDIR_STATUS_SUCCESS, camera->device.configuration));
}

static void on_set_alt_setting(struct camera *camera, const struct usbredir_header *header, const uint8_t *body)
{
	struct usbredir_connection *connection = &camera->connection;
	uint8_t interface = body[0];
	bool known = (interface == UVC_CONTROL_INTERFACE || interface == UVC_STREAMING_INTERFACE) && body[1] == 0;

	if (known) {
		if (interface == UVC_STREAMING_INTERFACE) {
			stop_stream(camera);
			camera->device.halted = false;
		}
		queue_endpoint_info(camera, camera->device.configuration == UVC_CONFIGURATION_VALUE);
	}
	usbredir_commit(connection, usbredir_encode_alt_setting_status(usbredir_reserve(connection, USBREDIR_STATUS_PACKET_SIZE), header->id,
		known ? USBREDIR_STATUS_SUCCESS : USBREDIR_STATUS_STALL, interface, 0));
}

static void on_get_alt_setting(struct camera *camera, const struct usbredir_header *header, const uint8_t *body)
{
	struct usbredir_connection *connection = &camera->connection;
	uint8_t interface = body[0];
	bool known = interface == UVC_CONTROL_INTERFACE || interface == UVC_STREAMING_INTERFACE;

	usbredir_commit(connection, usbredir_encode_alt_setting_status(usbredir_reserve(connection, USBREDIR_STATUS_PACKET_SIZE), header->id,
		known ? USBREDIR_STATUS_SUCCESS : USBREDIR_STATUS_STALL, interface, 0));
}

static bool dispatch(struct camera *camera, const struct usbredir_header *header, const uint8_t *body)
{
	switch (header->type) {
	case USBREDIR_HELLO:
		return camera->connection.peer_hello || on_hello(camera, header, body);
	case USBREDIR_RESET:
		reset_device(camera);
		return true;
	case USBREDIR_SET_CONFIGURATION:
		on_set_configuration(camera, header, body);
		return true;
	case USBREDIR_GET_CONFIGURATION:
		on_get_configuration(camera, header);
		return true;
	case USBREDIR_SET_ALT_SETTING:
		on_set_alt_setting(camera, header, body);
		return true;
	case USBREDIR_GET_ALT_SETTING:
		on_get_alt_setting(camera, header, body);
		return true;
	case USBREDIR_CONTROL_PACKET:
		on_control(camera, header, body);
		return true;
	case USBREDIR_BULK_PACKET:
		on_bulk(camera, header, body);
		return true;
	case USBREDIR_CANCEL_DATA_PACKET:
		if (uvc_queue_remove(&camera->queue, header->id))
			answer_bulk(camera, header->id, UVC_STREAMING_ENDPOINT, USBREDIR_STATUS_CANCELLED);
		return true;
	case USBREDIR_FILTER_REJECT:
		camera_log("QEMU rejected the camera");
		return true;
	default:
		return true;
	}
}

static bool handle_packets(struct camera *camera)
{
	struct usbredir_header header;
	const uint8_t *body;
	enum usbredir_parse parse;

	while ((parse = usbredir_next_packet(&camera->connection, &header, &body)) == USBREDIR_PACKET_READY) {
		if (!dispatch(camera, &header, body))
			return false;
		usbredir_consume_packet(&camera->connection);
	}
	if (parse == USBREDIR_MALFORMED)
		camera_log("malformed usbredir packet of type %u", header.type);
	return parse != USBREDIR_MALFORMED;
}

static void send_frames(struct camera *camera)
{
	struct usbredir_connection *connection = &camera->connection;
	uint8_t frame_index = camera->device.commit.frame_index;
	size_t frame_size = uvc_frame_bytes(frame_index);
	struct uvc_request request;
	size_t room;
	size_t payload;
	uint8_t *out;

	while (camera->capture_on && usbredir_pending(connection) < OUTPUT_BACKLOG_LIMIT && uvc_queue_peek(&camera->queue, &request)) {
		if (!uvc_stream_has_frame(&camera->stream)) {
			if (!capture_copy_frame(uvc_stream_reserve(&camera->stream, frame_size), uvc_frame_width(frame_index), uvc_frame_height(frame_index), &camera->frame_sequence))
				return;
			uvc_stream_load(&camera->stream, frame_size);
		}
		uvc_queue_pop(&camera->queue, NULL);
		room = request.length < UVC_PAYLOAD_MAX ? request.length : UVC_PAYLOAD_MAX;
		out = usbredir_reserve(connection, USBREDIR_BULK_REPLY_SIZE + room);
		payload = uvc_pack_payload(&camera->stream, request.length, out + USBREDIR_BULK_REPLY_SIZE);
		usbredir_encode_bulk_reply(out, request.id, UVC_STREAMING_ENDPOINT, USBREDIR_STATUS_SUCCESS, (uint32_t)payload);
		usbredir_commit(connection, USBREDIR_BULK_REPLY_SIZE + payload);
		if (!uvc_stream_has_frame(&camera->stream))
			camera->frames_sent++;
	}
}

static void flush_before_close(struct camera *camera)
{
	double deadline = now_seconds() + FLUSH_MILLISECONDS / 1000.0;
	struct pollfd socket_fd = {camera->connection.fd, POLLOUT, 0};

	while (usbredir_pending(&camera->connection) && now_seconds() < deadline) {
		poll(&socket_fd, 1, POLL_MILLISECONDS);
		if (usbredir_send(&camera->connection) < 0)
			return;
	}
}

static void unplug(struct camera *camera)
{
	struct usbredir_connection *connection = &camera->connection;

	if (!camera->plugged)
		return;
	usbredir_commit(connection, usbredir_encode_device_disconnect(usbredir_reserve(connection, USBREDIR_HEADER_SIZE)));
	flush_before_close(camera);
	camera->plugged = false;
	camera_log("unplugged from the VM");
}

static bool check_timers(struct camera *camera)
{
	double now = now_seconds();
	enum access_state state;

	if (!camera->connection.peer_hello && now - camera->connected_at >= HELLO_TIMEOUT_SECONDS) {
		camera_log("no hello from QEMU within %.0f s, connecting again", HELLO_TIMEOUT_SECONDS);
		return false;
	}
	if (camera->capture_on && uvc_idle_expired(camera->last_bulk_arrival, now))
		stop_capture(camera);
	if (camera->capture_on && !camera->test_pattern && now - camera->last_access_check >= ACCESS_POLL_SECONDS) {
		camera->last_access_check = now;
		state = camera_access();
		if (state != ACCESS_ALLOWED) {
			log_access(state);
			stop_capture(camera);
			unplug(camera);
			return false;
		}
	}
	return true;
}

static bool service_connection(struct camera *camera)
{
	struct pollfd fds[2] = {{camera->connection.fd, POLLIN, 0}, {wake_pipe[0], POLLIN, 0}};

	if (usbredir_pending(&camera->connection))
		fds[0].events |= POLLOUT;
	if (poll(fds, 2, POLL_MILLISECONDS) < 0 && errno != EINTR)
		return false;
	if (fds[1].revents & POLLIN)
		drain_wake_pipe();
	if (fds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
		if (usbredir_receive(&camera->connection) < 0 || !handle_packets(camera))
			return false;
	}
	if (!check_timers(camera))
		return false;
	send_frames(camera);
	return usbredir_send(&camera->connection) == 0;
}

static void run_connection(struct camera *camera, int fd)
{
	struct usbredir_connection *connection = &camera->connection;

	usbredir_connection_init(connection, fd);
	uvc_device_reset(&camera->device);
	camera->plugged = false;
	camera->connected_at = now_seconds();
	camera->last_access_check = camera->connected_at;
	usbredir_commit(connection, usbredir_encode_hello(usbredir_reserve(connection, USBREDIR_HELLO_SIZE), VERSION_TEXT, USBREDIR_CAPS));
	while (!terminate_requested) {
		bool keep_going;

		@autoreleasepool {
			keep_going = service_connection(camera);
		}
		if (!keep_going)
			break;
	}
	stop_capture(camera);
	if (terminate_requested)
		unplug(camera);
	uvc_queue_clear(&camera->queue);
	uvc_stream_drop_frame(&camera->stream);
	usbredir_connection_close(connection);
}

static void run(struct camera *camera)
{
	bool waiting_logged = false;
	int fd;

	while (!terminate_requested) {
		if (!camera->test_pattern && !wait_for_access())
			return;
		fd = usbredir_connect(camera->socket_path);
		if (fd < 0) {
			if (!waiting_logged)
				camera_log("waiting for the VM at %s (%s)", camera->socket_path, strerror(errno));
			waiting_logged = true;
			if (!wait_seconds(RECONNECT_SECONDS))
				return;
			continue;
		}
		waiting_logged = false;
		camera_log("connected to %s", camera->socket_path);
		run_connection(camera, fd);
		camera_log("disconnected from %s", camera->socket_path);
		if (!wait_seconds(RECONNECT_SECONDS))
			return;
	}
}

static int usage(const char *program)
{
	fprintf(stderr, "usage: %s --socket PATH [--test-pattern]\n", program);
	return EXIT_USAGE;
}

int main(int argc, char **argv)
{
	struct camera camera = {0};

	for (int index = 1; index < argc; index++) {
		if (strcmp(argv[index], "--socket") == 0 && index + 1 < argc)
			camera.socket_path = argv[++index];
		else if (strcmp(argv[index], "--test-pattern") == 0)
			camera.test_pattern = true;
		else
			return usage(argv[0]);
	}
	if (!camera.socket_path)
		return usage(argv[0]);
	if (pipe(wake_pipe) < 0)
		return 1;
	fcntl(wake_pipe[0], F_SETFL, O_NONBLOCK);
	fcntl(wake_pipe[1], F_SETFL, O_NONBLOCK);
	signal(SIGPIPE, SIG_IGN);
	signal(SIGTERM, handle_terminate);
	signal(SIGINT, handle_terminate);
	camera_log("using the pid.codes test USB ID %04x:%04x, for local builds only", UVC_VENDOR_ID, UVC_PRODUCT_ID);
	capture_init(camera.test_pattern, wake);
	run(&camera);
	capture_finish();
	uvc_stream_free(&camera.stream);
	return 0;
}
