#ifndef WN_CAMERA_USBREDIR_H
#define WN_CAMERA_USBREDIR_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define USBREDIR_HELLO 0
#define USBREDIR_DEVICE_CONNECT 1
#define USBREDIR_DEVICE_DISCONNECT 2
#define USBREDIR_RESET 3
#define USBREDIR_INTERFACE_INFO 4
#define USBREDIR_EP_INFO 5
#define USBREDIR_SET_CONFIGURATION 6
#define USBREDIR_GET_CONFIGURATION 7
#define USBREDIR_CONFIGURATION_STATUS 8
#define USBREDIR_SET_ALT_SETTING 9
#define USBREDIR_GET_ALT_SETTING 10
#define USBREDIR_ALT_SETTING_STATUS 11
#define USBREDIR_CANCEL_DATA_PACKET 21
#define USBREDIR_FILTER_REJECT 22
#define USBREDIR_CONTROL_PACKET 100
#define USBREDIR_BULK_PACKET 101

#define USBREDIR_STATUS_SUCCESS 0
#define USBREDIR_STATUS_CANCELLED 1
#define USBREDIR_STATUS_STALL 4

#define USBREDIR_CAPS 0x7Eu
#define USBREDIR_REQUIRED_PEER_CAPS 0x72u
#define USBREDIR_SPEED_HIGH 2
#define USBREDIR_ENDPOINT_CONTROL 0
#define USBREDIR_ENDPOINT_BULK 2
#define USBREDIR_ENDPOINT_INVALID 255
#define USBREDIR_ENDPOINT_SLOTS 32
#define USBREDIR_INTERFACE_SLOTS 32
#define USBREDIR_DIRECTION_IN 0x80

#define USBREDIR_HELLO_HEADER_SIZE 12
#define USBREDIR_HEADER_SIZE 16
#define USBREDIR_HELLO_VERSION_SIZE 64
#define USBREDIR_HELLO_SIZE (USBREDIR_HELLO_HEADER_SIZE + USBREDIR_HELLO_VERSION_SIZE + 4)
#define USBREDIR_DEVICE_CONNECT_SIZE (USBREDIR_HEADER_SIZE + 10)
#define USBREDIR_INTERFACE_INFO_SIZE (USBREDIR_HEADER_SIZE + 4 + 4 * USBREDIR_INTERFACE_SLOTS)
#define USBREDIR_EP_INFO_SIZE (USBREDIR_HEADER_SIZE + 5 * USBREDIR_ENDPOINT_SLOTS)
#define USBREDIR_STATUS_PACKET_SIZE (USBREDIR_HEADER_SIZE + 3)
#define USBREDIR_CONTROL_HEADER_SIZE 10
#define USBREDIR_BULK_HEADER_SIZE 10
#define USBREDIR_CONTROL_REPLY_SIZE (USBREDIR_HEADER_SIZE + USBREDIR_CONTROL_HEADER_SIZE)
#define USBREDIR_BULK_REPLY_SIZE (USBREDIR_HEADER_SIZE + USBREDIR_BULK_HEADER_SIZE)
#define USBREDIR_INPUT_LIMIT (1024 * 1024)
#define USBREDIR_SEND_BUFFER_SIZE (4 * 1024 * 1024)

enum usbredir_parse {
	USBREDIR_NEED_MORE,
	USBREDIR_PACKET_READY,
	USBREDIR_MALFORMED,
};

struct usbredir_header {
	uint32_t type;
	uint32_t length;
	uint64_t id;
};

struct usbredir_control {
	uint8_t endpoint;
	uint8_t request;
	uint8_t requesttype;
	uint8_t status;
	uint16_t value;
	uint16_t index;
	uint16_t length;
};

struct usbredir_bulk {
	uint8_t endpoint;
	uint8_t status;
	uint32_t length;
	uint32_t stream_id;
};

struct usbredir_device_info {
	uint8_t speed;
	uint8_t device_class;
	uint8_t device_subclass;
	uint8_t device_protocol;
	uint16_t vendor_id;
	uint16_t product_id;
	uint16_t device_version;
};

struct usbredir_interface {
	uint8_t number;
	uint8_t interface_class;
	uint8_t interface_subclass;
	uint8_t interface_protocol;
};

struct usbredir_endpoint {
	uint8_t address;
	uint8_t type;
	uint8_t interval;
	uint8_t interface;
	uint16_t max_packet_size;
};

struct usbredir_connection {
	int fd;
	bool peer_hello;
	size_t packet_size;
	uint8_t *input;
	size_t input_used;
	size_t input_capacity;
	uint8_t *output;
	size_t output_start;
	size_t output_end;
	size_t output_capacity;
};

size_t usbredir_encode_header(uint8_t *out, uint32_t type, uint32_t length, uint64_t id);
size_t usbredir_encode_hello(uint8_t *out, const char *version, uint32_t caps);
size_t usbredir_encode_device_connect(uint8_t *out, const struct usbredir_device_info *info);
size_t usbredir_encode_device_disconnect(uint8_t *out);
size_t usbredir_encode_interface_info(uint8_t *out, const struct usbredir_interface *interfaces, uint32_t count);
size_t usbredir_encode_ep_info(uint8_t *out, const struct usbredir_endpoint *endpoints, size_t count);
size_t usbredir_encode_configuration_status(uint8_t *out, uint64_t id, uint8_t status, uint8_t configuration);
size_t usbredir_encode_alt_setting_status(uint8_t *out, uint64_t id, uint8_t status, uint8_t interface, uint8_t alt);
size_t usbredir_encode_control_reply(uint8_t *out, uint64_t id, const struct usbredir_control *control, const uint8_t *data);
size_t usbredir_encode_bulk_reply(uint8_t *out, uint64_t id, uint8_t endpoint, uint8_t status, uint32_t data_length);
uint32_t usbredir_minimum_length(uint32_t type);
uint32_t usbredir_decode_hello_caps(const uint8_t *body, uint32_t length);
void usbredir_decode_control(const uint8_t *body, struct usbredir_control *control);
void usbredir_decode_bulk(const uint8_t *body, struct usbredir_bulk *bulk);

int usbredir_connect(const char *path);
void usbredir_connection_init(struct usbredir_connection *connection, int fd);
void usbredir_connection_close(struct usbredir_connection *connection);
int usbredir_receive(struct usbredir_connection *connection);
enum usbredir_parse usbredir_next_packet(struct usbredir_connection *connection, struct usbredir_header *header, const uint8_t **body);
void usbredir_consume_packet(struct usbredir_connection *connection);
uint8_t *usbredir_reserve(struct usbredir_connection *connection, size_t size);
void usbredir_commit(struct usbredir_connection *connection, size_t size);
size_t usbredir_pending(const struct usbredir_connection *connection);
int usbredir_send(struct usbredir_connection *connection);

#endif
