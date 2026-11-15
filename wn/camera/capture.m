#import <AVFoundation/AVFoundation.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <VideoToolbox/VideoToolbox.h>
#include <os/lock.h>
#include <signal.h>
#include <stdatomic.h>
#include <string.h>

#include "capture.h"

#define FRAMES_PER_SECOND 30
#define BYTES_PER_PIXEL 2
#define PATTERN_FIRST_LEVEL 16
#define PATTERN_LEVELS 200
#define PATTERN_REFUSALS 2

@interface WNCaptureDelegate : NSObject <AVCaptureVideoDataOutputSampleBufferDelegate>
@end

static os_unfair_lock slot_lock = OS_UNFAIR_LOCK_INIT;
static CVPixelBufferRef slot_buffer;
static uint64_t slot_sequence;
static _Atomic uint32_t target_size;
static atomic_bool running;
static bool use_test_pattern;
static capture_frame_ready notify_frame_ready;
static dispatch_queue_t control_queue;
static dispatch_queue_t frame_queue;
static dispatch_source_t pattern_timer;
static dispatch_source_t pattern_stop_signal;
static int pattern_refusals_left;
static uint64_t pattern_count;
static VTPixelTransferSessionRef transfer;
static WNCaptureDelegate *delegate;
static AVCaptureSession *session;
static AVCaptureDevice *device;
static AVCaptureVideoDataOutput *output;
static NSArray *observers;
static bool observing_suspended;
static NSString *last_failure;
static atomic_bool frame_seen;
static bool frame_rate_logged;

static uint32_t pack_size(size_t width, size_t height)
{
	return (uint32_t)(width << 16 | height);
}

static size_t size_width(uint32_t size)
{
	return size >> 16;
}

static size_t size_height(uint32_t size)
{
	return size & 0xFFFF;
}

static void store_frame(CVPixelBufferRef buffer)
{
	CVPixelBufferRef previous;

	os_unfair_lock_lock(&slot_lock);
	if (!atomic_load(&running)) {
		os_unfair_lock_unlock(&slot_lock);
		return;
	}
	previous = slot_buffer;
	slot_buffer = CVPixelBufferRetain(buffer);
	slot_sequence++;
	os_unfair_lock_unlock(&slot_lock);
	CVPixelBufferRelease(previous);
	notify_frame_ready();
}

static void clear_frame(void)
{
	CVPixelBufferRef previous;

	os_unfair_lock_lock(&slot_lock);
	previous = slot_buffer;
	slot_buffer = NULL;
	os_unfair_lock_unlock(&slot_lock);
	CVPixelBufferRelease(previous);
}

static CVPixelBufferRef create_frame_buffer(uint32_t size)
{
	NSDictionary *attributes = @{(id)kCVPixelBufferIOSurfacePropertiesKey: @{}};
	CVPixelBufferRef buffer = NULL;

	CVPixelBufferCreate(kCFAllocatorDefault, size_width(size), size_height(size), kCVPixelFormatType_422YpCbCr8_yuvs, (__bridge CFDictionaryRef)attributes, &buffer);
	return buffer;
}

static void store_camera_frame(CVPixelBufferRef buffer)
{
	uint32_t size = atomic_load(&target_size);
	CVPixelBufferRef scaled;

	if (pack_size(CVPixelBufferGetWidth(buffer), CVPixelBufferGetHeight(buffer)) == size && CVPixelBufferGetPixelFormatType(buffer) == kCVPixelFormatType_422YpCbCr8_yuvs) {
		store_frame(buffer);
		return;
	}
	scaled = create_frame_buffer(size);
	if (scaled && VTPixelTransferSessionTransferImage(transfer, buffer, scaled) == noErr)
		store_frame(scaled);
	CVPixelBufferRelease(scaled);
}

static void log_failure(NSString *message)
{
	if ([message isEqualToString:last_failure])
		return;
	camera_log("%s", message.UTF8String);
	last_failure = message;
}

static void tear_down(void)
{
	if (!session)
		return;
	[session stopRunning];
	for (id observer in observers)
		[[NSNotificationCenter defaultCenter] removeObserver:observer];
	if (observing_suspended)
		[device removeObserver:delegate forKeyPath:@"suspended"];
	observing_suspended = false;
	observers = nil;
	output = nil;
	device = nil;
	session = nil;
}

static void fail_session(NSString *reason)
{
	dispatch_async(control_queue, ^{
		if (!session)
			return;
		log_failure([NSString stringWithFormat:@"camera stopped: %@", reason]);
		tear_down();
		atomic_store(&running, false);
		clear_frame();
	});
}

@implementation WNCaptureDelegate

- (void)captureOutput:(AVCaptureOutput *)captureOutput didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer fromConnection:(AVCaptureConnection *)connection
{
	CVPixelBufferRef buffer = CMSampleBufferGetImageBuffer(sampleBuffer);

	if (!buffer || !atomic_load(&running))
		return;
	if (!atomic_exchange(&frame_seen, true)) {
		dispatch_async(control_queue, ^{
			last_failure = nil;
		});
	}
	store_camera_frame(buffer);
}

