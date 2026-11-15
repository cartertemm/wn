#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "usbredir.h"
#include "uvc.h"

static int failures;

static void expect(bool passed, const char *condition, int line)
{
	if (!passed) {
		failures++;
		fprintf(stderr, "camera_units.c:%d: %s\n", line, condition);
	}
}

#define EXPECT(condition) expect((condition), #condition, __LINE__)

static const uint8_t expected_device_descriptor[] = {0x12, 0x01, 0x00, 0x02, 0xEF, 0x02, 0x01, 0x40, 0x09, 0x12, 0x01, 0x00, 0x00, 0x01, 0x01, 0x02, 0x00, 0x01};

static const uint8_t expected_qualifier[] = {0x0A, 0x06, 0x00, 0x02, 0xEF, 0x02, 0x01, 0x40, 0x01, 0x00};

static const uint8_t expected_configuration[] = {
	0x09, 0x02, 0xBD, 0x00, 0x02, 0x01, 0x00, 0x80, 0xFA,
	0x08, 0x0B, 0x00, 0x02, 0x0E, 0x03, 0x00, 0x02,
	0x09, 0x04, 0x00, 0x00, 0x00, 0x0E, 0x01, 0x00, 0x02,
	0x0D, 0x24, 0x01, 0x10, 0x01, 0x28, 0x00, 0x00, 0x6C, 0xDC, 0x02, 0x01, 0x01,
	0x12, 0x24, 0x02, 0x01, 0x01, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00,
	0x09, 0x24, 0x03, 0x02, 0x01, 0x01, 0x00, 0x01, 0x00,
	0x09, 0x04, 0x01, 0x00, 0x01, 0x0E, 0x02, 0x00, 0x00,
	0x0E, 0x24, 0x01, 0x01, 0x6B, 0x00, 0x81, 0x00, 0x02, 0x00, 0x00, 0x00, 0x01, 0x00,
	0x1B, 0x24, 0x04, 0x01, 0x02, 0x59, 0x55, 0x59, 0x32, 0x00, 0x00, 0x10, 0x00, 0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71, 0x10, 0x01, 0x00, 0x00, 0x00, 0x00,
	0x1E, 0x24, 0x05, 0x01, 0x00, 0x00, 0x05, 0xD0, 0x02, 0x00, 0x00, 0x5E, 0x1A, 0x00, 0x00, 0x5E, 0x1A, 0x00, 0x20, 0x1C, 0x00, 0x15, 0x16, 0x05, 0x00, 0x01, 0x15, 0x16, 0x05, 0x00,
	0x1E, 0x24, 0x05, 0x02, 0x00, 0x80, 0x02, 0xE0, 0x01, 0x00, 0x00, 0xCA, 0x08, 0x00, 0x00, 0xCA, 0x08, 0x00, 0x60, 0x09, 0x00, 0x15, 0x16, 0x05, 0x00, 0x01, 0x15, 0x16, 0x05, 0x00,
	0x06, 0x24, 0x0D, 0x01, 0x01, 0x01,
	0x07, 0x05, 0x81, 0x02, 0x00, 0x02, 0x00,
};

static uint16_t read16(const uint8_t *in)
{
	return (uint16_t)(in[0] | in[1] << 8);
}

static uint32_t read32(const uint8_t *in)
{
	return read16(in) | (uint32_t)read16(in + 2) << 16;
}

static uint64_t read64(const uint8_t *in)
{
	return read32(in) | (uint64_t)read32(in + 4) << 32;
}

static void test_hello(void)
{
	uint8_t out[USBREDIR_HELLO_SIZE];

	EXPECT(usbredir_encode_hello(out, "wn-camera 1", USBREDIR_CAPS) == 80);
	EXPECT(read32(out) == USBREDIR_HELLO);
	EXPECT(read32(out + 4) == 68);
	EXPECT(read32(out + 8) == 0);
	EXPECT(strcmp((const char *)out + 12, "wn-camera 1") == 0);
	EXPECT(out[12 + 63] == 0);
	EXPECT(read32(out + 76) == 0x7E);
	EXPECT(usbredir_decode_hello_caps(out + 12, 68) == 0x7E);
	EXPECT(usbredir_decode_hello_caps(out + 12, 64) == 0);
}

