#ifndef WN_CAMERA_CAPTURE_H
#define WN_CAMERA_CAPTURE_H

#include <stdbool.h>
#include <stdint.h>

typedef void (*capture_frame_ready)(void);

void camera_log(const char *format, ...) __attribute__((format(printf, 1, 2)));

void capture_init(bool test_pattern, capture_frame_ready frame_ready);
void capture_start(uint16_t width, uint16_t height);
void capture_stop(void);
bool capture_active(void);
bool capture_copy_frame(uint8_t *destination, uint16_t width, uint16_t height, uint64_t *sequence);
void capture_finish(void);

#endif
