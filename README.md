# wn

`wn` controls a Windows 11 ARM64 VM on macOS from the terminal. It starts and
stops the VM, reports its state and IP address, and sends keystrokes to the
guest through QEMU's QMP socket.

`wn` needs only the Python 3.9 standard library.

## Install

Run `wn` from the repository root:

```sh
python3 -m wn status
```

## Configuration

`wn` reads `~/.config/wn/config.json`. Every setting is optional.

```json
{
	"vm_name": "Windows",
	"backend": "utm",
	"qmp_socket": "~/vm/qmp.sock",
	"key_delay_ms": 20
}
```

| Setting | Default | Meaning |
|---|---|---|
| `vm_name` | `Windows` | Name of the VM in the backend. |
| `backend` | `utm` | Which backend drives the VM. |
| `qmp_socket` | none | Path of the QMP Unix socket. When unset, `wn` asks the backend. |
| `key_delay_ms` | `20` | Pause after each key press for `key`, `type`, and `attach`. |
| `keygrab_binary` | `keygrab/bin/wn-keygrab` | Path of the key grab helper for `grab`. |
| `keygrab_keycodes` | `keygrab/keycodes.json` | Table that maps macOS keycodes to QEMU key names. |
| `keygrab_send_repeats` | `true` | Send auto-repeat key presses to the guest in `grab` mode. |

The UTM and QEMU backends are not written yet, so commands that need a
backend report that it is not available. `key`, `type`, `attach`, and
`grab` work without a backend when `qmp_socket` is set.

## Commands

### Start, stop, and status

```sh
python3 -m wn start
python3 -m wn stop           # ask Windows to shut down
python3 -m wn stop --force   # power off at once
python3 -m wn status         # prints "Windows: running"
python3 -m wn ip             # prints the guest IP address
```

### Press keys

`wn key` presses one or more key combos in order. Join keys in a combo with `+`.

```sh
python3 -m wn key ctrl+alt+del
python3 -m wn key win+r
python3 -m wn key shift+f10 enter
```

Modifiers are `ctrl`, `shift`, `alt`, `altgr`, and `win` (also `meta` or
`cmd`). Other names include letters, digits, `enter`, `space`, `tab`, `esc`,
`backspace`, `del`, `insert`, `home`, `end`, `pgup`, `pgdn`, arrow keys
(`up`, `down`, `left`, `right`), `f1` to `f12`, keypad keys (`kp_0` to
`kp_9`, `kp_add`, `kp_enter`), punctuation (`minus`, `equal`,
`bracket_left`, `semicolon`, `apostrophe`, `grave_accent`, `backslash`,
`comma`, `dot`, `slash`), and media keys (`audiomute`, `volumeup`,
`volumedown`). Any QEMU qcode name also works. If you mistype a name, `wn`
suggests close matches.

### Type text

`wn type` types a string with a US keyboard layout. It supports ASCII
letters, digits, punctuation, space, tab, and newline. Other characters
are rejected before any key is sent.

```sh
python3 -m wn type 'Get-ComputerInfo'
python3 -m wn type --delay-ms 50 'slow typing'
```

### Options for key and type

| Option | Meaning |
|---|---|
| `--dry-run` | Print each QMP command as JSON instead of sending it. |
| `--delay-ms N` | Pause N milliseconds after each key press. |
| `--hold-ms N` | Hold each combo down for N milliseconds. QEMU uses 100 when you omit it. |

For example:

```sh
$ python3 -m wn key ctrl+alt+del --dry-run
{"execute": "send-key", "arguments": {"keys": [{"type": "qcode", "data": "ctrl"}, {"type": "qcode", "data": "alt"}, {"type": "qcode", "data": "delete"}]}}
```

### Attach the terminal

`wn attach` sends what you type in the terminal to the guest. To detach,
press `ctrl+]` twice within one second. A single `ctrl+]` is sent to the
guest after that second.

A terminal sends characters, not key presses, so attach mode cannot deliver
every key:

- The Windows key and combos that use it.
- `ctrl+alt+del` and most `ctrl+alt` and `ctrl+shift` combos.
- Most `alt` combos. They work only if your terminal sends Option as Meta.
- `ctrl` with digits or punctuation, and `shift` with arrows or function keys.
- Characters outside US-ASCII.

A lone `esc` reaches the guest after a short wait, because `esc` also starts
the sequences for arrow and function keys.

Use `wn grab` or `wn key` for any of these, for example `wn key win+r`.
Attach mode is the fallback for when you cannot use `grab`, for example over
SSH.

### Grab the keyboard

`wn grab` sends every key press on the Mac keyboard to the guest as a real
key down and key up. This is the mode that delivers what attach mode cannot:

- The Windows key. Command maps to it, so Command+R in grab mode is `win+r`
  in Windows.
- `ctrl+alt+del` and every other combo.
- Keys held down, for example a held Shift or a held arrow key.

```sh
python3 -m wn grab
python3 -m wn attach --grab   # the same as wn grab
```

While the grab is on, macOS and other apps do not see the keyboard. Press
Control+Option+Command together and release them to give the keyboard back to
macOS. Press them again to grab it again. When the grab turns off, `wn` releases
every key the guest still holds, so no key stays stuck. Press `ctrl+c` while
the grab is off to stop `wn grab`.

| Option | Meaning |
|---|---|
| `--start-released` | Start with the keyboard released. |
| `--release-hotkey MODIFIERS` | Use other modifiers to release and grab, for example `control,shift`. Names are `control`, `option`, `command`, `shift`, `capslock`, and `fn`. |
| `--dry-run` | Print each QMP command as JSON instead of sending it. The keyboard is still grabbed. |

Grab mode runs the `wn-keygrab` helper in `keygrab/`. Build it once with
`keygrab/build.sh`. The helper reads the keyboard through a macOS event tap,
so it needs Accessibility permission. Turn it on in System Settings > Privacy &
Security > Accessibility for your terminal app, and for
`keygrab/bin/wn-keygrab` if it is in the list. After you rebuild the helper,
turn its entry off and on again. See `keygrab/README.md` for details.

Grab mode only works in a local session on the Mac. Over SSH, use `attach`.

### Shared folders and networking

`wn share <host-path>` and `wn net` are placeholders. They report that the
backend does not support them yet.

## Run the tests

```sh
python3 -m unittest discover -s tests -v
```
