#include "usbredir.h"

#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define READ_CHUNK 65536

static void put16(uint8_t *out, uint16_t value)
{
	out[0] = value & 0xFF;
	out[1] = value >> 8;
}

static void put32(uint8_t *out, uint32_t value)
{
	put16(out, value & 0xFFFF);
	put16(out + 2, value >> 16);
}

static void put64(uint8_t *out, uint64_t value)
{
	put32(out, value & 0xFFFFFFFFu);
	put32(out + 4, value >> 32);
}

static uint16_t get16(const uint8_t *in)
{
	return (uint16_t)(in[0] | in[1] << 8);
}

static uint32_t get32(const uint8_t *in)
{
	return get16(in) | (uint32_t)get16(in + 2) << 16;
}

static uint64_t get64(const uint8_t *in)
{
	return get32(in) | (uint64_t)get32(in + 4) << 32;
}

static size_t endpoint_slot(uint8_t address)
{
	return ((address & USBREDIR_DIRECTION_IN) >> 3) | (address & 0x0F);
}

size_t usbredir_encode_header(uint8_t *out, uint32_t type, uint32_t length, uint64_t id)
{
	put32(out, type);
	put32(out + 4, length);
	put64(out + 8, id);
	return USBREDIR_HEADER_SIZE;
}

size_t usbredir_encode_hello(uint8_t *out, const char *version, uint32_t caps)
{
	put32(out, USBREDIR_HELLO);
	put32(out + 4, USBREDIR_HELLO_VERSION_SIZE + 4);
	put32(out + 8, 0);
	memset(out + USBREDIR_HELLO_HEADER_SIZE, 0, USBREDIR_HELLO_VERSION_SIZE);
	strncpy((char *)out + USBREDIR_HELLO_HEADER_SIZE, version, USBREDIR_HELLO_VERSION_SIZE - 1);
	put32(out + USBREDIR_HELLO_HEADER_SIZE + USBREDIR_HELLO_VERSION_SIZE, caps);
	return USBREDIR_HELLO_SIZE;
}

size_t usbredir_encode_device_connect(uint8_t *out, const struct usbredir_device_info *info)
{
	uint8_t *body = out + usbredir_encode_header(out, USBREDIR_DEVICE_CONNECT, USBREDIR_DEVICE_CONNECT_SIZE - USBREDIR_HEADER_SIZE, 0);
	body[0] = info->speed;
	body[1] = info->device_class;
	body[2] = info->device_subclass;
	body[3] = info->device_protocol;
	put16(body + 4, info->vendor_id);
	put16(body + 6, info->product_id);
	put16(body + 8, info->device_version);
	return USBREDIR_DEVICE_CONNECT_SIZE;
}

size_t usbredir_encode_device_disconnect(uint8_t *out)
{
	return usbredir_encode_header(out, USBREDIR_DEVICE_DISCONNECT, 0, 0);
}

size_t usbredir_encode_interface_info(uint8_t *out, const struct usbredir_interface *interfaces, uint32_t count)
{
	uint8_t *body = out + usbredir_encode_header(out, USBREDIR_INTERFACE_INFO, USBREDIR_INTERFACE_INFO_SIZE - USBREDIR_HEADER_SIZE, 0);
	memset(body, 0, USBREDIR_INTERFACE_INFO_SIZE - USBREDIR_HEADER_SIZE);
	put32(body, count);
	for (uint32_t index = 0; index < count; index++) {
		body[4 + index] = interfaces[index].number;
		body[4 + USBREDIR_INTERFACE_SLOTS + index] = interfaces[index].interface_class;
		body[4 + 2 * USBREDIR_INTERFACE_SLOTS + index] = interfaces[index].interface_subclass;
		body[4 + 3 * USBREDIR_INTERFACE_SLOTS + index] = interfaces[index].interface_protocol;
	}
	return USBREDIR_INTERFACE_INFO_SIZE;
}

size_t usbredir_encode_ep_info(uint8_t *out, const struct usbredir_endpoint *endpoints, size_t count)
{
	uint8_t *body = out + usbredir_encode_header(out, USBREDIR_EP_INFO, USBREDIR_EP_INFO_SIZE - USBREDIR_HEADER_SIZE, 0);
	memset(body, 0, USBREDIR_EP_INFO_SIZE - USBREDIR_HEADER_SIZE);
	memset(body, USBREDIR_ENDPOINT_INVALID, USBREDIR_ENDPOINT_SLOTS);
	for (size_t index = 0; index < count; index++) {
		size_t slot = endpoint_slot(endpoints[index].address);
		body[slot] = endpoints[index].type;
		body[USBREDIR_ENDPOINT_SLOTS + slot] = endpoints[index].interval;
		body[2 * USBREDIR_ENDPOINT_SLOTS + slot] = endpoints[index].interface;
		put16(body + 3 * USBREDIR_ENDPOINT_SLOTS + 2 * slot, endpoints[index].max_packet_size);
	}
	return USBREDIR_EP_INFO_SIZE;
}

