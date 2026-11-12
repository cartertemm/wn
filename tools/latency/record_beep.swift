// record_beep: record the default input (the built-in mic) and find the first sound onset.
//
// Usage: record_beep [--seconds S] [--freq HZ] [--factor F] [--settle-ms MS] [--floor-ms MS] [--window-ms MS]
//                    [--play-tone-at-ms MS] [--wav PATH]
//
// Prints "ready <start_host_ns>" when the first input buffer arrives, then one JSON
// object when done. Host times are mach_absolute_time in ns, the same clock as
// Python's time.clock_gettime_ns(time.CLOCK_UPTIME_RAW).
// --freq 0 (the default) uses broadband RMS. Any other value uses Goertzel energy at
// that frequency. The first --settle-ms are skipped (the mic ramps up from silence),
// the next --floor-ms set the noise floor. The onset is the first window above
// max(factor * floor, 30% of the peak), and only counts if the peak is above
// factor * floor. --play-tone-at-ms plays a 1 kHz tone on the default output at that
// time after the start, to measure the Mac's own output plus input latency.
// Exit codes: 0 done, 2 no microphone permission, 3 audio engine error, 64 usage.

import AVFoundation
import CoreAudio
import Foundation

let exitNoPermission: Int32 = 2
let exitEngine: Int32 = 3
let exitUsage: Int32 = 64
let nanosecondsPerSecond = 1_000_000_000.0
let calibrationToneHz = 1000.0
let calibrationToneSeconds = 0.15
let calibrationToneAmplitude: Float = 0.5
let hopMilliseconds = 0.25
let peakFraction = 0.3
let toneSpanMilliseconds = 300.0

struct Options {
	var seconds = 2.0
	var freq = 0.0
	var factor = 3.0
	var settleMilliseconds = 250.0
	var floorMilliseconds = 300.0
	var windowMilliseconds = 10.0
	var playToneAtMilliseconds: Double? = nil
	var wavPath: String? = nil
}

func fail(_ message: String, _ code: Int32) -> Never {
	FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
	print("{\"error\": \"\(message)\"}")
	exit(code)
}

func parseOptions() -> Options {
	var options = Options()
	var arguments = Array(CommandLine.arguments.dropFirst())
	while !arguments.isEmpty {
		let name = arguments.removeFirst()
		guard let value = arguments.first else { fail("missing value for \(name)", exitUsage) }
		arguments.removeFirst()
		switch name {
		case "--seconds": options.seconds = Double(value) ?? options.seconds
		case "--freq": options.freq = Double(value) ?? options.freq
		case "--factor": options.factor = Double(value) ?? options.factor
		case "--settle-ms": options.settleMilliseconds = Double(value) ?? options.settleMilliseconds
		case "--floor-ms": options.floorMilliseconds = Double(value) ?? options.floorMilliseconds
		case "--window-ms": options.windowMilliseconds = Double(value) ?? options.windowMilliseconds
		case "--play-tone-at-ms": options.playToneAtMilliseconds = Double(value)
		case "--wav": options.wavPath = value
		default: fail("unknown option \(name)", exitUsage)
		}
	}
	return options
}

var timebase = mach_timebase_info_data_t()
mach_timebase_info(&timebase)

func hostTimeToNanoseconds(_ hostTime: UInt64) -> UInt64 {
	return hostTime * UInt64(timebase.numer) / UInt64(timebase.denom)
}

func nanosecondsToHostTime(_ nanoseconds: UInt64) -> UInt64 {
	return nanoseconds * UInt64(timebase.denom) / UInt64(timebase.numer)
}

func checkMicrophonePermission() {
	switch AVCaptureDevice.authorizationStatus(for: .audio) {
	case .authorized:
		return
	case .notDetermined:
		let semaphore = DispatchSemaphore(value: 0)
		var granted = false
		AVCaptureDevice.requestAccess(for: .audio) { answer in
			granted = answer
			semaphore.signal()
		}
		semaphore.wait()
		if !granted {
			fail("microphone permission denied. Allow it for your terminal in System Settings > Privacy & Security > Microphone", exitNoPermission)
		}
	default:
		fail("microphone permission denied. Allow it for your terminal in System Settings > Privacy & Security > Microphone", exitNoPermission)
	}
}

func windowEnergy(_ samples: [Float], _ start: Int, _ length: Int, _ freq: Double, _ sampleRate: Double) -> Double {
	if freq <= 0 {
		var sum = 0.0
		for index in start..<(start + length) {
			sum += Double(samples[index]) * Double(samples[index])
		}
		return (sum / Double(length)).squareRoot()
	}
	let coefficient = 2 * cos(2 * Double.pi * freq / sampleRate)
	var previous = 0.0
	var beforePrevious = 0.0
	for index in start..<(start + length) {
		let current = Double(samples[index]) + coefficient * previous - beforePrevious
		beforePrevious = previous
		previous = current
	}
	let power = previous * previous + beforePrevious * beforePrevious - coefficient * previous * beforePrevious
	return power.squareRoot() / Double(length)
}

