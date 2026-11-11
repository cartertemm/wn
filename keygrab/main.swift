import ApplicationServices
import CoreGraphics
import Darwin
import Foundation

let programName = "wn-keygrab"

let usageExitCode: Int32 = 64
let permissionExitCode: Int32 = 2

enum Keycode {
	static let command: Int64 = 55
	static let rightCommand: Int64 = 54
	static let shift: Int64 = 56
	static let rightShift: Int64 = 60
	static let control: Int64 = 59
	static let rightControl: Int64 = 62
	static let option: Int64 = 58
	static let rightOption: Int64 = 61
	static let capsLock: Int64 = 57
	static let function: Int64 = 63
}

/// Per-key bits that AppKit calls device dependent. CGEventFlags only tells us
/// that some shift key is down, these bits tell us which one.
enum DeviceFlag {
	static let leftControl: UInt64 = 0x0000_0001
	static let leftShift: UInt64 = 0x0000_0002
	static let rightShift: UInt64 = 0x0000_0004
	static let leftCommand: UInt64 = 0x0000_0008
	static let rightCommand: UInt64 = 0x0000_0010
	static let leftOption: UInt64 = 0x0000_0020
	static let rightOption: UInt64 = 0x0000_0040
	static let rightControl: UInt64 = 0x0000_2000
}

struct ModifierKey {
	let keycode: Int64
	/// Bit in CGEventFlags that tracks this physical key.
	let mask: UInt64
	/// Bit that covers the whole modifier group, used as a fallback when the
	/// device dependent bit is missing.
	let groupMask: UInt64
}

let modifierKeys: [ModifierKey] = [
	ModifierKey(keycode: Keycode.shift, mask: DeviceFlag.leftShift, groupMask: CGEventFlags.maskShift.rawValue),
	ModifierKey(keycode: Keycode.rightShift, mask: DeviceFlag.rightShift, groupMask: CGEventFlags.maskShift.rawValue),
	ModifierKey(keycode: Keycode.control, mask: DeviceFlag.leftControl, groupMask: CGEventFlags.maskControl.rawValue),
	ModifierKey(keycode: Keycode.rightControl, mask: DeviceFlag.rightControl, groupMask: CGEventFlags.maskControl.rawValue),
	ModifierKey(keycode: Keycode.option, mask: DeviceFlag.leftOption, groupMask: CGEventFlags.maskAlternate.rawValue),
	ModifierKey(keycode: Keycode.rightOption, mask: DeviceFlag.rightOption, groupMask: CGEventFlags.maskAlternate.rawValue),
	ModifierKey(keycode: Keycode.command, mask: DeviceFlag.leftCommand, groupMask: CGEventFlags.maskCommand.rawValue),
	ModifierKey(keycode: Keycode.rightCommand, mask: DeviceFlag.rightCommand, groupMask: CGEventFlags.maskCommand.rawValue),
	ModifierKey(keycode: Keycode.capsLock, mask: CGEventFlags.maskAlphaShift.rawValue, groupMask: CGEventFlags.maskAlphaShift.rawValue),
	ModifierKey(keycode: Keycode.function, mask: CGEventFlags.maskSecondaryFn.rawValue, groupMask: CGEventFlags.maskSecondaryFn.rawValue)
]

let hotkeyModifierMasks: [String: UInt64] = [
	"control": CGEventFlags.maskControl.rawValue,
	"ctrl": CGEventFlags.maskControl.rawValue,
	"option": CGEventFlags.maskAlternate.rawValue,
	"alt": CGEventFlags.maskAlternate.rawValue,
	"command": CGEventFlags.maskCommand.rawValue,
	"cmd": CGEventFlags.maskCommand.rawValue,
	"shift": CGEventFlags.maskShift.rawValue,
	"capslock": CGEventFlags.maskAlphaShift.rawValue,
	"fn": CGEventFlags.maskSecondaryFn.rawValue,
	"function": CGEventFlags.maskSecondaryFn.rawValue
]

let defaultReleaseHotkey = "control,option,command"

func monotonicSeconds() -> Double {
	return ProcessInfo.processInfo.systemUptime
}

func emit(_ line: String) {
	print(line)
	fflush(stdout)
}

func emitKey(type: String, keycode: Int64, flags: UInt64, isRepeat: Bool) {
	let repeatField = isRepeat ? "\"repeat\":true," : ""
	emit("{\"type\":\"\(type)\",\(repeatField)\"keycode\":\(keycode),\"flags\":\(flags),\"ts\":\(String(format: "%.6f", monotonicSeconds()))}")
}

func emitGrabState(_ active: Bool) {
	emit("{\"type\":\"grab\",\"active\":\(active ? "true" : "false")}")
}