- (void)observeValueForKeyPath:(NSString *)keyPath ofObject:(id)object change:(NSDictionary *)change context:(void *)context
{
	if ([object isSuspended])
		fail_session(@"the camera is suspended, the lid may be closed");
}

@end

static id observe(NSNotificationName name, id object, NSString *reason)
{
	return [[NSNotificationCenter defaultCenter] addObserverForName:name object:object queue:nil usingBlock:^(NSNotification *notification) {
		NSError *error = notification.userInfo[AVCaptureSessionErrorKey];
		fail_session(error ? [NSString stringWithFormat:@"%@: %@", reason, error.localizedDescription] : reason);
	}];
}

static bool build_session(void)
{
	AVCaptureDeviceInput *input;
	NSError *error = nil;

	device = [AVCaptureDevice defaultDeviceWithMediaType:AVMediaTypeVideo];
	if (!device) {
		log_failure(@"camera not found");
		return false;
	}
	input = [AVCaptureDeviceInput deviceInputWithDevice:device error:&error];
	if (!input) {
		log_failure([NSString stringWithFormat:@"cannot open the camera: %@", error.localizedDescription]);
		device = nil;
		return false;
	}
	session = [[AVCaptureSession alloc] init];
	output = [[AVCaptureVideoDataOutput alloc] init];
	output.alwaysDiscardsLateVideoFrames = YES;
	[output setSampleBufferDelegate:delegate queue:frame_queue];
	if (![session canAddInput:input] || ![session canAddOutput:output]) {
		log_failure(@"cannot use the camera in a capture session");
		output = nil;
		device = nil;
		session = nil;
		return false;
	}
	[session beginConfiguration];
	[session addInput:input];
	[session addOutput:output];
	[session commitConfiguration];
	[device addObserver:delegate forKeyPath:@"suspended" options:0 context:NULL];
	observing_suspended = true;
	observers = @[
		observe(AVCaptureDeviceWasDisconnectedNotification, device, @"the camera was disconnected"),
		observe(AVCaptureSessionRuntimeErrorNotification, session, @"camera error"),
		observe(AVCaptureSessionWasInterruptedNotification, session, @"the camera session was interrupted"),
	];
	return true;
}

static bool supports_frame_rate(AVCaptureDeviceFormat *format)
{
	CMTime duration = CMTimeMake(1, FRAMES_PER_SECOND);

	for (AVFrameRateRange *range in format.videoSupportedFrameRateRanges) {
		if (CMTimeCompare(range.minFrameDuration, duration) <= 0 && CMTimeCompare(range.maxFrameDuration, duration) >= 0)
			return true;
	}
	return false;
}

static void run_session(uint32_t size)
{
	NSError *error = nil;

	[session beginConfiguration];
	output.videoSettings = @{
		(id)kCVPixelBufferPixelFormatTypeKey: @(kCVPixelFormatType_422YpCbCr8_yuvs),
		(id)kCVPixelBufferWidthKey: @(size_width(size)),
		(id)kCVPixelBufferHeightKey: @(size_height(size)),
	};
	[session commitConfiguration];
	if (!session.running)
		[session startRunning];
	if (!supports_frame_rate(device.activeFormat)) {
		if (!frame_rate_logged)
			camera_log("the camera does not support %d fps, using its default frame rate", FRAMES_PER_SECOND);
		frame_rate_logged = true;
		return;
	}
	if (![device lockForConfiguration:&error]) {
		camera_log("cannot set 30 fps: %s", error.localizedDescription.UTF8String);
		return;
	}
	@try {
		device.activeVideoMinFrameDuration = CMTimeMake(1, FRAMES_PER_SECOND);
		device.activeVideoMaxFrameDuration = CMTimeMake(1, FRAMES_PER_SECOND);
	} @finally {
		[device unlockForConfiguration];
	}
}

static void start_camera(uint32_t size)
{
	@try {
		if (!session && !build_session()) {
			atomic_store(&running, false);
			return;
		}
		if (device.isSuspended) {
			log_failure(@"the camera is suspended, the lid may be closed");
			tear_down();
			atomic_store(&running, false);
			return;
		}
		atomic_store(&frame_seen, false);
		run_session(size);
	} @catch (NSException *exception) {
		log_failure([NSString stringWithFormat:@"camera failed: %@", exception.reason]);
		tear_down();
		atomic_store(&running, false);
	}
}

static void emit_pattern_frame(void)
{
	CVPixelBufferRef buffer;

	if (!atomic_load(&running))
		return;
	buffer = create_frame_buffer(atomic_load(&target_size));
	if (!buffer)
		return;
	CVPixelBufferLockBaseAddress(buffer, 0);
	memset(CVPixelBufferGetBaseAddress(buffer), PATTERN_FIRST_LEVEL + pattern_count % PATTERN_LEVELS, CVPixelBufferGetBytesPerRow(buffer) * CVPixelBufferGetHeight(buffer));
	CVPixelBufferUnlockBaseAddress(buffer, 0);
	pattern_count++;
	store_frame(buffer);
	CVPixelBufferRelease(buffer);
}

