#include "uvc.h"

#include <stdlib.h>
#include <string.h>

#define USB_TYPE_MASK 0x60
#define USB_TYPE_STANDARD 0x00
#define USB_TYPE_CLASS 0x20
#define USB_DEVICE_TO_HOST 0x80
#define USB_INTERFACE_TO_HOST 0x81
#define USB_ENDPOINT_TO_HOST 0x82
#define USB_HOST_TO_ENDPOINT 0x02
#define USB_REQUEST_GET_STATUS 0x00
#define USB_REQUEST_CLEAR_FEATURE 0x01
#define USB_REQUEST_SET_FEATURE 0x03
#define USB_REQUEST_GET_DESCRIPTOR 0x06
#define USB_DESCRIPTOR_DEVICE 0x01
#define USB_DESCRIPTOR_CONFIGURATION 0x02
#define USB_DESCRIPTOR_STRING 0x03
#define USB_DESCRIPTOR_DEVICE_QUALIFIER 0x06
#define USB_DESCRIPTOR_OTHER_SPEED_CONFIGURATION 0x07
#define USB_FEATURE_ENDPOINT_HALT 0x00
#define USB_FULL_SPEED_BULK_MAX_PACKET 64
#define USB_LANGUAGE_US_ENGLISH 0x0409
#define DEVICE_QUALIFIER_SIZE 10
#define ENDPOINT_MAX_PACKET_OFFSET 186

#define UVC_CLASS_SET 0x21
#define UVC_CLASS_GET 0xA1
#define UVC_SET_CUR 0x01
#define UVC_GET_CUR 0x81
#define UVC_GET_MIN 0x82
#define UVC_GET_MAX 0x83
#define UVC_GET_RES 0x84
#define UVC_GET_LEN 0x85
#define UVC_GET_INFO 0x86
#define UVC_GET_DEF 0x87
#define UVC_CAMERA_TERMINAL_ID 1
#define UVC_OUTPUT_TERMINAL_ID 2
#define UVC_VC_REQUEST_ERROR_CODE_CONTROL 0x02
#define UVC_VS_PROBE_CONTROL 0x01
#define UVC_VS_COMMIT_CONTROL 0x02
#define UVC_VS_STREAM_ERROR_CODE_CONTROL 0x06
#define UVC_ERROR_NONE 0x00
#define UVC_ERROR_OUT_OF_RANGE 0x04
#define UVC_ERROR_INVALID_UNIT 0x05
#define UVC_ERROR_INVALID_CONTROL 0x06
#define UVC_ERROR_INVALID_REQUEST 0x07
#define UVC_INFO_GET 0x01
#define UVC_INFO_GET_SET 0x03
#define UVC_DEFAULT_FRAME_INDEX UVC_FRAME_INDEX_1280X720
#define UVC_PROBE_DELAY_MS 33
#define UVC_CLOCK_FREQUENCY 48000000
#define UVC_FRAMING_INFO 0x03
#define UVC_PAYLOAD_VERSION 1
#define UVC_MACROPIXEL_SIZE 4
#define UVC_QUEUE_INITIAL_CAPACITY 64

const uint8_t uvc_device_descriptor[UVC_DEVICE_DESCRIPTOR_SIZE] = {
	0x12, 0x01, 0x00, 0x02, UVC_DEVICE_CLASS, UVC_DEVICE_SUBCLASS, UVC_DEVICE_PROTOCOL, UVC_CONTROL_MAX_PACKET,
	UVC_VENDOR_ID & 0xFF, UVC_VENDOR_ID >> 8, UVC_PRODUCT_ID & 0xFF, UVC_PRODUCT_ID >> 8,
	UVC_DEVICE_BCD & 0xFF, UVC_DEVICE_BCD >> 8, 0x01, 0x02, 0x00, 0x01,
};

static const uint8_t device_qualifier[DEVICE_QUALIFIER_SIZE] = {
	0x0A, 0x06, 0x00, 0x02, UVC_DEVICE_CLASS, UVC_DEVICE_SUBCLASS, UVC_DEVICE_PROTOCOL, UVC_CONTROL_MAX_PACKET, 0x01, 0x00,
};