func writeStderr(_ message: String) {
	FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

final class KeyGrabber {
	private let hotkeyMask: UInt64
	private var tap: CFMachPort?
	private var runLoopSource: CFRunLoopSource?
	private var previousFlags: UInt64 = 0
	private var heldKeycodes: Set<Int64> = []
	private var hotkeyHeld = false
	private var grabbing: Bool

	init(hotkeyMask: UInt64, startGrabbed: Bool) {
		self.hotkeyMask = hotkeyMask
		self.grabbing = startGrabbed
	}

	func start() -> Bool {
		let mask = (1 << CGEventType.keyDown.rawValue) |
			(1 << CGEventType.keyUp.rawValue) |
			(1 << CGEventType.flagsChanged.rawValue)
		guard let tap = CGEvent.tapCreate(
			tap: .cghidEventTap,
			place: .headInsertEventTap,
			options: .defaultTap,
			eventsOfInterest: CGEventMask(mask),
			callback: { _, type, event, refcon in
				guard let refcon else { return Unmanaged.passUnretained(event) }
				let grabber = Unmanaged<KeyGrabber>.fromOpaque(refcon).takeUnretainedValue()
				return grabber.handle(type: type, event: event)
			},
			userInfo: Unmanaged.passUnretained(self).toOpaque()
		) else {
			return false
		}
		self.tap = tap
		let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
		self.runLoopSource = source
		CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
		CGEvent.tapEnable(tap: tap, enable: true)
		previousFlags = CGEventSource.flagsState(.combinedSessionState).rawValue
		emitGrabState(grabbing)
		return true
	}

	func setGrabbing(_ active: Bool) {
		guard active != grabbing else { return }
		if !active {
			releaseHeldKeys()
		}
		grabbing = active
		emitGrabState(active)
	}

	func shutDown() {
		releaseHeldKeys()
		if let tap {
			CGEvent.tapEnable(tap: tap, enable: false)
			if let runLoopSource {
				CFRunLoopRemoveSource(CFRunLoopGetMain(), runLoopSource, .commonModes)
			}
			CFMachPortInvalidate(tap)
		}
		tap = nil
		runLoopSource = nil
	}

	private func releaseHeldKeys() {
		let flags = previousFlags
		for keycode in heldKeycodes.sorted() {
			emitKey(type: "up", keycode: keycode, flags: flags, isRepeat: false)
		}
		heldKeycodes.removeAll()
	}

	private func handle(type: CGEventType, event: CGEvent) -> Unmanaged<CGEvent>? {
		switch type {
		case .tapDisabledByTimeout, .tapDisabledByUserInput:
			if let tap {
				CGEvent.tapEnable(tap: tap, enable: true)
			}
			let reason = type == .tapDisabledByTimeout ? "timeout" : "user-input"
			emit("{\"type\":\"tap\",\"state\":\"re-enabled\",\"reason\":\"\(reason)\"}")
			return nil
		case .flagsChanged:
			handleFlagsChanged(event)
		case .keyDown, .keyUp:
			handleKey(type: type, event: event)
		default:
			return Unmanaged.passUnretained(event)
		}
		return grabbing ? nil : Unmanaged.passUnretained(event)
	}

	private func handleKey(type: CGEventType, event: CGEvent) {
		guard grabbing else { return }
		let keycode = event.getIntegerValueField(.keyboardEventKeycode)
		let isRepeat = event.getIntegerValueField(.keyboardEventAutorepeat) != 0
		if type == .keyDown {
			heldKeycodes.insert(keycode)
			emitKey(type: "down", keycode: keycode, flags: event.flags.rawValue, isRepeat: isRepeat)
		} else {
			heldKeycodes.remove(keycode)
			emitKey(type: "up", keycode: keycode, flags: event.flags.rawValue, isRepeat: false)
		}
	}

	private func handleFlagsChanged(_ event: CGEvent) {
		let newFlags = event.flags.rawValue
		let changed = newFlags ^ previousFlags
		if grabbing {
			var reported = false
			for modifier in modifierKeys where changed & modifier.mask != 0 {
				let isDown = newFlags & modifier.mask != 0
				report(modifier: modifier.keycode, isDown: isDown, flags: newFlags)
				reported = true
			}
			if !reported {
				reportByKeycode(event: event, newFlags: newFlags, changed: changed)
			}
		}
		previousFlags = newFlags
		updateHotkey(newFlags)
	}

	/// Keyboards that do not set the device dependent bits only tell us which
	/// key changed through the event keycode.
	private func reportByKeycode(event: CGEvent, newFlags: UInt64, changed: UInt64) {
		let keycode = event.getIntegerValueField(.keyboardEventKeycode)
		guard let modifier = modifierKeys.first(where: { $0.keycode == keycode }) else { return }
		guard changed & modifier.groupMask != 0 else { return }
		report(modifier: keycode, isDown: newFlags & modifier.groupMask != 0, flags: newFlags)
	}

	private func report(modifier keycode: Int64, isDown: Bool, flags: UInt64) {
		if isDown {
			heldKeycodes.insert(keycode)
		} else {
			heldKeycodes.remove(keycode)
		}
		emitKey(type: isDown ? "down" : "up", keycode: keycode, flags: flags, isRepeat: false)
	}

	private func updateHotkey(_ newFlags: UInt64) {
		let complete = newFlags & hotkeyMask == hotkeyMask
		if complete {
			hotkeyHeld = true
			return
		}
		if hotkeyHeld {
			hotkeyHeld = false
			setGrabbing(!grabbing)
		}
	}
}

func printUsage(to stream: UnsafeMutablePointer<FILE>) {
	let usage = """
	Usage: \(programName) [options]

	Taps the keyboard and prints one JSON object per line to stdout, so a
	terminal driven VM controller can forward keys the terminal never sees,
	such as Command+D or Command+Tab. While grabbed, keys are consumed and do
	not reach other applications.

	Options:
	  --release-hotkey LIST   Modifiers that toggle the grab, comma separated.
	                          Names: control, option, command, shift, capslock, fn.
	                          Default: \(defaultReleaseHotkey)
	  --start-released        Start with the grab off.
	  -h, --help              Print this help and exit.

	Stdin commands, one per line: "grab on", "grab off", "quit". Stdin EOF,
	SIGINT and SIGTERM all exit cleanly after releasing held keys.

	The tap needs Accessibility permission for the process that creates it.
	Grant it in System Settings > Privacy & Security > Accessibility.
	"""
	fputs(usage + "\n", stream)
}

func parseHotkey(_ list: String) -> UInt64? {
	var mask: UInt64 = 0
	for name in list.split(separator: ",") {
		let key = name.trimmingCharacters(in: .whitespaces).lowercased()
		guard let modifierMask = hotkeyModifierMasks[key] else { return nil }
		mask |= modifierMask
	}
	return mask == 0 ? nil : mask
}

var hotkeyMask = parseHotkey(defaultReleaseHotkey)!
var startGrabbed = true

var arguments = Array(CommandLine.arguments.dropFirst())
while let argument = arguments.first {
	arguments.removeFirst()
	switch argument {
	case "-h", "--help":
		printUsage(to: stdout)
		exit(0)
	case "--start-released":
		startGrabbed = false
	case "--release-hotkey":
		guard let list = arguments.first else {
			writeStderr("\(programName): --release-hotkey needs a comma separated modifier list")
			exit(usageExitCode)
		}
		arguments.removeFirst()
		guard let mask = parseHotkey(list) else {
			writeStderr("\(programName): unknown modifier in --release-hotkey \(list)")
			exit(usageExitCode)
		}
		hotkeyMask = mask
	default:
		writeStderr("\(programName): unknown option \(argument)")
		printUsage(to: stderr)
		exit(usageExitCode)
	}
}

let promptKey = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
AXIsProcessTrustedWithOptions([promptKey: true] as CFDictionary)

let grabber = KeyGrabber(hotkeyMask: hotkeyMask, startGrabbed: startGrabbed)
if !grabber.start() {
	emit("{\"type\":\"error\",\"reason\":\"accessibility-permission\"}")
	writeStderr("""
	\(programName): cannot create the keyboard event tap.

	Grant Accessibility permission in System Settings > Privacy & Security >
	Accessibility to the terminal application you are running this from, and to
	\(CommandLine.arguments[0]) if it appears in the list. Toggle an existing
	entry off and on again after a rebuild, because the permission follows the
	signed binary. Input Monitoring also works if your setup grants that instead.
	""")
	exit(permissionExitCode)
}

func shutDownAndExit() {
	grabber.shutDown()
	exit(0)
}

var signalSources: [DispatchSourceSignal] = []
for signalNumber in [SIGINT, SIGTERM] {
	signal(signalNumber, SIG_IGN)
	let source = DispatchSource.makeSignalSource(signal: signalNumber, queue: .main)
	source.setEventHandler { shutDownAndExit() }
	source.resume()
	signalSources.append(source)
}

let stdinThread = Thread {
	while let line = readLine(strippingNewline: true) {
		let command = line.trimmingCharacters(in: .whitespaces).lowercased()
		switch command {
		case "":
			continue
		case "grab on":
			DispatchQueue.main.async { grabber.setGrabbing(true) }
		case "grab off":
			DispatchQueue.main.async { grabber.setGrabbing(false) }
		case "quit":
			DispatchQueue.main.async { shutDownAndExit() }
		default:
			writeStderr("\(programName): unknown command \(command)")
		}
	}
	DispatchQueue.main.async { shutDownAndExit() }
}
stdinThread.start()

CFRunLoopRun()
