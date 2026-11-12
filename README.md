# wn

Windows 11 on Apple Silicon at near native speed, driven from the terminal.

## Why

`wn` builds a Windows 11 ARM64 VM in UTM, which runs on Apple's
Hypervisor.framework, the same hypervisor Parallels uses. Published Geekbench 6
results on an M4 Mac mini put UTM at 3144 single core and 8788 multi core,
and Parallels at 3141 and 7958. `wn setup` picks the settings that booted and
stayed stable in tests on an M4: the in-kernel interrupt controller is off
(it causes a reboot hang in UTM), the disk and network use virtio drivers,
the display uses virtio-gpu, and audio goes straight to CoreAudio with a
15 ms queue tuned for a screen reader. After the first boot, the VM reaches
the network about 10 to 12 seconds after `wn start`. The install runs with no
input and took under 11 minutes on an M4 Pro.

What it does not do. There is no 3D acceleration, so Windows renders on the
CPU. Microsoft has not authorized this setup (Parallels is the only
authorized VM for Windows 11 Pro on a Mac). Windows installs with Microsoft's
generic key and is not activated until you enter your own key in Settings.

## Requirements

- A Mac with Apple Silicon.
- macOS 15 or later. macOS 26 is recommended.
- UTM 5.0.8 or later, from https://mac.getutm.app or GitHub, in `/Applications`.
- Python 3.9, which comes with the Xcode Command Line Tools.
- About 20 GB of free disk space.
- An internet connection. The Windows ISO is 8.5 GB from Microsoft.

## Set up

```sh
git clone <repository URL> windows-native
cd windows-native
python3 -m wn setup
```

`wn setup` asks these questions. Press Return to keep the default.

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