static void test_header_has_64_bit_id(void)
{
	uint8_t out[USBREDIR_HEADER_SIZE];

	EXPECT(usbredir_encode_header(out, USBREDIR_BULK_PACKET, 10, 0x1122334455667788ull) == 16);
	EXPECT(read32(out) == 101);
	EXPECT(read32(out + 4) == 10);
	EXPECT(read64(out + 8) == 0x1122334455667788ull);
}

static void test_device_connect(void)
{
	struct usbredir_device_info info = {USBREDIR_SPEED_HIGH, 0xEF, 0x02, 0x01, 0x1209, 0x0001, 0x0100};
	uint8_t out[USBREDIR_DEVICE_CONNECT_SIZE];
	const uint8_t body[] = {0x02, 0xEF, 0x02, 0x01, 0x09, 0x12, 0x01, 0x00, 0x00, 0x01};

	EXPECT(usbredir_encode_device_connect(out, &info) == 26);
	EXPECT(read32(out) == USBREDIR_DEVICE_CONNECT);
	EXPECT(read32(out + 4) == 10);
	EXPECT(memcmp(out + 16, body, sizeof body) == 0);
}

static void test_device_disconnect(void)
{
	uint8_t out[USBREDIR_HEADER_SIZE];

	EXPECT(usbredir_encode_device_disconnect(out) == 16);
	EXPECT(read32(out) == USBREDIR_DEVICE_DISCONNECT);
	EXPECT(read32(out + 4) == 0);
}

static void test_interface_info(void)
{
	const struct usbredir_interface interfaces[] = {{0, 0x0E, 0x01, 0x00}, {1, 0x0E, 0x02, 0x00}};
	uint8_t out[USBREDIR_INTERFACE_INFO_SIZE];
	uint8_t *body = out + 16;

	EXPECT(usbredir_encode_interface_info(out, interfaces, 2) == 148);
	EXPECT(read32(out) == USBREDIR_INTERFACE_INFO);
	EXPECT(read32(out + 4) == 132);
	EXPECT(read32(body) == 2);
	EXPECT(body[4] == 0 && body[5] == 1);
	EXPECT(body[36] == 0x0E && body[37] == 0x0E);
	EXPECT(body[68] == 0x01 && body[69] == 0x02);
	EXPECT(body[100] == 0 && body[101] == 0);
}

static void test_ep_info(void)
{
	const struct usbredir_endpoint endpoints[] = {
		{0x00, USBREDIR_ENDPOINT_CONTROL, 0, 0, 64},
		{0x80, USBREDIR_ENDPOINT_CONTROL, 0, 0, 64},
		{0x81, USBREDIR_ENDPOINT_BULK, 0, 1, 512},
	};
	uint8_t out[USBREDIR_EP_INFO_SIZE];
	uint8_t *body = out + 16;

	EXPECT(usbredir_encode_ep_info(out, endpoints, 3) == 176);
	EXPECT(read32(out + 4) == 160);
	EXPECT(body[0] == 0 && body[16] == 0 && body[17] == 2);
	EXPECT(body[1] == 255 && body[18] == 255 && body[31] == 255);
	EXPECT(body[64 + 17] == 1);
	EXPECT(read16(body + 96 + 2 * 17) == 512);
	EXPECT(read16(body + 96) == 64);
	EXPECT(read16(body + 96 + 2 * 16) == 64);
}

static void test_status_packets(void)
{
	uint8_t out[USBREDIR_STATUS_PACKET_SIZE];

	EXPECT(usbredir_encode_configuration_status(out, 7, USBREDIR_STATUS_SUCCESS, 1) == 18);
	EXPECT(read32(out) == USBREDIR_CONFIGURATION_STATUS && read32(out + 4) == 2 && read64(out + 8) == 7);
	EXPECT(out[16] == 0 && out[17] == 1);
	EXPECT(usbredir_encode_alt_setting_status(out, 8, USBREDIR_STATUS_STALL, 1, 0) == 19);
	EXPECT(read32(out) == USBREDIR_ALT_SETTING_STATUS && read32(out + 4) == 3);
	EXPECT(out[16] == 4 && out[17] == 1 && out[18] == 0);
}

