#ifndef WN_CAMERA_UVC_H
#define WN_CAMERA_UVC_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define UVC_VENDOR_ID 0x1209
#define UVC_PRODUCT_ID 0x0001
#define UVC_DEVICE_BCD 0x0101

#define UVC_DEVICE_CLASS 0xEF
#define UVC_DEVICE_SUBCLASS 0x02
#define UVC_DEVICE_PROTOCOL 0x01
#define UVC_VIDEO_CLASS 0x0E
#define UVC_VIDEOCONTROL_SUBCLASS 0x01
#define UVC_VIDEOSTREAMING_SUBCLASS 0x02
#define UVC_CONTROL_INTERFACE 0
#define UVC_STREAMING_INTERFACE 1
#define UVC_STREAMING_ENDPOINT 0x81
#define UVC_STREAMING_MAX_PACKET 512
#define UVC_CONTROL_MAX_PACKET 64
#define UVC_CONFIGURATION_VALUE 1

#define UVC_DEVICE_DESCRIPTOR_SIZE 18
#define UVC_CONFIGURATION_DESCRIPTOR_SIZE 189
#define UVC_PROBE_SIZE 34
#define UVC_REPLY_CAPACITY 256
#define UVC_FORMAT_INDEX 1
#define UVC_FRAME_INDEX_1280X720 1
#define UVC_FRAME_INDEX_640X480 2
#define UVC_FRAME_INTERVAL 333333
#define UVC_BITS_PER_PIXEL 12
#define UVC_PAYLOAD_HEADER_SIZE 2
#define UVC_PAYLOAD_MAX 65534
#define UVC_MAX_PAYLOAD_TRANSFER 65536
#define UVC_HEADER_FRAME_ID 0x01
#define UVC_HEADER_END_OF_FRAME 0x02
#define UVC_HEADER_END_OF_HEADER 0x80
#define UVC_IDLE_SECONDS 3.0

struct uvc_probe {
	uint16_t hint;
	uint8_t format_index;
	uint8_t frame_index;
	uint32_t frame_interval;
};

struct uvc_reply {
	bool stall;
	bool stream_stop;
	bool committed;
	uint16_t length;
	uint8_t data[UVC_REPLY_CAPACITY];
};

struct uvc_device {
	uint8_t configuration;
	bool halted;
	uint8_t request_error;
	struct uvc_probe probe;
	struct uvc_probe commit;
	bool has_commit;
};

struct uvc_stream {
	uint8_t *frame;
	size_t frame_capacity;
	size_t frame_size;
	size_t frame_offset;
	uint8_t frame_id;
};

struct uvc_request {
	uint64_t id;
	uint32_t length;
};

struct uvc_request_queue {
	struct uvc_request *items;
	size_t head;
	size_t count;
	size_t capacity;
};

extern const uint8_t uvc_device_descriptor[UVC_DEVICE_DESCRIPTOR_SIZE];
extern const uint8_t uvc_configuration_descriptor[UVC_CONFIGURATION_DESCRIPTOR_SIZE];

uint16_t uvc_frame_width(uint8_t frame_index);
uint16_t uvc_frame_height(uint8_t frame_index);
size_t uvc_frame_bytes(uint8_t frame_index);
void uvc_default_probe(struct uvc_probe *probe);
void uvc_encode_probe(const struct uvc_probe *probe, uint8_t *out);
void uvc_device_reset(struct uvc_device *device);
void uvc_handle_control(struct uvc_device *device, uint8_t requesttype, uint8_t request, uint16_t value, uint16_t index, uint16_t length, const uint8_t *data, size_t data_length, struct uvc_reply *reply);

uint8_t *uvc_stream_reserve(struct uvc_stream *stream, size_t frame_size);
void uvc_stream_load(struct uvc_stream *stream, size_t frame_size);
bool uvc_stream_has_frame(const struct uvc_stream *stream);
void uvc_stream_drop_frame(struct uvc_stream *stream);
size_t uvc_pack_payload(struct uvc_stream *stream, uint32_t request_length, uint8_t *out);
void uvc_stream_free(struct uvc_stream *stream);

void uvc_queue_push(struct uvc_request_queue *queue, uint64_t id, uint32_t length);
bool uvc_queue_peek(const struct uvc_request_queue *queue, struct uvc_request *request);
bool uvc_queue_pop(struct uvc_request_queue *queue, struct uvc_request *request);
bool uvc_queue_remove(struct uvc_request_queue *queue, uint64_t id);
void uvc_queue_clear(struct uvc_request_queue *queue);

bool uvc_idle_expired(double last_arrival, double now);

#endif
