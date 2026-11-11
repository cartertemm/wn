# wn-keygrab

`wn-keygrab` is a small macOS helper that reads the keyboard through a
CoreGraphics event tap and prints one JSON object per line to stdout. A
terminal driven VM controller runs it so it can forward keys that a terminal
never receives, such as Command+D, Command+R, and Command+Tab.

While the grab is on, the tap is an active filter: it consumes keyDown, keyUp,
and flagsChanged events, so they do not reach the frontmost application. While
the grab is off, events pass through untouched and nothing is printed.

## Build

```sh
./build.sh
```

The script compiles `main.swift` with `swiftc -O` into `bin/wn-keygrab` and
signs it ad-hoc with `codesign -s - --force`. The signature matters: macOS ties
the Accessibility permission to the signed binary identity, so an unsigned or
re-signed binary can lose a permission that was already granted.

## Permission

An event tap only works if the process that creates it has Accessibility (or
Input Monitoring) permission. On the first run, the helper calls
`AXIsProcessTrustedWithOptions` with the prompt option, which makes macOS show
the permission dialog once.

Grant the permission in System Settings > Privacy & Security > Accessibility,
for the terminal application you run the helper from, and for
`bin/wn-keygrab` itself if it appears in the list. After a rebuild, toggle the
existing entry off and on again, because the permission follows the signed
binary.

If the tap cannot be created, the helper prints

```json
{"type":"error","reason":"accessibility-permission"}
```

on stdout, prints an explanation on stderr, and exits with code 2.

## Release hotkey

Press Control+Option+Command together and then release them to toggle the grab
off. Press and release the same combination again to toggle it back on. The
toggle fires on release, so the combination itself is never delivered twice.

Change the combination with `--release-hotkey`, which takes a comma separated
list of modifier names: `control`, `option`, `command`, `shift`, `capslock`,
`fn` (`ctrl`, `alt`, `cmd`, and `function` are accepted as aliases).

```sh
bin/wn-keygrab --release-hotkey control,shift
```

The helper starts grabbed. Pass `--start-released` to start with the grab off.

## Commands and exit

The helper reads commands from stdin, one per line:

* `grab on` turns the grab on.
* `grab off` turns the grab off.
* `quit` exits.

Stdin EOF, SIGINT, and SIGTERM also exit. On exit, and whenever the grab turns
off, the helper prints a synthetic `up` event for every key it still believes is
held, then disables the tap. A consumer that replays those events leaves no key
stuck in the guest.

Exit codes: 0 on a clean exit, 2 when the tap cannot be created, 64 for a usage
error.

## JSON protocol

Every line is one complete JSON object. Timestamps are monotonic seconds from
`ProcessInfo.systemUptime`, useful for intervals, not as wall clock time.

| Line | Meaning |
| --- | --- |
| `{"type":"down","keycode":0,"flags":256,"ts":12345.678901}` | Key pressed. `keycode` is the macOS virtual keycode, `flags` is the raw `CGEventFlags` value as an integer. |
| `{"type":"down","repeat":true,"keycode":0,"flags":256,"ts":...}` | Autorepeat. The key is still held and this is a real repeat from the keyboard, so forward it like any other press. |
| `{"type":"up","keycode":0,"flags":0,"ts":...}` | Key released. |
| `{"type":"grab","active":true}` | The grab turned on, or is on at startup. |
| `{"type":"grab","active":false}` | The grab turned off. |
| `{"type":"tap","state":"re-enabled","reason":"timeout"}` | The system disabled the tap and the helper turned it back on. See below. |
| `{"type":"error","reason":"accessibility-permission"}` | The tap could not be created. The helper exits with code 2 right after. |

Modifier keys do not produce keyDown and keyUp events on macOS. They produce
flagsChanged events instead, so the helper compares the previous and the new
flags and reports the key that changed as a `down` or an `up` with the keycode
of that modifier. Left and right modifiers are distinguished: shift 56 and 60,
control 59 and 62, option 58 and 61, command 55 and 54, plus caps lock 57 and fn
63.

Fields may be added later. Ignore any field or `type` value you do not know.

## Handling a tap disabled by timeout

macOS disables an event tap whose callback is too slow, and it does the same
after some kinds of user input. The tap stops delivering events until something
re-enables it. The helper handles this itself: on
`kCGEventTapDisabledByTimeout` or `kCGEventTapDisabledByUserInput` it calls
`CGEvent.tapEnable` again and prints the `tap` line above.

A consumer should treat that line as a warning that keys were lost:

1. Assume the key state in the guest is now wrong. Send an "all keys up" to the
   guest, the same way you would after `{"type":"grab","active":false}`.
2. Do not try to reconstruct the missing events. There is no record of them.
3. If the line repeats often, the consumer is the slow part. Read stdout in a
   dedicated reader that never blocks, buffer the lines, and do the work that
   talks to the VM somewhere else. The tap callback runs inside this helper, but
   a consumer that stops reading stdout fills the pipe and stalls the helper's
   writes, which is what trips the timeout.

## keycodes.json

`keycodes.json` maps macOS virtual keycodes to QEMU qcode names, for a consumer
that drives QEMU through `input-send-event` or a similar interface:

```json
{"keycodes": {"0": "a", "1": "s"}, "_unmapped": [63]}
```

Keys are decimal keycodes as strings, in the ANSI US layout, including the
keypad, F1 to F20, arrows, modifiers, the ISO section key (10), and the JIS keys
(93, 94, 95, 102, 104), and the PC Application key (110). Values are names from `QKeyCodeEnum` in QEMU's
`qapi/ui.json`.

Two mappings are a judgement call. Keycode 114 is the Apple Help key, which sits
where Insert sits on a PC keyboard, so it maps to `insert`. The JIS keys 102
(Eisu) and 104 (Kana) map to `muhenkan` and `katakanahiragana`; QEMU also has
`lang2` and `lang1`, which match what the Apple keyboard reports over HID, so
change these if your guest expects that instead.

Keycode 110 has no `kVK_` constant in Carbon's `Events.h`, but macOS reports it
for the Application key on PC keyboards. It maps to `menu`, which Windows uses
to open context menus.

`_unmapped` lists keycodes that exist on a Mac keyboard but have no QEMU
equivalent. Only the fn key (63) is in it.

Check the table against QEMU:

```sh
python3 -I check_keycodes.py
```

The script downloads `qapi/ui.json` from the QEMU repository, parses
`QKeyCodeEnum`, and fails if any name in the table is not in the enum, if a
keycode is both mapped and unmapped, or if two keycodes claim the same qcode. It
uses only the standard library. Pass a path to a local copy of `ui.json` as the
first argument to run it offline.