size_t usbredir_encode_configuration_status(uint8_t *out, uint64_t id, uint8_t status, uint8_t configuration)
{
	uint8_t *body = out + usbredir_encode_header(out, USBREDIR_CONFIGURATION_STATUS, 2, id);
	body[0] = status;
	body[1] = configuration;
	return USBREDIR_HEADER_SIZE + 2;
}

size_t usbredir_encode_alt_setting_status(uint8_t *out, uint64_t id, uint8_t status, uint8_t interface, uint8_t alt)
{
	uint8_t *body = out + usbredir_encode_header(out, USBREDIR_ALT_SETTING_STATUS, 3, id);
	body[0] = status;
	body[1] = interface;
	body[2] = alt;
	return USBREDIR_HEADER_SIZE + 3;
}

size_t usbredir_encode_control_reply(uint8_t *out, uint64_t id, const struct usbredir_control *control, const uint8_t *data)
{
	size_t data_length = (control->endpoint & USBREDIR_DIRECTION_IN) ? control->length : 0;
	uint8_t *body = out + usbredir_encode_header(out, USBREDIR_CONTROL_PACKET, (uint32_t)(USBREDIR_CONTROL_HEADER_SIZE + data_length), id);
	body[0] = control->endpoint;
	body[1] = control->request;
	body[2] = control->requesttype;
	body[3] = control->status;
	put16(body + 4, control->value);
	put16(body + 6, control->index);
	put16(body + 8, control->length);
	memcpy(body + USBREDIR_CONTROL_HEADER_SIZE, data, data_length);
	return USBREDIR_CONTROL_REPLY_SIZE + data_length;
}

size_t usbredir_encode_bulk_reply(uint8_t *out, uint64_t id, uint8_t endpoint, uint8_t status, uint32_t data_length)
{
	uint8_t *body = out + usbredir_encode_header(out, USBREDIR_BULK_PACKET, USBREDIR_BULK_HEADER_SIZE + data_length, id);
	body[0] = endpoint;
	body[1] = status;
	put16(body + 2, data_length & 0xFFFF);
	put32(body + 4, 0);
	put16(body + 8, data_length >> 16);
	return USBREDIR_BULK_REPLY_SIZE;
}

uint32_t usbredir_minimum_length(uint32_t type)
{
	switch (type) {
	case USBREDIR_HELLO:
		return USBREDIR_HELLO_VERSION_SIZE;
	case USBREDIR_SET_CONFIGURATION:
	case USBREDIR_GET_ALT_SETTING:
		return 1;
	case USBREDIR_SET_ALT_SETTING:
		return 2;
	case USBREDIR_CONTROL_PACKET:
		return USBREDIR_CONTROL_HEADER_SIZE;
	case USBREDIR_BULK_PACKET:
		return USBREDIR_BULK_HEADER_SIZE;
	default:
		return 0;
	}
}

uint32_t usbredir_decode_hello_caps(const uint8_t *body, uint32_t length)
{
	if (length < USBREDIR_HELLO_VERSION_SIZE + 4)
		return 0;
	return get32(body + USBREDIR_HELLO_VERSION_SIZE);
}

void usbredir_decode_control(const uint8_t *body, struct usbredir_control *control)
{
	control->endpoint = body[0];
	control->request = body[1];
	control->requesttype = body[2];
	control->status = body[3];
	control->value = get16(body + 4);
	control->index = get16(body + 6);
	control->length = get16(body + 8);
}

void usbredir_decode_bulk(const uint8_t *body, struct usbredir_bulk *bulk)
{
	bulk->endpoint = body[0];
	bulk->status = body[1];
	bulk->length = get16(body + 2) | (uint32_t)get16(body + 8) << 16;
	bulk->stream_id = get32(body + 4);
}