static void test_control_reply(void)
{
	struct usbredir_control in_reply = {0x80, 0x06, 0x80, USBREDIR_STATUS_SUCCESS, 0x0100, 0x0409, 3};
	struct usbredir_control out_reply = {0x00, 0x01, 0x21, USBREDIR_STATUS_SUCCESS, 0x0100, 0x0001, 34};
	const uint8_t data[] = {1, 2, 3};
	uint8_t out[USBREDIR_CONTROL_REPLY_SIZE + 3];

	EXPECT(usbredir_encode_control_reply(out, 9, &in_reply, data) == 29);
	EXPECT(read32(out) == USBREDIR_CONTROL_PACKET && read32(out + 4) == 13 && read64(out + 8) == 9);
	EXPECT(out[16] == 0x80 && out[17] == 0x06 && out[18] == 0x80 && out[19] == 0);
	EXPECT(read16(out + 20) == 0x0100 && read16(out + 22) == 0x0409 && read16(out + 24) == 3);
	EXPECT(memcmp(out + 26, data, 3) == 0);
	EXPECT(usbredir_encode_control_reply(out, 9, &out_reply, NULL) == 26);
	EXPECT(read32(out + 4) == 10);
	EXPECT(read16(out + 22) == 0x0001 && read16(out + 24) == 34);
}

static void test_bulk_reply(void)
{
	uint8_t out[USBREDIR_BULK_REPLY_SIZE];

	EXPECT(usbredir_encode_bulk_reply(out, 0xABCDEF0123ull, 0x81, USBREDIR_STATUS_SUCCESS, 65534) == 26);
	EXPECT(read32(out) == USBREDIR_BULK_PACKET);
	EXPECT(read32(out + 4) == 10 + 65534);
	EXPECT(read64(out + 8) == 0xABCDEF0123ull);
	EXPECT(out[16] == 0x81 && out[17] == 0);
	EXPECT(read16(out + 18) == 65534 && read32(out + 20) == 0 && read16(out + 24) == 0);
	usbredir_encode_bulk_reply(out, 1, 0x81, USBREDIR_STATUS_SUCCESS, 70000);
	EXPECT(read16(out + 18) == (70000 & 0xFFFF) && read16(out + 24) == 1);
}

static void test_decode_requests(void)
{
	const uint8_t control_body[] = {0x80, 0x06, 0x80, 0x00, 0x00, 0x02, 0x00, 0x00, 0xFF, 0x00};
	const uint8_t bulk_body[] = {0x81, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00};
	struct usbredir_control control;
	struct usbredir_bulk bulk;

	usbredir_decode_control(control_body, &control);
	EXPECT(control.endpoint == 0x80 && control.request == 6 && control.requesttype == 0x80);
	EXPECT(control.value == 0x0200 && control.index == 0 && control.length == 255);
	usbredir_decode_bulk(bulk_body, &bulk);
	EXPECT(bulk.endpoint == 0x81 && bulk.length == 65536 && bulk.stream_id == 0);
}

static void test_minimum_lengths(void)
{
	EXPECT(usbredir_minimum_length(USBREDIR_HELLO) == 64);
	EXPECT(usbredir_minimum_length(USBREDIR_SET_CONFIGURATION) == 1);
	EXPECT(usbredir_minimum_length(USBREDIR_SET_ALT_SETTING) == 2);
	EXPECT(usbredir_minimum_length(USBREDIR_CONTROL_PACKET) == 10);
	EXPECT(usbredir_minimum_length(USBREDIR_BULK_PACKET) == 10);
	EXPECT(usbredir_minimum_length(USBREDIR_RESET) == 0);
}