const uint8_t uvc_configuration_descriptor[UVC_CONFIGURATION_DESCRIPTOR_SIZE] = {
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

static const char *const strings[] = {NULL, "wn", "Mac Camera"};

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

static uint16_t get16(const uint8_t *in)
{
	return (uint16_t)(in[0] | in[1] << 8);
}

static uint32_t get32(const uint8_t *in)
{
	return get16(in) | (uint32_t)get16(in + 2) << 16;
}

uint16_t uvc_frame_width(uint8_t frame_index)
{
	return frame_index == UVC_FRAME_INDEX_640X480 ? 640 : 1280;
}

uint16_t uvc_frame_height(uint8_t frame_index)
{
	return frame_index == UVC_FRAME_INDEX_640X480 ? 480 : 720;
}

size_t uvc_frame_bytes(uint8_t frame_index)
{
	return (size_t)uvc_frame_width(frame_index) * uvc_frame_height(frame_index) * UVC_BYTES_PER_PIXEL;
}

void uvc_default_probe(struct uvc_probe *probe)
{
	probe->hint = 0;
	probe->format_index = UVC_FORMAT_INDEX;
	probe->frame_index = UVC_DEFAULT_FRAME_INDEX;
	probe->frame_interval = UVC_FRAME_INTERVAL;
}

void uvc_encode_probe(const struct uvc_probe *probe, uint8_t *out)
{
	memset(out, 0, UVC_PROBE_SIZE);
	put16(out, probe->hint);
	out[2] = probe->format_index;
	out[3] = probe->frame_index;
	put32(out + 4, probe->frame_interval);
	put16(out + 16, UVC_PROBE_DELAY_MS);
	put32(out + 18, (uint32_t)uvc_frame_bytes(probe->frame_index));
	put32(out + 22, UVC_MAX_PAYLOAD_TRANSFER);
	put32(out + 26, UVC_CLOCK_FREQUENCY);
	out[30] = UVC_FRAMING_INFO;
	out[31] = UVC_PAYLOAD_VERSION;
	out[32] = UVC_PAYLOAD_VERSION;
	out[33] = UVC_PAYLOAD_VERSION;
}

void uvc_device_reset(struct uvc_device *device)
{
	device->configuration = 0;
	device->halted = false;
	device->request_error = UVC_ERROR_NONE;
	uvc_default_probe(&device->probe);
	uvc_default_probe(&device->commit);
	device->has_commit = false;
}

static void reply_bytes(struct uvc_reply *reply, const uint8_t *bytes, size_t size, uint16_t length)
{
	size_t count = size < length ? size : length;

	memcpy(reply->data, bytes, count);
	reply->length = (uint16_t)count;
}

static size_t string_descriptor(uint8_t index, uint8_t *out)
{
	size_t count;

	if (index == 0) {
		out[0] = 4;
		out[1] = USB_DESCRIPTOR_STRING;
		put16(out + 2, USB_LANGUAGE_US_ENGLISH);
		return 4;
	}
	if (index >= sizeof strings / sizeof strings[0])
		return 0;
	count = strlen(strings[index]);
	out[0] = (uint8_t)(2 + 2 * count);
	out[1] = USB_DESCRIPTOR_STRING;
	for (size_t position = 0; position < count; position++)
		put16(out + 2 + 2 * position, (uint8_t)strings[index][position]);
	return 2 + 2 * count;
}

static size_t descriptor(uint8_t type, uint8_t index, uint8_t *out)
{
	switch (type) {
	case USB_DESCRIPTOR_DEVICE:
		memcpy(out, uvc_device_descriptor, UVC_DEVICE_DESCRIPTOR_SIZE);
		return UVC_DEVICE_DESCRIPTOR_SIZE;
	case USB_DESCRIPTOR_CONFIGURATION:
	case USB_DESCRIPTOR_OTHER_SPEED_CONFIGURATION:
		if (index != 0)
			return 0;
		memcpy(out, uvc_configuration_descriptor, UVC_CONFIGURATION_DESCRIPTOR_SIZE);
		if (type == USB_DESCRIPTOR_OTHER_SPEED_CONFIGURATION) {
			out[1] = USB_DESCRIPTOR_OTHER_SPEED_CONFIGURATION;
			put16(out + ENDPOINT_MAX_PACKET_OFFSET, USB_FULL_SPEED_BULK_MAX_PACKET);
		}
		return UVC_CONFIGURATION_DESCRIPTOR_SIZE;
	case USB_DESCRIPTOR_STRING:
		return string_descriptor(index, out);
	case USB_DESCRIPTOR_DEVICE_QUALIFIER:
		memcpy(out, device_qualifier, DEVICE_QUALIFIER_SIZE);
		return DEVICE_QUALIFIER_SIZE;
	default:
		return 0;
	}
}

static void handle_standard(struct uvc_device *device, uint8_t requesttype, uint8_t request, uint16_t value, uint16_t index, uint16_t length, struct uvc_reply *reply)
{
	uint8_t bytes[UVC_REPLY_CAPACITY] = {0};
	size_t size;

	if (requesttype == USB_DEVICE_TO_HOST && request == USB_REQUEST_GET_DESCRIPTOR) {
		size = descriptor(value >> 8, value & 0xFF, bytes);
		if (size > 0) {
			reply_bytes(reply, bytes, size, length);
			return;
		}
	} else if (request == USB_REQUEST_GET_STATUS) {
		bool device_status = requesttype == USB_DEVICE_TO_HOST;
		bool interface_status = requesttype == USB_INTERFACE_TO_HOST && (index == UVC_CONTROL_INTERFACE || index == UVC_STREAMING_INTERFACE);
		bool endpoint_status = requesttype == USB_ENDPOINT_TO_HOST && (index == 0 || index == UVC_STREAMING_ENDPOINT);
		if (device_status || interface_status || endpoint_status) {
			bytes[0] = endpoint_status && index == UVC_STREAMING_ENDPOINT && device->halted;
			reply_bytes(reply, bytes, 2, length);
			return;
		}
	} else if (requesttype == USB_HOST_TO_ENDPOINT && value == USB_FEATURE_ENDPOINT_HALT && (request == USB_REQUEST_CLEAR_FEATURE || request == USB_REQUEST_SET_FEATURE)) {
		if (index == UVC_STREAMING_ENDPOINT) {
			device->halted = request == USB_REQUEST_SET_FEATURE;
			reply->stream_stop = true;
			return;
		}
		if (index == 0 && request == USB_REQUEST_CLEAR_FEATURE)
			return;
	}
	reply->stall = true;
}

static uint8_t reply_byte(uint8_t value, uint16_t length, struct uvc_reply *reply)
{
	reply_bytes(reply, &value, 1, length);
	return UVC_ERROR_NONE;
}

static uint8_t byte_control(uint8_t request, uint8_t value, uint16_t length, struct uvc_reply *reply)
{
	if (request == UVC_GET_CUR)
		return reply_byte(value, length, reply);
	if (request == UVC_GET_INFO)
		return reply_byte(UVC_INFO_GET, length, reply);
	return UVC_ERROR_INVALID_REQUEST;
}

static uint8_t reply_probe(const struct uvc_probe *probe, uint16_t length, struct uvc_reply *reply)
{
	uint8_t bytes[UVC_PROBE_SIZE];

	uvc_encode_probe(probe, bytes);
	reply_bytes(reply, bytes, UVC_PROBE_SIZE, length);
	return UVC_ERROR_NONE;
}

static uint8_t reply_probe_length(uint16_t length, struct uvc_reply *reply)
{
	uint8_t bytes[2];

	put16(bytes, UVC_PROBE_SIZE);
	reply_bytes(reply, bytes, sizeof bytes, length);
	return UVC_ERROR_NONE;
}

static uint8_t reply_probe_resolution(uint16_t length, struct uvc_reply *reply)
{
	uint8_t bytes[UVC_PROBE_SIZE] = {0};

	bytes[2] = 1;
	bytes[3] = 1;
	put32(bytes + 4, 1);
	reply_bytes(reply, bytes, UVC_PROBE_SIZE, length);
	return UVC_ERROR_NONE;
}

static bool decode_probe(const uint8_t *data, size_t data_length, struct uvc_probe *probe)
{
	uint8_t bytes[UVC_PROBE_SIZE] = {0};

	if (data_length > UVC_PROBE_SIZE)
		return false;
	memcpy(bytes, data, data_length);
	probe->hint = get16(bytes);
	probe->format_index = bytes[2];
	probe->frame_index = bytes[3];
	probe->frame_interval = get32(bytes + 4);
	return true;
}

static bool frame_index_supported(uint8_t frame_index)
{
	return frame_index == UVC_FRAME_INDEX_1280X720 || frame_index == UVC_FRAME_INDEX_640X480;
}

static uint8_t probe_request(struct uvc_device *device, uint8_t request, uint16_t length, const uint8_t *data, size_t data_length, struct uvc_reply *reply)
{
	struct uvc_probe probe;

	switch (request) {
	case UVC_SET_CUR:
		if (!decode_probe(data, data_length, &device->probe))
			return UVC_ERROR_INVALID_REQUEST;
		device->probe.format_index = UVC_FORMAT_INDEX;
		if (!frame_index_supported(device->probe.frame_index))
			device->probe.frame_index = UVC_DEFAULT_FRAME_INDEX;
		device->probe.frame_interval = UVC_FRAME_INTERVAL;
		return UVC_ERROR_NONE;
	case UVC_GET_CUR:
		return reply_probe(&device->probe, length, reply);
	case UVC_GET_MIN:
		uvc_default_probe(&probe);
		probe.frame_index = UVC_FRAME_INDEX_640X480;
		return reply_probe(&probe, length, reply);
	case UVC_GET_MAX:
	case UVC_GET_DEF:
		uvc_default_probe(&probe);
		return reply_probe(&probe, length, reply);
	case UVC_GET_RES:
		return reply_probe_resolution(length, reply);
	case UVC_GET_LEN:
		return reply_probe_length(length, reply);
	case UVC_GET_INFO:
		return reply_byte(UVC_INFO_GET_SET, length, reply);
	default:
		return UVC_ERROR_INVALID_REQUEST;
	}
}

static uint8_t commit_request(struct uvc_device *device, uint8_t request, uint16_t length, const uint8_t *data, size_t data_length, struct uvc_reply *reply)
{
	struct uvc_probe probe;

	switch (request) {
	case UVC_SET_CUR:
		if (!decode_probe(data, data_length, &probe))
			return UVC_ERROR_INVALID_REQUEST;
		if (probe.format_index != UVC_FORMAT_INDEX || !frame_index_supported(probe.frame_index) || probe.frame_interval != UVC_FRAME_INTERVAL)
			return UVC_ERROR_OUT_OF_RANGE;
		device->commit = probe;
		device->has_commit = true;
		reply->committed = true;
		return UVC_ERROR_NONE;
	case UVC_GET_CUR:
		return reply_probe(&device->commit, length, reply);
	case UVC_GET_LEN:
		return reply_probe_length(length, reply);
	case UVC_GET_INFO:
		return reply_byte(UVC_INFO_GET_SET, length, reply);
	default:
		return UVC_ERROR_INVALID_REQUEST;
	}
}

static uint8_t control_interface_request(struct uvc_device *device, uint8_t entity, uint8_t selector, uint8_t request, uint16_t length, struct uvc_reply *reply)
{
	if (entity == UVC_CAMERA_TERMINAL_ID || entity == UVC_OUTPUT_TERMINAL_ID)
		return UVC_ERROR_INVALID_CONTROL;
	if (entity != 0)
		return UVC_ERROR_INVALID_UNIT;
	if (selector != UVC_VC_REQUEST_ERROR_CODE_CONTROL)
		return UVC_ERROR_INVALID_CONTROL;
	return byte_control(request, device->request_error, length, reply);
}

static uint8_t streaming_interface_request(struct uvc_device *device, uint8_t entity, uint8_t selector, uint8_t request, uint16_t length, const uint8_t *data, size_t data_length, struct uvc_reply *reply)
{
	if (entity != 0)
		return UVC_ERROR_INVALID_UNIT;
	if (selector == UVC_VS_PROBE_CONTROL)
		return probe_request(device, request, length, data, data_length, reply);
	if (selector == UVC_VS_COMMIT_CONTROL)
		return commit_request(device, request, length, data, data_length, reply);
	if (selector == UVC_VS_STREAM_ERROR_CODE_CONTROL)
		return byte_control(request, UVC_ERROR_NONE, length, reply);
	return UVC_ERROR_INVALID_CONTROL;
}

static void handle_class(struct uvc_device *device, uint8_t requesttype, uint8_t request, uint16_t value, uint16_t index, uint16_t length, const uint8_t *data, size_t data_length, struct uvc_reply *reply)
{
	uint8_t interface = index & 0xFF;
	uint8_t entity = index >> 8;
	uint8_t selector = value >> 8;
	uint8_t error;

	if (requesttype != (request == UVC_SET_CUR ? UVC_CLASS_SET : UVC_CLASS_GET))
		error = UVC_ERROR_INVALID_REQUEST;
	else if (interface == UVC_CONTROL_INTERFACE)
		error = control_interface_request(device, entity, selector, request, length, reply);
	else if (interface == UVC_STREAMING_INTERFACE)
		error = streaming_interface_request(device, entity, selector, request, length, data, data_length, reply);
	else
		error = UVC_ERROR_INVALID_UNIT;
	device->request_error = error;
	if (error != UVC_ERROR_NONE) {
		reply->stall = true;
		reply->length = 0;
		reply->committed = false;
	}
}

void uvc_handle_control(struct uvc_device *device, uint8_t requesttype, uint8_t request, uint16_t value, uint16_t index, uint16_t length, const uint8_t *data, size_t data_length, struct uvc_reply *reply)
{
	memset(reply, 0, sizeof *reply);
	if ((requesttype & USB_TYPE_MASK) == USB_TYPE_STANDARD)
		handle_standard(device, requesttype, request, value, index, length, reply);
	else if ((requesttype & USB_TYPE_MASK) == USB_TYPE_CLASS)
		handle_class(device, requesttype, request, value, index, length, data, data_length, reply);
	else
		reply->stall = true;
}