struct Analysis {
	var thresholdEnergy = 0.0
	var floorEnergy = 0.0
	var peakEnergy = 0.0
	var onsetMilliseconds: Double? = nil
	var aboveMilliseconds = 0.0
	var maxGapMilliseconds = 0.0
}

func energies(_ samples: [Float], _ sampleRate: Double, _ options: Options) -> [Double] {
	let window = max(1, Int(options.windowMilliseconds / 1000 * sampleRate))
	let hop = max(1, Int(hopMilliseconds / 1000 * sampleRate))
	var result = [Double]()
	var start = 0
	while start + window <= samples.count {
		result.append(windowEnergy(samples, start, window, options.freq, sampleRate))
		start += hop
	}
	return result
}

func analyze(_ samples: [Float], _ sampleRate: Double, _ options: Options) -> Analysis {
	var analysis = Analysis()
	let values = energies(samples, sampleRate, options)
	let floorStart = Int(options.settleMilliseconds / hopMilliseconds)
	let floorEnd = Int((options.settleMilliseconds + options.floorMilliseconds) / hopMilliseconds)
	guard values.count > floorEnd else { return analysis }
	analysis.floorEnergy = values[floorStart..<floorEnd].max() ?? 0
	analysis.peakEnergy = values[floorEnd...].max() ?? 0
	analysis.thresholdEnergy = max(analysis.floorEnergy * options.factor, analysis.peakEnergy * peakFraction)
	guard analysis.peakEnergy > analysis.floorEnergy * options.factor,
		let onsetIndex = values[floorEnd...].firstIndex(where: { $0 > analysis.thresholdEnergy }) else { return analysis }
	analysis.onsetMilliseconds = Double(onsetIndex) * hopMilliseconds
	let spanEnd = min(values.count, onsetIndex + Int(toneSpanMilliseconds / hopMilliseconds))
	var lastAbove = onsetIndex
	for index in onsetIndex..<spanEnd where values[index] > analysis.thresholdEnergy {
		analysis.aboveMilliseconds += hopMilliseconds
		analysis.maxGapMilliseconds = max(analysis.maxGapMilliseconds, Double(index - lastAbove - 1) * hopMilliseconds)
		lastAbove = index
	}
	return analysis
}

func writeWav(_ samples: [Float], _ sampleRate: Double, _ path: String) {
	guard let format = AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: 1),
		let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)) else { return }
	buffer.frameLength = AVAudioFrameCount(samples.count)
	samples.withUnsafeBufferPointer { source in
		buffer.floatChannelData![0].update(from: source.baseAddress!, count: samples.count)
	}
	if let file = try? AVAudioFile(forWriting: URL(fileURLWithPath: path), settings: format.settings) {
		try? file.write(from: buffer)
	}
}

func makeToneBuffer(_ format: AVAudioFormat) -> AVAudioPCMBuffer {
	let frames = AVAudioFrameCount(calibrationToneSeconds * format.sampleRate)
	let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames)!
	buffer.frameLength = frames
	for channel in 0..<Int(format.channelCount) {
		for frame in 0..<Int(frames) {
			buffer.floatChannelData![channel][frame] = calibrationToneAmplitude * Float(sin(2 * Double.pi * calibrationToneHz * Double(frame) / format.sampleRate))
		}
	}
	return buffer
}

func deviceUInt32(_ device: AudioObjectID, _ selector: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope) -> UInt32 {
	var address = AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
	var value: UInt32 = 0
	var size = UInt32(MemoryLayout<UInt32>.size)
	AudioObjectGetPropertyData(device, &address, 0, nil, &size, &value)
	return value
}

func defaultDevice(_ selector: AudioObjectPropertySelector) -> AudioObjectID {
	var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
	var device = AudioObjectID(0)
	var size = UInt32(MemoryLayout<AudioObjectID>.size)
	AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &device)
	return device
}

func streamLatency(_ device: AudioObjectID, _ scope: AudioObjectPropertyScope) -> UInt32 {
	var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams, mScope: scope, mElement: kAudioObjectPropertyElementMain)
	var size: UInt32 = 0
	AudioObjectGetPropertyDataSize(device, &address, 0, nil, &size)
	var streams = [AudioStreamID](repeating: 0, count: Int(size) / MemoryLayout<AudioStreamID>.size)
	AudioObjectGetPropertyData(device, &address, 0, nil, &size, &streams)
	guard let first = streams.first else { return 0 }
	return deviceUInt32(first, kAudioStreamPropertyLatency, kAudioObjectPropertyScopeGlobal)
}