static void test_connection_parses_packets(void)
{
	struct usbredir_connection connection;
	struct usbredir_header header;
	const uint8_t *body;
	uint8_t hello[USBREDIR_HELLO_SIZE];
	uint8_t reset[USBREDIR_HEADER_SIZE];
	int pair[2];

	EXPECT(socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0);
	usbredir_connection_init(&connection, pair[0]);
	write(pair[1], hello, usbredir_encode_hello(hello, "qemu", 0xFF));
	write(pair[1], reset, usbredir_encode_header(reset, USBREDIR_RESET, 0, 0x1122334455667788ull));
	shutdown(pair[1], SHUT_WR);
	EXPECT(usbredir_receive(&connection) == -1);
	EXPECT(usbredir_next_packet(&connection, &header, &body) == USBREDIR_PACKET_READY);
	EXPECT(header.type == USBREDIR_HELLO && header.length == 68 && usbredir_decode_hello_caps(body, header.length) == 0xFF);
	usbredir_consume_packet(&connection);
	connection.peer_hello = true;
	EXPECT(usbredir_next_packet(&connection, &header, &body) == USBREDIR_PACKET_READY);
	EXPECT(header.type == USBREDIR_RESET && header.id == 0x1122334455667788ull);
	usbredir_consume_packet(&connection);
	EXPECT(usbredir_next_packet(&connection, &header, &body) == USBREDIR_NEED_MORE);
	usbredir_connection_close(&connection);
	close(pair[1]);
}

static void test_connection_rejects_bad_packets(void)
{
	struct usbredir_connection connection;
	struct usbredir_header header;
	const uint8_t *body;
	uint8_t bytes[USBREDIR_HEADER_SIZE];

	usbredir_connection_init(&connection, -1);
	connection.input = malloc(64);
	connection.input_capacity = 64;
	usbredir_encode_header(bytes, USBREDIR_RESET, 0, 0);
	memcpy(connection.input, bytes, 12);
	connection.input_used = 12;
	EXPECT(usbredir_next_packet(&connection, &header, &body) == USBREDIR_MALFORMED);
	connection.peer_hello = true;
	usbredir_encode_header(connection.input, USBREDIR_CONTROL_PACKET, 4, 0);
	connection.input_used = 16;
	EXPECT(usbredir_next_packet(&connection, &header, &body) == USBREDIR_MALFORMED);
	usbredir_encode_header(connection.input, USBREDIR_BULK_PACKET, USBREDIR_INPUT_LIMIT + 1, 0);
	EXPECT(usbredir_next_packet(&connection, &header, &body) == USBREDIR_MALFORMED);
	usbredir_connection_close(&connection);
}

static void test_connection_sends_output(void)
{
	struct usbredir_connection connection;
	uint8_t received[USBREDIR_HEADER_SIZE * 2];
	int pair[2];

	EXPECT(socketpair(AF_UNIX, SOCK_STREAM, 0, pair) == 0);
	usbredir_connection_init(&connection, pair[0]);
	usbredir_commit(&connection, usbredir_encode_device_disconnect(usbredir_reserve(&connection, USBREDIR_HEADER_SIZE)));
	usbredir_commit(&connection, usbredir_encode_header(usbredir_reserve(&connection, USBREDIR_HEADER_SIZE), USBREDIR_RESET, 0, 5));
	EXPECT(usbredir_pending(&connection) == 32);
	EXPECT(usbredir_send(&connection) == 0);
	EXPECT(usbredir_pending(&connection) == 0);
	EXPECT(read(pair[1], received, sizeof received) == 32);
	EXPECT(read32(received) == USBREDIR_DEVICE_DISCONNECT && read32(received + 16) == USBREDIR_RESET);
	usbredir_connection_close(&connection);
	close(pair[1]);
}

static void request(struct uvc_device *device, uint8_t requesttype, uint8_t request_code, uint16_t value, uint16_t index, uint16_t length, struct uvc_reply *reply)
{
	uvc_handle_control(device, requesttype, request_code, value, index, length, NULL, 0, reply);
}

static void set_cur(struct uvc_device *device, uint16_t selector, uint8_t frame_index, uint32_t interval, struct uvc_reply *reply)
{
	uint8_t data[UVC_PROBE_SIZE] = {0};

	data[2] = UVC_FORMAT_INDEX;
	data[3] = frame_index;
	data[4] = interval & 0xFF;
	data[5] = (interval >> 8) & 0xFF;
	data[6] = (interval >> 16) & 0xFF;
	data[7] = interval >> 24;
	uvc_handle_control(device, 0x21, 0x01, (uint16_t)(selector << 8), 0x0001, UVC_PROBE_SIZE, data, sizeof data, reply);
}

static uint8_t error_code(struct uvc_device *device)
{
	struct uvc_reply reply;

	request(device, 0xA1, 0x81, 0x0200, 0x0000, 1, &reply);
	return reply.stall ? 0xFF : reply.data[0];
}

