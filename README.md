# wn

`wn` controls a Windows 11 ARM64 VM on macOS from the terminal. It starts and
stops the VM, reports its state and IP address, and sends keystrokes and
mouse input to the guest through QEMU's QMP socket.

`wn` needs only the Python 3.9 standard library.

## Quick start

1. Install [UTM](https://mac.getutm.app) in `/Applications`.
2. From the repository root, run:

   ```sh
   python3 -m wn setup
   ```

3. Answer the questions. Press Return to keep the default shown in brackets.
4. Wait. `wn` downloads Windows 11 ARM64 and the drivers, builds the VM, and
   installs Windows with no further input. It prints a progress line with
   the elapsed time while it works. The whole install takes about 25 minutes
   after the downloads.

When it finishes, the VM is running at the Windows desktop and `wn` prints
its IP address. Use the VM in its UTM window. In that window, Command acts
as the Windows key. To drive it from the terminal, see [Commands](#commands).

## Set up a VM

`wn setup` asks these questions. Each flag sets the answer, and `--yes`
skips the questions and uses the flags and defaults.

| Question | Flag | Default |
|---|---|---|
| VM name | `--name NAME` | `Windows` |
| Windows user name | `--username NAME` | your Mac user name |
| Windows password | `--password TEXT` | none |
| CPU cores | `--cpus N` | `8` |
| Memory in MiB | `--memory-mib N` | `8192` |
| Disk size in GiB | `--disk-gib N` | `64` |
| Run without a display window | `--headless` | no |
| Software to install (1 is NVDA) | `--software nvda` or `--software none` | NVDA |
| Windows 11 edition | `--edition NAME` | `Pro` |

Other flags: `--downloads-dir DIR` (default
`~/Library/Application Support/wn/downloads`) and `--output DIR` (default UTM's
documents folder).

For example:

```sh
python3 -m wn setup --yes --name Windows --username carter --software nvda
```

`wn setup` does these steps:

1. Downloads and checks the Windows 11 ARM64 ISO from Microsoft, the
   virtio-win ARM64 drivers, the UTM guest tools, and NVDA. Each file is
   checked against its published SHA256 hash. Files that are already there
   and verified are not downloaded again.
2. Builds a setup disc with `autounattend.xml`, the drivers, and the
   installers, and creates the UTM bundle with the Windows ISO and that disc.
3. Restarts UTM so that it finds the new VM, and starts the VM. UTM must
   not be running other VMs.
4. Waits while Windows Setup installs Windows. At the first sign-in, the
   answer file installs the virtio drivers, the UTM guest tools, and NVDA,
   then restarts Windows once.
5. Shuts the VM down, removes the install discs, switches the display to
   `virtio-gpu-pci`, and starts the VM again. If the screen stays blank, it
   goes back to `virtio-ramfb`.
6. Writes `~/.config/wn/config.json` if it does not exist.

Windows is installed with Microsoft's generic key for the edition. It is not
activated. To activate it, enter your own product key in Settings.

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
| `utmctl_path` | `/Applications/UTM.app/Contents/MacOS/utmctl` | Path of UTM's `utmctl` tool. |
| `qmp_port` | `4444` | TCP port of the VM's QMP server on `127.0.0.1` for one-shot commands such as `key`, `type`, and `mouse`. |
| `qmp_session_port` | `qmp_port` + 1 | TCP port of the second QMP server, used by `attach` and `grab`. |
| `qmp_socket` | none | QMP address: a Unix socket path or `tcp:HOST:PORT`. When unset, `wn` asks the backend. When set, every command uses it. |
| `key_delay_ms` | `20` | Pause after each key is released for `key`, `type`, and `attach`. |
| `screen_size` | none | Guest screen size such as `1280x800`. When set, `wn mouse move` takes pixel positions. |
| `keygrab_binary` | `keygrab/bin/wn-keygrab` | Path of the key grab helper for `grab`. |
| `keygrab_keycodes` | `keygrab/keycodes.json` | Table that maps macOS keycodes to QEMU key names. |
| `keygrab_send_repeats` | `true` | Send auto-repeat key presses to the guest in `grab` mode. |

`utm` is the only backend so far. `key`, `type`, `mouse`, `attach`, and `grab` work
with any QEMU VM when `qmp_socket` is set.

### Configure for UTM

`wn` drives UTM 5 through `utmctl` and sends keys through a QMP server that
QEMU opens on a local TCP port. A typical `~/.config/wn/config.json`:

```json
{
	"backend": "utm",
	"vm_name": "Windows",
	"qmp_port": 4444
}
```

`vm_name` must match the VM's name in UTM. The VM's QEMU additional
arguments must include two QMP servers:

```
-qmp tcp:127.0.0.1:4444,server=on,wait=off
-qmp tcp:127.0.0.1:4445,server=on,wait=off
```

Use `qmp_port` for the first and `qmp_session_port` for the second. Each QMP
server serves one client at a time, so `attach` and `grab` use the second
server, and `key`, `type`, and `mouse` still work while they run.
`wn init-vm` adds both arguments for you. For a VM made by an older `wn`, see
[Update the QEMU arguments of a VM](#update-the-qemu-arguments-of-a-vm).

## Commands

### Start, stop, and status

```sh
python3 -m wn start
python3 -m wn stop           # ask Windows to shut down
python3 -m wn stop --force   # power off at once
python3 -m wn status         # prints "Windows: running"
python3 -m wn ip             # prints the guest IP address
```

### Create a UTM VM

`wn init-vm` writes a UTM bundle for a Windows 11 ARM64 VM. It creates an
empty qcow2 disk with `qemu-img`, copies the installer ISO into the bundle,
and writes `config.plist`.

```sh
python3 -m wn init-vm --iso ~/Downloads/Win11_ARM64.iso
```

The bundle goes in `~/Library/Containers/com.utmapp.UTM/Data/Documents`,
named after `vm_name`. `wn` does not overwrite an existing bundle. Open the
new bundle once in UTM, or run `open` on the path that `wn` prints, so UTM
adds it to its list.

| Option | Default | Meaning |
|---|---|---|
| `--name NAME` | `vm_name` | VM name in UTM. |
| `--disk-gib N` | `64` | Disk size in GiB. |
| `--cpus N` | `8` | CPU cores. |
| `--memory-mib N` | `8192` | Memory in MiB. |
| `--iso PATH` | none | Installer ISO to copy into the bundle. |
| `--driver-iso PATH` | none | Driver ISO, such as virtio-win, to copy into the bundle as a second CD. |
| `--machine-properties PROPS` | `kernel-irqchip=off` | QEMU machine properties (UTM's `MachinePropertyOverride`). The default avoids the UTM 5.0.8 reboot hang with 2 or more vCPUs (UTM issue 8009). An empty string keeps UTM's defaults. |
| `--headless` | off | Create the VM without a display window. |
| `--audio MODE` | `coreaudio` | `coreaudio` plays straight to macOS with low latency. `spice` uses UTM's audio path. |
| `--audio-args ARGS` | none | QEMU arguments that replace the default coreaudio block (`-audiodev`, `intel-hda`, and `hda-output`). Use it to tune buffers or try another sound device. Needs `--audio coreaudio`. |
| `--extra-args ARGS` | none | More QEMU arguments, added after everything else. For example `-device virtio-keyboard-pci,id=vkbd`. |
| `--mac ADDRESS` | random `52:54:00:xx:xx:xx` | MAC address of the network card. |
| `--output DIR` | UTM's documents folder | Where to write the bundle. |
| `--qemu-img PATH` | `/opt/homebrew/bin/qemu-img` | The `qemu-img` tool. Install it with `brew install qemu`. |

### Update the QEMU arguments of a VM

`wn update-vm-args` rewrites the QEMU additional arguments of an existing UTM
bundle, for example to add the second QMP server to a VM made by an older
`wn`. It changes nothing else in `config.plist`, and it refuses to run while
the VM is not stopped.

```sh
python3 -m wn stop
python3 -m wn status          # wait for "Windows: stopped"
python3 -m wn update-vm-args
```

Then quit UTM and open it again so it reads the new arguments, and run
`wn start`.

| Option | Default | Meaning |
|---|---|---|
| `--bundle PATH` | `vm_name.utm` in UTM's documents folder | The bundle to update. |
| `--headless` | off | Give this if the VM was created with `--headless`. |
| `--audio MODE` | `coreaudio` | Give the audio mode the VM was created with. |
| `--audio-args ARGS` | none | QEMU arguments that replace the default coreaudio block. See `init-vm`. |
| `--extra-args ARGS` | none | More QEMU arguments, added at the end. See `init-vm`. |

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
| `--delay-ms N` | Pause N milliseconds after each combo is released. |
| `--hold-ms N` | Hold each combo down for N milliseconds before it is released. The default is 50. |

`wn` sends each combo as one QMP `input-send-event` command that presses its
keys and a second one that releases them, last key first. A repeated key,
such as `wn key down down` or the two l's in `wn type hello`, reaches the guest
as separate presses. For example:

```sh
$ python3 -m wn key ctrl+c --dry-run
{"execute": "input-send-event", "arguments": {"events": [{"type": "key", "data": {"down": true, "key": {"type": "qcode", "data": "ctrl"}}}, {"type": "key", "data": {"down": true, "key": {"type": "qcode", "data": "c"}}}]}}
{"execute": "input-send-event", "arguments": {"events": [{"type": "key", "data": {"down": false, "key": {"type": "qcode", "data": "c"}}}, {"type": "key", "data": {"down": false, "key": {"type": "qcode", "data": "ctrl"}}}]}}
```

### Use the mouse

`wn mouse` moves the guest's pointer, clicks, and scrolls through QEMU's USB
tablet.

```sh
python3 -m wn mouse move 16383 16383               # the middle of the screen
python3 -m wn mouse move 640 400 --screen 1280x800 # the same point, in pixels
python3 -m wn mouse click                          # left click
python3 -m wn mouse click right
python3 -m wn mouse doubleclick
python3 -m wn mouse down                           # press and keep the left button down
python3 -m wn mouse up
python3 -m wn mouse scroll down 3                  # three wheel notches
```

| Action | Meaning |
|---|---|
| `move X Y` | Move the pointer. X and Y go from 0 to 32767 across the screen. With `--screen WxH`, or `screen_size` in the config, they are pixels. |
| `click [BUTTON]` | Press and release a button. |
| `doubleclick [BUTTON]` | Click twice. |
| `down [BUTTON]` | Press a button and keep it down, for example to drag. |
| `up [BUTTON]` | Release a button. |
| `scroll up\|down [N]` | Turn the wheel N notches. The default is 1. |

`BUTTON` is `left`, `right`, or `middle`. The default is `left`. Every action
takes `--dry-run`, which prints the QMP commands as JSON instead of sending
them. Clicks happen where the pointer is, so move it first.

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
`attach` and `grab` connect to the QMP server on `qmp_session_port`, so
`wn key` works in another terminal while they run.
Attach mode is the fallback for when you cannot use `grab`, for example over
SSH.

### Shared folders and networking

`wn share <host-path>` and `wn net` are placeholders. They report that the
backend does not support them yet.

## Advanced

### Grab the keyboard

The UTM window already maps Command to the Windows key, and it is the
normal way to type in the VM. Use `wn grab` when you want the terminal to
send the keys instead.

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

## Run the tests

```sh
python3 -m unittest discover -s tests -v
```