int usbredir_connect(const char *path)
{
	struct sockaddr_un address;
	int buffer_size = USBREDIR_SEND_BUFFER_SIZE;
	int enabled = 1;
	int saved_errno;
	int fd;

	if (strlen(path) >= sizeof address.sun_path) {
		errno = ENAMETOOLONG;
		return -1;
	}
	fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0)
		return -1;
	memset(&address, 0, sizeof address);
	address.sun_family = AF_UNIX;
	strncpy(address.sun_path, path, sizeof address.sun_path - 1);
	setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &buffer_size, sizeof buffer_size);
	setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof enabled);
	if (connect(fd, (struct sockaddr *)&address, sizeof address) < 0) {
		saved_errno = errno;
		close(fd);
		errno = saved_errno;
		return -1;
	}
	fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
	return fd;
}

void usbredir_connection_init(struct usbredir_connection *connection, int fd)
{
	memset(connection, 0, sizeof *connection);
	connection->fd = fd;
}

void usbredir_connection_close(struct usbredir_connection *connection)
{
	if (connection->fd >= 0)
		close(connection->fd);
	free(connection->input);
	free(connection->output);
	usbredir_connection_init(connection, -1);
}

static uint8_t *grow(uint8_t *buffer, size_t *capacity, size_t needed)
{
	size_t size = *capacity ? *capacity : READ_CHUNK;
	uint8_t *grown;

	if (needed <= *capacity)
		return buffer;
	while (size < needed)
		size *= 2;
	grown = realloc(buffer, size);
	if (!grown)
		abort();
	*capacity = size;
	return grown;
}

int usbredir_receive(struct usbredir_connection *connection)
{
	ssize_t count;

	for (;;) {
		connection->input = grow(connection->input, &connection->input_capacity, connection->input_used + READ_CHUNK);
		count = read(connection->fd, connection->input + connection->input_used, connection->input_capacity - connection->input_used);
		if (count > 0) {
			connection->input_used += (size_t)count;
			continue;
		}
		if (count < 0 && errno == EINTR)
			continue;
		if (count < 0 && errno == EAGAIN)
			return 0;
		return -1;
	}
}

enum usbredir_parse usbredir_next_packet(struct usbredir_connection *connection, struct usbredir_header *header, const uint8_t **body)
{
	size_t header_size = connection->peer_hello ? USBREDIR_HEADER_SIZE : USBREDIR_HELLO_HEADER_SIZE;
	const uint8_t *input = connection->input;

	if (connection->input_used < header_size)
		return USBREDIR_NEED_MORE;
	header->type = get32(input);
	header->length = get32(input + 4);
	header->id = connection->peer_hello ? get64(input + 8) : get32(input + 8);
	if (header->length > USBREDIR_INPUT_LIMIT || (!connection->peer_hello && header->type != USBREDIR_HELLO))
		return USBREDIR_MALFORMED;
	if (header->length < usbredir_minimum_length(header->type))
		return USBREDIR_MALFORMED;
	if (connection->input_used < header_size + header->length)
		return USBREDIR_NEED_MORE;
	connection->packet_size = header_size + header->length;
	*body = input + header_size;
	return USBREDIR_PACKET_READY;
}

void usbredir_consume_packet(struct usbredir_connection *connection)
{
	connection->input_used -= connection->packet_size;
	memmove(connection->input, connection->input + connection->packet_size, connection->input_used);
	connection->packet_size = 0;
}

uint8_t *usbredir_reserve(struct usbredir_connection *connection, size_t size)
{
	if (connection->output_end + size > connection->output_capacity && connection->output_start > 0) {
		memmove(connection->output, connection->output + connection->output_start, connection->output_end - connection->output_start);
		connection->output_end -= connection->output_start;
		connection->output_start = 0;
	}
	connection->output = grow(connection->output, &connection->output_capacity, connection->output_end + size);
	return connection->output + connection->output_end;
}

void usbredir_commit(struct usbredir_connection *connection, size_t size)
{
	connection->output_end += size;
}

size_t usbredir_pending(const struct usbredir_connection *connection)
{
	return connection->output_end - connection->output_start;
}

int usbredir_send(struct usbredir_connection *connection)
{
	ssize_t count;

	while (connection->output_start < connection->output_end) {
		count = write(connection->fd, connection->output + connection->output_start, connection->output_end - connection->output_start);
		if (count < 0 && errno == EINTR)
			continue;
		if (count < 0 && errno == EAGAIN)
			return 0;
		if (count < 0)
			return -1;
		connection->output_start += (size_t)count;
	}
	connection->output_start = 0;
	connection->output_end = 0;
	return 0;
}