static void test_descriptor_bytes(void)
{
	EXPECT(sizeof expected_configuration == UVC_CONFIGURATION_DESCRIPTOR_SIZE);
	EXPECT(memcmp(uvc_device_descriptor, expected_device_descriptor, sizeof expected_device_descriptor) == 0);
	EXPECT(memcmp(uvc_configuration_descriptor, expected_configuration, sizeof expected_configuration) == 0);
}

static void test_get_descriptor(void)
{
	struct uvc_device device;
	struct uvc_reply reply;

	uvc_device_reset(&device);
	request(&device, 0x80, 0x06, 0x0100, 0, 64, &reply);
	EXPECT(!reply.stall && reply.length == 18 && memcmp(reply.data, expected_device_descriptor, 18) == 0);
	request(&device, 0x80, 0x06, 0x0100, 0, 8, &reply);
	EXPECT(reply.length == 8);
	request(&device, 0x80, 0x06, 0x0200, 0, 255, &reply);
	EXPECT(reply.length == 189 && memcmp(reply.data, expected_configuration, 189) == 0);
	request(&device, 0x80, 0x06, 0x0201, 0, 255, &reply);
	EXPECT(reply.stall);
	request(&device, 0x80, 0x06, 0x0600, 0, 10, &reply);
	EXPECT(!reply.stall && reply.length == 10 && memcmp(reply.data, expected_qualifier, 10) == 0);
	request(&device, 0x80, 0x06, 0x0700, 0, 255, &reply);
	EXPECT(reply.length == 189 && reply.data[1] == 0x07 && read16(reply.data + 186) == 64);
	EXPECT(reply.data[0] == expected_configuration[0] && memcmp(reply.data + 2, expected_configuration + 2, 184) == 0 && reply.data[188] == expected_configuration[188]);
	request(&device, 0x80, 0x06, 0x0F00, 0, 255, &reply);
	EXPECT(reply.stall);
}

static void test_strings(void)
{
	struct uvc_device device;
	struct uvc_reply reply;
	const uint8_t language[] = {0x04, 0x03, 0x09, 0x04};
	const uint8_t product[] = {22, 3, 'M', 0, 'a', 0, 'c', 0, ' ', 0, 'C', 0, 'a', 0, 'm', 0, 'e', 0, 'r', 0, 'a', 0};

	uvc_device_reset(&device);
	request(&device, 0x80, 0x06, 0x0300, 0, 255, &reply);
	EXPECT(reply.length == 4 && memcmp(reply.data, language, 4) == 0);
	request(&device, 0x80, 0x06, 0x0302, 0x0409, 255, &reply);
	EXPECT(reply.length == 22 && memcmp(reply.data, product, 22) == 0);
	request(&device, 0x80, 0x06, 0x0301, 0x0409, 255, &reply);
	EXPECT(reply.length == 6 && reply.data[2] == 'w' && reply.data[4] == 'n');
	request(&device, 0x80, 0x06, 0x03EE, 0, 18, &reply);
	EXPECT(reply.stall);
	request(&device, 0x80, 0x06, 0x0303, 0x0409, 255, &reply);
	EXPECT(reply.stall);
}

static void test_status_and_halt(void)
{
	struct uvc_device device;
	struct uvc_reply reply;

	uvc_device_reset(&device);
	request(&device, 0x80, 0x00, 0, 0, 2, &reply);
	EXPECT(!reply.stall && reply.length == 2 && reply.data[0] == 0 && reply.data[1] == 0);
	request(&device, 0x81, 0x00, 0, 1, 2, &reply);
	EXPECT(!reply.stall && reply.length == 2);
	request(&device, 0x81, 0x00, 0, 2, 2, &reply);
	EXPECT(reply.stall);
	request(&device, 0x02, 0x03, 0, 0x81, 0, &reply);
	EXPECT(!reply.stall && reply.stream_stop && device.halted);
	request(&device, 0x82, 0x00, 0, 0x81, 2, &reply);
	EXPECT(!reply.stall && reply.length == 2 && reply.data[0] == 1 && reply.data[1] == 0);
	request(&device, 0x02, 0x01, 0, 0x81, 0, &reply);
	EXPECT(!reply.stall && reply.stream_stop && !device.halted);
	request(&device, 0x82, 0x00, 0, 0x81, 2, &reply);
	EXPECT(!reply.stall && reply.length == 2 && reply.data[0] == 0 && reply.data[1] == 0);
	request(&device, 0x02, 0x01, 0, 0x00, 0, &reply);
	EXPECT(!reply.stall && !reply.stream_stop);
	request(&device, 0x00, 0x03, 1, 0, 0, &reply);
	EXPECT(reply.stall);
	request(&device, 0x82, 0x00, 0, 0x02, 2, &reply);
	EXPECT(reply.stall);
	request(&device, 0x40, 0x01, 0, 0, 0, &reply);
	EXPECT(reply.stall);
}