Pass `--no-battery` to leave out the [native battery](#native-battery).

Then it works on its own:

1. Downloads the Windows 11 ARM64 ISO, the virtio-win drivers, the UTM guest
   tools, and NVDA, and checks each one against its SHA256 hash. Files that
   are already there and verified are not downloaded again.
2. Builds a setup disc with the answer file, the drivers, and the installers,
   and creates the UTM bundle.
3. Restarts UTM so it finds the new VM, then starts the VM.
4. Waits while Windows installs. At the first sign in, Windows installs the
   drivers, the guest tools, and NVDA if you chose it, then restarts once.
5. Shuts down the VM, removes the install discs, switches the display to
   virtio-gpu, and starts it again.
6. Writes `~/.config/wn/config.json` if it does not exist.
7. Installs and starts `wn agent`, which feeds the
   [native battery](#native-battery). macOS asks whether "wn-agent" may
   access data from other apps. Click Allow.

After the downloads, this takes about 11 minutes. A UTM window opens while
Windows installs. Leave it alone until `wn` prints `Done` and the IP address.

To run setup from a script, pass `--yes` and the flags you need:

```sh
python3 -m wn setup --yes --name Windows --username carter --software nvda
```

## Use it

| Command | Example | What it does |
|---|---|---|
| `start` | `python3 -m wn start` | Starts the VM. |
| `stop` | `python3 -m wn stop` | Shuts down Windows and waits until the VM has stopped, up to `--timeout` seconds (120). Exits with 1 if it is still running then. `--force` powers off at once. |
| `status` | `python3 -m wn status` | Prints `Windows: running` or `Windows: stopped`. |
| `ip` | `python3 -m wn ip` | Prints the guest IP address. |
| `key` | `python3 -m wn key ctrl+alt+del` | Presses one or more key combos. |
| `type` | `python3 -m wn type 'Get-ComputerInfo'` | Types ASCII text with a US layout. |
| `mouse` | `python3 -m wn mouse move 640 400 --screen 1280x800` | Moves, clicks, or scrolls. |
| `grab` | `python3 -m wn grab` | Sends every Mac key press to the guest. |
| `init-vm` | `python3 -m wn init-vm --iso ~/Downloads/Win11_ARM64.iso` | Creates a UTM bundle without installing Windows. |
| `update-vm-args` | `python3 -m wn update-vm-args --battery` | Changes parts of the QEMU arguments of a stopped VM and prints the change. |
| `agent` | `python3 -m wn agent start` | Writes the Mac's battery status to the native battery in the guest. Takes `start`, `stop`, `status`, `--install`, `--uninstall`, or `--once`. See [Native battery](#native-battery). |
| `battery` | `python3 -m wn battery status` | Reads or writes the native battery block in guest memory. See [Native battery](#native-battery). |

`key`, `type`, and `mouse` take `--dry-run` to print the QMP commands as JSON
instead of sending them. Run `python3 -m wn <command> --help` for every option.

For everyday typing, use the UTM window. It already maps Command to the
Windows key, and Ctrl+Option captures and releases the keyboard. The CLI is
for scripts.

## Screen reader

If you chose NVDA, setup installs it and sets it to start after you sign in.
Audio is tuned for low latency. On an M4 Pro, the time from a key press to
NVDA speech was about 115 ms. Most of that time is spent inside the Windows
audio stack.

## Native battery

Windows shows the Mac's battery in its own battery icon, in Settings, in
Device Manager under Batteries, and to NVDA. `wn setup` sets this up, so it
works on the first desktop with Secure Boot on:

- The VM's Secure Boot variables come with the certificate
  `efi/dist/wn-sb.cer` in the allowed list (`db`).
- At the first sign in, Windows copies the signed firmware driver
  `efi/dist/wn-acpi.efi` to the EFI system partition and registers it as a
  driver load option. The log is `C:\wn-acpi-install.log`.
- QEMU maps the battery file
  `~/Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM/wn-battery-<name>.bin`
  into the guest as an NVDIMM. The driver adds an ACPI battery that reads it.
- Setup installs `wn agent` as a launch agent. It writes the Mac's battery
  status into that file and starts at every login. Setup prints the plist
  path. To remove it, run `python3 -m wn agent --uninstall`.

To make a VM without the battery, pass `--no-battery` to `wn setup`.

The battery file is in UTM's app data folder, which macOS protects. So the
launch agent runs Python through a small app,
`~/Library/Application Support/wn/wn-agent.app`, and macOS asks you about that
app by name: "wn-agent" would like to access data from other apps. Click
Allow. If the repository is in your Documents folder, macOS also asks for
access to Documents once.

macOS can ask again when the agent starts, for example after you log in. To
stop the question, add `wn-agent.app` to System Settings > Privacy & Security
> Full Disk Access. Until you allow access, `wn-agent.log` shows "Operation not
permitted" and Windows keeps the last status written. The `wn battery`
commands work from Terminal.

For an existing VM that has the driver and the certificate, stop the VM, then
add the NVDIMM and install the agent:

```sh
python3 -m wn update-vm-args --battery
python3 -m wn agent --install
```

Quit UTM and open it again, then start the VM. VMs made before setup enrolled
the certificate do not have it, so the firmware refuses the driver. Make a new
VM with `wn setup` for those.

Check and control the agent:

```sh
python3 -m wn agent status
python3 -m wn agent stop
python3 -m wn agent start
```

The agent checks the battery every 5 seconds and writes the file when the
status changes, and every 30 seconds otherwise. Windows sees each write within
about a second. To test, write a fake status once, then the real one:

```sh
python3 -m wn battery set --percent 20 --discharging
python3 -m wn battery sync
```

`wn battery status` prints the block as the guest sees it.

Windows also finds QEMU's NVDIMM bus. Device Manager shows "Storage Class
Memory Bus" under System devices with code 10. It does not affect the battery.

To remove the battery from a VM, stop it and run
`python3 -m wn update-vm-args --no-battery`.

If you change the driver, sign it with your own key. Install `osslsigncode`,
create a key pair, build, and sign:

```sh
brew install osslsigncode
mkdir -p efi/keys
openssl req -new -x509 -newkey rsa:2048 -nodes -subj "/CN=wn Secure Boot/" \
	-keyout efi/keys/wn-sb.key -out efi/keys/wn-sb.crt -days 3650
openssl x509 -in efi/keys/wn-sb.crt -outform DER -out efi/keys/wn-sb.cer
(cd efi/wn-acpi && cargo build --release)
efi/sign.sh
```

`efi/sign.sh` writes `efi/dist/wn-acpi.efi` and `efi/dist/wn-sb.cer`, and the
next `wn setup` enrolls that certificate. Git ignores `efi/keys/*.key`. Keep
the private key safe: anything signed with it runs in your VM with Secure Boot
on.

## Configuration

`wn` reads `~/.config/wn/config.json`. Every setting is optional.

| Setting | Default | Meaning |
|---|---|---|
| `vm_name` | `Windows` | Name of the VM in UTM. |
| `backend` | `utm` | Which backend drives the VM. `utm` is the only one so far. |
| `utmctl_path` | `/Applications/UTM.app/Contents/MacOS/utmctl` | Path of UTM's `utmctl` tool. |
| `qmp_port` | `4444` | QMP port on `127.0.0.1` for `key`, `type`, and `mouse`. |
| `qmp_session_port` | `qmp_port` + 1 | Second QMP port, used by `grab`. |
| `qmp_socket` | none | QMP address: a Unix socket path or `tcp:HOST:PORT`. When set, every command uses it. |
| `key_delay_ms` | `20` | Pause after each key is released for `key` and `type`. |
| `screen_size` | none | Guest screen size such as `1280x800`. When set, `wn mouse move` takes pixels. |
| `keygrab_binary` | `keygrab/bin/wn-keygrab` | Path of the key grab helper. |
| `keygrab_keycodes` | `keygrab/keycodes.json` | Table that maps macOS keycodes to QEMU key names. |
| `keygrab_send_repeats` | `true` | Send auto-repeat key presses in `grab` mode. |
| `battery_sync` | `true` | Write the Mac's battery status to the native battery block from `wn agent`. |
| `battery_channel` | `file` | How the agent writes the battery block: `file` (the NVDIMM file) or `gdb` (QEMU's GDB stub, older). |
| `battery_file` | `~/Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM/wn-battery-<vm_name>.bin` | Battery file that QEMU maps into the guest. UTM's QEMU can open it only in that folder. |
| `gdb_port` | `4446` | Port of the VM's GDB stub on `127.0.0.1`, for the `gdb` battery channel. |

## Advanced

**Key grab mode.** `wn grab` is for when you have no UTM window, for example
a headless VM or a terminal session. It sends real key down and key up events,
so the Windows key, `ctrl+alt+del`, and held keys all work. Press
Control+Option+Command to release the keyboard to macOS, and again to grab
it. Change this with `--release-hotkey`, for example `control,shift`. Build
the helper once with `keygrab/build.sh`, then give your terminal app
Accessibility permission in System Settings > Privacy & Security >
Accessibility. See `keygrab/README.md` for details.

**Headless mode.** `--headless` on `setup` or `init-vm` creates the VM with no
display window.

**QEMU arguments.** `init-vm` and `update-vm-args` take these options:

- `--audio-args ARGS` replaces the default CoreAudio block (`-audiodev`,
  `intel-hda`, and `hda-output`), for example to change the buffer sizes.
- `--extra-args ARGS` adds QEMU arguments after everything else, for example
  `-device virtio-keyboard-pci,id=vkbd`.
- `--machine-properties PROPS` (`init-vm` only) sets the QEMU machine
  properties. The default `kernel-irqchip=off` avoids the UTM 5.0.8 reboot
  hang with 2 or more vCPUs. An empty string keeps UTM's defaults.

`update-vm-args` reads the bundle's current arguments and changes only the
parts that its flags name. Everything else stays as it is:

| Flag | Part it replaces |
|---|---|
| `--qmp` | The QMP servers, from `qmp_port` and `qmp_session_port` |
| `--headless` | Adds `-device ramfb` |
| `--audio spice`, `--audio coreaudio`, `--audio-args ARGS` | The audio arguments |
| `--battery`, `--no-battery` | The battery NVDIMM |
| `--extra-args ARGS` | Every argument that is not in one of the parts above |

It prints the old and new arguments as a diff. With no flags, nothing
changes. Afterwards, quit UTM and open it again so it reads the new
arguments.

`wn setup` restarts UTM so it finds the new VM. Stop other VMs in UTM before
you run it.

## Development

Run the tests:

```sh
python3 -m unittest discover -s tests
```

- `wn/`: the CLI, setup, answer file, UTM config, and QMP code.
- `tests/`: unit tests with fake QMP, `utmctl`, and key grab helpers.
- `keygrab/`: the Swift key grab helper and its keycode table.
- `latency/`: tools that measure input and audio latency.

## License

License: to be decided.