static void start_pattern(void)
{
	if (pattern_timer)
		return;
	pattern_timer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, frame_queue);
	dispatch_source_set_timer(pattern_timer, dispatch_time(DISPATCH_TIME_NOW, 0), NSEC_PER_SEC / FRAMES_PER_SECOND, 0);
	dispatch_source_set_event_handler(pattern_timer, ^{
		emit_pattern_frame();
	});
	dispatch_resume(pattern_timer);
}

static void stop_pattern(void)
{
	if (!pattern_timer)
		return;
	dispatch_source_cancel(pattern_timer);
	pattern_timer = nil;
}

static void start_pattern_if_allowed(void)
{
	if (pattern_refusals_left) {
		pattern_refusals_left--;
		atomic_store(&running, false);
		return;
	}
	start_pattern();
}

static void fail_pattern(void)
{
	if (!pattern_timer)
		return;
	camera_log("camera stopped: the test pattern was told to stop");
	stop_pattern();
	pattern_refusals_left = PATTERN_REFUSALS;
	atomic_store(&running, false);
	clear_frame();
}

static void watch_pattern_stop_signal(void)
{
	signal(SIGUSR1, SIG_IGN);
	pattern_stop_signal = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, SIGUSR1, 0, control_queue);
	dispatch_source_set_event_handler(pattern_stop_signal, ^{
		fail_pattern();
	});
	dispatch_resume(pattern_stop_signal);
}

void capture_init(bool test_pattern, capture_frame_ready frame_ready)
{
	use_test_pattern = test_pattern;
	notify_frame_ready = frame_ready;
	control_queue = dispatch_queue_create("wn.camera.control", DISPATCH_QUEUE_SERIAL);
	frame_queue = dispatch_queue_create("wn.camera.frames", DISPATCH_QUEUE_SERIAL);
	delegate = [[WNCaptureDelegate alloc] init];
	VTPixelTransferSessionCreate(kCFAllocatorDefault, &transfer);
	VTSessionSetProperty(transfer, kVTPixelTransferPropertyKey_DestinationYCbCrMatrix, kCVImageBufferYCbCrMatrix_ITU_R_709_2);
	VTSessionSetProperty(transfer, kVTPixelTransferPropertyKey_DestinationColorPrimaries, kCVImageBufferColorPrimaries_ITU_R_709_2);
	VTSessionSetProperty(transfer, kVTPixelTransferPropertyKey_DestinationTransferFunction, kCVImageBufferTransferFunction_ITU_R_709_2);
	if (test_pattern)
		watch_pattern_stop_signal();
}

void capture_start(uint16_t width, uint16_t height)
{
	uint32_t size = pack_size(width, height);

	atomic_store(&target_size, size);
	atomic_store(&running, true);
	dispatch_async(control_queue, ^{
		if (!atomic_load(&running))
			return;
		if (use_test_pattern)
			start_pattern_if_allowed();
		else
			start_camera(size);
	});
}

void capture_stop(void)
{
	atomic_store(&running, false);
	dispatch_async(control_queue, ^{
		if (atomic_load(&running))
			return;
		if (use_test_pattern)
			stop_pattern();
		else
			[session stopRunning];
		clear_frame();
	});
}

bool capture_active(void)
{
	return atomic_load(&running);
}

bool capture_copy_frame(uint8_t *destination, uint16_t width, uint16_t height, uint64_t *sequence)
{
	size_t row_bytes = (size_t)width * BYTES_PER_PIXEL;
	CVPixelBufferRef buffer;
	uint64_t buffer_sequence;
	bool copied = false;

	os_unfair_lock_lock(&slot_lock);
	buffer = slot_sequence != *sequence ? CVPixelBufferRetain(slot_buffer) : NULL;
	buffer_sequence = slot_sequence;
	os_unfair_lock_unlock(&slot_lock);
	if (!buffer)
		return false;
	if (CVPixelBufferGetWidth(buffer) == width && CVPixelBufferGetHeight(buffer) == height && CVPixelBufferLockBaseAddress(buffer, kCVPixelBufferLock_ReadOnly) == kCVReturnSuccess) {
		const uint8_t *source = CVPixelBufferGetBaseAddress(buffer);
		size_t stride = CVPixelBufferGetBytesPerRow(buffer);
		for (size_t row = 0; row < height; row++)
			memcpy(destination + row * row_bytes, source + row * stride, row_bytes);
		CVPixelBufferUnlockBaseAddress(buffer, kCVPixelBufferLock_ReadOnly);
		copied = true;
	}
	CVPixelBufferRelease(buffer);
	*sequence = buffer_sequence;
	return copied;
}

void capture_finish(void)
{
	atomic_store(&running, false);
	dispatch_sync(control_queue, ^{
		stop_pattern();
		tear_down();
		clear_frame();
	});
}