static void test_probe(void)
{
	struct uvc_device device;
	struct uvc_reply reply;

	uvc_device_reset(&device);
	request(&device, 0xA1, 0x87, 0x0100, 0x0001, 34, &reply);
	EXPECT(!reply.stall && reply.length == 34);
	EXPECT(reply.data[2] == 1 && reply.data[3] == 1 && read32(reply.data + 4) == 333333);
	EXPECT(read16(reply.data + 16) == 33 && read32(reply.data + 18) == 1843200 && read32(reply.data + 22) == 65536);
	EXPECT(read32(reply.data + 26) == 48000000 && reply.data[30] == 3 && reply.data[31] == 1 && reply.data[32] == 1 && reply.data[33] == 1);
	set_cur(&device, 1, 2, 0, &reply);
	EXPECT(!reply.stall && reply.length == 0);
	request(&device, 0xA1, 0x81, 0x0100, 0x0001, 34, &reply);
	EXPECT(reply.data[3] == 2 && read32(reply.data + 4) == 333333 && read32(reply.data + 18) == 614400);
	set_cur(&device, 1, 9, 5, &reply);
	request(&device, 0xA1, 0x81, 0x0100, 0x0001, 34, &reply);
	EXPECT(reply.data[2] == 1 && reply.data[3] == 1 && read32(reply.data + 18) == 1843200);
	request(&device, 0xA1, 0x82, 0x0100, 0x0001, 34, &reply);
	EXPECT(reply.data[3] == 2);
	request(&device, 0xA1, 0x83, 0x0100, 0x0001, 34, &reply);
	EXPECT(reply.data[3] == 1);
	request(&device, 0xA1, 0x84, 0x0100, 0x0001, 34, &reply);
	EXPECT(reply.data[2] == 1 && reply.data[3] == 1 && read32(reply.data + 4) == 1 && read32(reply.data + 18) == 0);
	request(&device, 0xA1, 0x85, 0x0100, 0x0001, 2, &reply);
	EXPECT(reply.length == 2 && reply.data[0] == 0x22 && reply.data[1] == 0);
	request(&device, 0xA1, 0x86, 0x0100, 0x0001, 1, &reply);
	EXPECT(reply.length == 1 && reply.data[0] == 3);
	request(&device, 0xA1, 0x81, 0x0100, 0x0001, 26, &reply);
	EXPECT(reply.length == 26);
}

static void test_probe_longer_than_34_bytes(void)
{
	struct uvc_device device;
	struct uvc_reply reply;
	uint8_t data[UVC_PROBE_SIZE + 1] = {0};

	uvc_device_reset(&device);
	data[2] = UVC_FORMAT_INDEX;
	data[3] = UVC_FRAME_INDEX_640X480;
	uvc_handle_control(&device, 0x21, 0x01, 0x0100, 0x0001, sizeof data, data, sizeof data, &reply);
	EXPECT(reply.stall && device.probe.frame_index == UVC_FRAME_INDEX_1280X720);
	EXPECT(error_code(&device) == 0x07);
}