// Frames reported by CoreAudio for the default device in one direction: device latency,
// safety offset, stream latency, and I/O buffer size, plus the nominal rate.
func deviceLatency(_ selector: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope) -> [String: Any] {
	let device = defaultDevice(selector)
	var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyNominalSampleRate, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
	var rate = Float64(0)
	var size = UInt32(MemoryLayout<Float64>.size)
	AudioObjectGetPropertyData(device, &address, 0, nil, &size, &rate)
	return [
		"rate": rate,
		"device_latency": deviceUInt32(device, kAudioDevicePropertyLatency, scope),
		"safety_offset": deviceUInt32(device, kAudioDevicePropertySafetyOffset, scope),
		"stream_latency": streamLatency(device, scope),
		"buffer_frames": deviceUInt32(device, kAudioDevicePropertyBufferFrameSize, scope),
	]
}

let options = parseOptions()
checkMicrophonePermission()

let engine = AVAudioEngine()
let input = engine.inputNode
let inputFormat = input.inputFormat(forBus: 0)
let sampleRate = inputFormat.sampleRate
if sampleRate <= 0 { fail("no input device", exitEngine) }
let totalFrames = Int(options.seconds * sampleRate)
let lock = NSLock()
var samples = [Float]()
samples.reserveCapacity(totalFrames)
var startHostNanoseconds: UInt64? = nil
var startUnixNanoseconds: UInt64 = 0
let finished = DispatchSemaphore(value: 0)
var signaled = false
var outputEngine: AVAudioEngine? = nil
var toneScheduledNanoseconds: UInt64? = nil

func startCalibrationTone(_ startNanoseconds: UInt64, _ delayMilliseconds: Double) {
	let output = AVAudioEngine()
	let player = AVAudioPlayerNode()
	output.attach(player)
	let format = output.mainMixerNode.outputFormat(forBus: 0)
	output.connect(player, to: output.mainMixerNode, format: format)
	do { try output.start() } catch { fail("cannot start output engine: \(error)", exitEngine) }
	let toneNanoseconds = startNanoseconds + UInt64(delayMilliseconds * 1_000_000)
	toneScheduledNanoseconds = toneNanoseconds
	player.scheduleBuffer(makeToneBuffer(format), at: AVAudioTime(hostTime: nanosecondsToHostTime(toneNanoseconds)))
	player.play()
	outputEngine = output
}

input.installTap(onBus: 0, bufferSize: 512, format: inputFormat) { buffer, when in
	lock.lock()
	defer { lock.unlock() }
	if signaled { return }
	if startHostNanoseconds == nil {
		let nowHost = mach_absolute_time()
		var now = timespec()
		clock_gettime(CLOCK_REALTIME, &now)
		let firstNanoseconds = hostTimeToNanoseconds(when.hostTime)
		startHostNanoseconds = firstNanoseconds
		let unixNow = UInt64(now.tv_sec) * UInt64(nanosecondsPerSecond) + UInt64(now.tv_nsec)
		startUnixNanoseconds = unixNow - (hostTimeToNanoseconds(nowHost) - firstNanoseconds)
		print("ready \(firstNanoseconds)")
		fflush(stdout)
		if let delay = options.playToneAtMilliseconds {
			DispatchQueue.main.async { startCalibrationTone(firstNanoseconds, delay) }
		}
	}
	let channel = buffer.floatChannelData![0]
	let count = min(Int(buffer.frameLength), totalFrames - samples.count)
	samples.append(contentsOf: UnsafeBufferPointer(start: channel, count: count))
	if samples.count >= totalFrames {
		signaled = true
		finished.signal()
	}
}

do {
	try engine.start()
} catch {
	fail("cannot start audio engine: \(error)", exitEngine)
}

DispatchQueue.global().async {
	finished.wait()
	engine.stop()
	input.removeTap(onBus: 0)
	lock.lock()
	let recorded = samples
	lock.unlock()
	let analysis = analyze(recorded, sampleRate, options)
	if let path = options.wavPath { writeWav(recorded, sampleRate, path) }
	var result: [String: Any] = [
		"sample_rate": sampleRate,
		"frames": recorded.count,
		"start_host_ns": startHostNanoseconds ?? 0,
		"start_unix_ns": startUnixNanoseconds,
		"freq": options.freq,
		"floor_energy": analysis.floorEnergy,
		"threshold_energy": analysis.thresholdEnergy,
		"peak_energy": analysis.peakEnergy,
		"above_ms": analysis.aboveMilliseconds,
		"max_gap_ms": analysis.maxGapMilliseconds,
		"output_device": deviceLatency(kAudioHardwarePropertyDefaultOutputDevice, kAudioObjectPropertyScopeOutput),
		"input_device": deviceLatency(kAudioHardwarePropertyDefaultInputDevice, kAudioObjectPropertyScopeInput),
	]
	if let onset = analysis.onsetMilliseconds {
		result["onset_ms"] = onset
		result["onset_host_ns"] = (startHostNanoseconds ?? 0) + UInt64(onset * 1_000_000)
	} else {
		result["onset_ms"] = NSNull()
	}
	if let tone = toneScheduledNanoseconds { result["tone_host_ns"] = tone }
	let data = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
	print(String(data: data, encoding: .utf8)!)
	fflush(stdout)
	exit(0)
}

dispatchMain()
