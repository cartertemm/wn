#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "usbredir.h"

static int failures;

static void expect(bool passed, const char *condition, int line)
{
	if (!passed) {
		failures++;
		fprintf(stderr, "camera_units.c:%d: %s\n", line, condition);
	}
}

#define EXPECT(condition) expect((condition), #condition, __LINE__)

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
	struct usbredir_control in_reply = {0x80, 0x06, 0x80, USBREDIR_STATUS_SUCCESS, 0x0100, 0, 3};
	struct usbredir_control out_reply = {0x00, 0x01, 0x21, USBREDIR_STATUS_SUCCESS, 0x0100, 0x0001, 34};
	const uint8_t data[] = {1, 2, 3};
	uint8_t out[USBREDIR_CONTROL_REPLY_SIZE + 3];

	EXPECT(usbredir_encode_control_reply(out, 9, &in_reply, data) == 29);
	EXPECT(read32(out + 4) == 13);
	EXPECT(out[16] == 0x80 && out[17] == 0x06 && out[18] == 0x80 && out[19] == 0);
	EXPECT(read16(out + 20) == 0x0100 && read16(out + 24) == 3);
	EXPECT(memcmp(out + 26, data, 3) == 0);
	EXPECT(usbredir_encode_control_reply(out, 9, &out_reply, NULL) == 26);
	EXPECT(read32(out + 4) == 10);
	EXPECT(read16(out + 24) == 34);
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
	write(pair[1], reset, usbredir_encode_header(reset, USBREDIR_RESET, 0, 42));
	shutdown(pair[1], SHUT_WR);
	EXPECT(usbredir_receive(&connection) == -1);
	EXPECT(usbredir_next_packet(&connection, &header, &body) == USBREDIR_PACKET_READY);
	EXPECT(header.type == USBREDIR_HELLO && header.length == 68 && usbredir_decode_hello_caps(body, header.length) == 0xFF);
	usbredir_consume_packet(&connection);
	connection.peer_hello = true;
	EXPECT(usbredir_next_packet(&connection, &header, &body) == USBREDIR_PACKET_READY);
	EXPECT(header.type == USBREDIR_RESET && header.id == 42);
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
	if (failures) {
		fprintf(stderr, "%d failed\n", failures);
		return 1;
	}
	printf("ok\n");
	return 0;
}