static void test_commit(void)
{
	struct uvc_device device;
	struct uvc_reply reply;

	uvc_device_reset(&device);
	set_cur(&device, 2, 2, 333333, &reply);
	EXPECT(!reply.stall && reply.committed && device.has_commit && device.commit.frame_index == 2);
	request(&device, 0xA1, 0x81, 0x0200, 0x0001, 34, &reply);
	EXPECT(reply.data[3] == 2 && read32(reply.data + 18) == 614400);
	set_cur(&device, 2, 1, 0, &reply);
	EXPECT(reply.stall && !reply.committed && device.commit.frame_index == 2);
	EXPECT(error_code(&device) == 0x04);
	EXPECT(error_code(&device) == 0x00);
	set_cur(&device, 2, 3, 333333, &reply);
	EXPECT(reply.stall);
	request(&device, 0xA1, 0x82, 0x0200, 0x0001, 34, &reply);
	EXPECT(reply.stall);
	EXPECT(error_code(&device) == 0x07);
	request(&device, 0xA1, 0x85, 0x0200, 0x0001, 2, &reply);
	EXPECT(!reply.stall && reply.data[0] == 0x22);
	request(&device, 0xA1, 0x86, 0x0200, 0x0001, 1, &reply);
	EXPECT(!reply.stall && reply.data[0] == 3);
}

static void test_unsupported_class_requests(void)
{
	struct uvc_device device;
	struct uvc_reply reply;

	uvc_device_reset(&device);
	request(&device, 0xA1, 0x86, 0x0200, 0x0000, 1, &reply);
	EXPECT(!reply.stall && reply.data[0] == 1);
	request(&device, 0xA1, 0x81, 0x0100, 0x0000, 1, &reply);
	EXPECT(reply.stall);
	EXPECT(error_code(&device) == 0x06);
	request(&device, 0xA1, 0x87, 0x0B00, 0x0100, 2, &reply);
	EXPECT(reply.stall);
	EXPECT(error_code(&device) == 0x06);
	request(&device, 0xA1, 0x81, 0x0100, 0x0700, 2, &reply);
	EXPECT(reply.stall);
	EXPECT(error_code(&device) == 0x05);
	request(&device, 0xA1, 0x81, 0x0600, 0x0001, 1, &reply);
	EXPECT(!reply.stall && reply.data[0] == 0);
	request(&device, 0xA1, 0x81, 0x0900, 0x0001, 1, &reply);
	EXPECT(reply.stall);
	EXPECT(error_code(&device) == 0x06);
	request(&device, 0xA2, 0x81, 0x0100, 0x0081, 1, &reply);
	EXPECT(reply.stall);
	EXPECT(error_code(&device) == 0x07);
	request(&device, 0x21, 0x81, 0x0100, 0x0001, 34, &reply);
	EXPECT(reply.stall);
	EXPECT(error_code(&device) == 0x07);
}

static void test_reset(void)
{
	struct uvc_device device;
	struct uvc_reply reply;

	uvc_device_reset(&device);
	set_cur(&device, 2, 2, 333333, &reply);
	device.configuration = 1;
	device.halted = true;
	uvc_device_reset(&device);
	EXPECT(device.configuration == 0 && !device.halted && !device.has_commit && device.commit.frame_index == 1 && device.probe.frame_index == 1);
}

static size_t pack_frame(struct uvc_stream *stream, size_t frame_size, size_t *sizes, uint8_t *headers, size_t *data_bytes)
{
	uint8_t payload[UVC_PAYLOAD_MAX];
	size_t count = 0;
	uint8_t *frame = uvc_stream_reserve(stream, frame_size);

	memset(frame, 0x5A, frame_size);
	uvc_stream_load(stream, frame_size);
	*data_bytes = 0;
	while (uvc_stream_has_frame(stream)) {
		sizes[count] = uvc_pack_payload(stream, 65536, payload);
		headers[count] = payload[1];
		*data_bytes += sizes[count] - 2;
		EXPECT(payload[0] == 2 && payload[2] == 0x5A && payload[sizes[count] - 1] == 0x5A);
		count++;
	}
	return count;
}

static void test_payloads_for_640x480(void)
{
	struct uvc_stream stream = {0};
	size_t sizes[64];
	uint8_t headers[64];
	size_t data_bytes;
	size_t count = pack_frame(&stream, uvc_frame_bytes(2), sizes, headers, &data_bytes);

	EXPECT(count == 10 && data_bytes == 614400);
	for (size_t index = 0; index < 9; index++)
		EXPECT(sizes[index] == 65534 && headers[index] == (0x80 | (headers[0] & 1)));
	EXPECT(sizes[9] == 24614 && headers[9] == (0x82 | (headers[0] & 1)));
	count = pack_frame(&stream, uvc_frame_bytes(2), sizes, headers + 10, &data_bytes);
	EXPECT(count == 10 && (headers[10] & 1) != (headers[0] & 1));
	uvc_stream_free(&stream);
}

static void test_payloads_for_1280x720(void)
{
	struct uvc_stream stream = {0};
	size_t sizes[64];
	uint8_t headers[64];
	size_t data_bytes;
	size_t count = pack_frame(&stream, uvc_frame_bytes(1), sizes, headers, &data_bytes);

	EXPECT(count == 29 && data_bytes == 1843200);
	EXPECT(sizes[0] == 65534 && sizes[27] == 65534 && sizes[28] == 8306);
	EXPECT((headers[27] & 0x02) == 0 && (headers[28] & 0x02) == 0x02);
	uvc_stream_free(&stream);
}

static void test_payload_fits_small_request(void)
{
	struct uvc_stream stream = {0};
	uint8_t payload[1024];
	uint8_t *frame = uvc_stream_reserve(&stream, 614400);
	uint8_t frame_id;

	memset(frame, 1, 614400);
	uvc_stream_load(&stream, 614400);
	EXPECT(uvc_pack_payload(&stream, 1000, payload) == 998);
	EXPECT(uvc_pack_payload(&stream, 512, payload) == 510);
	EXPECT(uvc_pack_payload(&stream, 5, payload) == 0);
	EXPECT(stream.frame_offset == 996 + 508);
	frame_id = payload[1] & UVC_HEADER_FRAME_ID;
	uvc_stream_drop_frame(&stream);
	EXPECT(!uvc_stream_has_frame(&stream));
	EXPECT(stream.frame_id == frame_id);
	uvc_stream_free(&stream);
}

static void test_queue_keeps_order(void)
{
	struct uvc_request_queue queue = {0};
	struct uvc_request item;

	uvc_queue_push(&queue, 1, 65536);
	uvc_queue_push(&queue, 2, 65536);
	uvc_queue_push(&queue, 3, 512);
	EXPECT(uvc_queue_remove(&queue, 2));
	EXPECT(!uvc_queue_remove(&queue, 2));
	EXPECT(uvc_queue_peek(&queue, &item) && item.id == 1);
	EXPECT(uvc_queue_pop(&queue, &item) && item.id == 1 && item.length == 65536);
	EXPECT(uvc_queue_pop(&queue, &item) && item.id == 3 && item.length == 512);
	EXPECT(!uvc_queue_pop(&queue, &item));
	for (uint64_t id = 0; id < 200; id++) {
		uvc_queue_push(&queue, id, 1);
		if (id % 3 == 0)
			uvc_queue_pop(&queue, NULL);
	}
	EXPECT(uvc_queue_peek(&queue, &item) && item.id == 67);
	EXPECT(queue.count == 133);
	uvc_queue_clear(&queue);
	EXPECT(!uvc_queue_peek(&queue, &item));
}

static void test_idle_timer(void)
{
	EXPECT(!uvc_idle_expired(10.0, 12.9));
	EXPECT(uvc_idle_expired(10.0, 13.0));
}

int main(void)
{
	test_hello();
	test_header_has_64_bit_id();
	test_device_connect();
	test_device_disconnect();
	test_interface_info();
	test_ep_info();
	test_status_packets();
	test_control_reply();
	test_bulk_reply();
	test_decode_requests();
	test_minimum_lengths();
	test_connection_parses_packets();
	test_connection_rejects_bad_packets();
	test_connection_sends_output();
	test_descriptor_bytes();
	test_get_descriptor();
	test_strings();
	test_status_and_halt();
	test_probe();
	test_probe_longer_than_34_bytes();
	test_commit();
	test_unsupported_class_requests();
	test_reset();
	test_payloads_for_640x480();
	test_payloads_for_1280x720();
	test_payload_fits_small_request();
	test_queue_keeps_order();
	test_idle_timer();
	if (failures) {
		fprintf(stderr, "%d failed\n", failures);
		return 1;
	}
	printf("ok\n");
	return 0;
}
