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
| `stop` | `python3 -m wn stop --force` | Shuts down Windows. `--force` powers off at once. |
| `status` | `python3 -m wn status` | Prints `Windows: running` or `Windows: stopped`. |
| `ip` | `python3 -m wn ip` | Prints the guest IP address. |
| `key` | `python3 -m wn key ctrl+alt+del` | Presses one or more key combos. |
| `type` | `python3 -m wn type 'Get-ComputerInfo'` | Types ASCII text with a US layout. |
| `mouse` | `python3 -m wn mouse move 640 400 --screen 1280x800` | Moves, clicks, or scrolls. |
| `grab` | `python3 -m wn grab` | Sends every Mac key press to the guest. |
| `init-vm` | `python3 -m wn init-vm --iso ~/Downloads/Win11_ARM64.iso` | Creates a UTM bundle without installing Windows. |
| `update-vm-args` | `python3 -m wn update-vm-args` | Rewrites the QEMU arguments of a stopped VM. |
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

With the firmware battery driver installed in the VM, Windows shows the Mac's
battery in its own battery icon, in Settings, in Device Manager under
Batteries, and to NVDA. The driver is a UEFI driver that adds an ACPI battery
table. The table reads a small data block from a file on the Mac, which QEMU
maps into the guest as an NVDIMM. `wn agent` writes the Mac's battery status
into that file.

This needs three things, each set up once:

1. The signed driver on the VM's EFI system partition, registered with the
   firmware as a driver load option.
2. The certificate of the key that signed it in the VM's Secure Boot allowed
   list (`db`).
3. The NVDIMM on the VM, added with `wn update-vm-args --battery`.

The repository carries the signed driver, `efi/dist/wn-acpi.efi`, and its
certificate, `efi/dist/wn-sb.cer`, so you do not build or sign anything.
If you change the driver, sign it with your own key and enroll that key's
certificate instead. Install `osslsigncode` and create a key pair:

```sh
brew install osslsigncode
mkdir -p efi/keys
openssl req -new -x509 -newkey rsa:2048 -nodes -subj "/CN=wn Secure Boot/" \
	-keyout efi/keys/wn-sb.key -out efi/keys/wn-sb.crt -days 3650
openssl x509 -in efi/keys/wn-sb.crt -outform DER -out efi/keys/wn-sb.cer
(cd efi/wn-acpi && cargo build --release)
efi/sign.sh
```

`efi/sign.sh` writes `efi/dist/wn-acpi.efi` and `efi/dist/wn-sb.cer` and
checks the signature. Git ignores `efi/keys/*.key`. Keep the private key safe:
anything signed with it runs in your VM with Secure Boot on.

**Install the driver.** Setup does this. A first-logon command runs
`efi\install-wn-acpi.ps1` from the setup disc. It copies the driver and the
certificate to `\EFI\wn` on the EFI system partition and writes the
`Driver####` and `DriverOrder` firmware variables. It logs to
`C:\wn-acpi-install.log`.

For an existing VM, copy the script, the driver, and the certificate into one
folder in the VM, then run the script as administrator. With the VM running:

```sh
U=/Applications/UTM.app/Contents/MacOS/utmctl
$U exec Windows --cmd cmd /c mkdir 'C:\wn-acpi'
$U file push Windows 'C:\wn-acpi\install-wn-acpi.ps1' < efi/guest/install-wn-acpi.ps1
$U file push Windows 'C:\wn-acpi\wn-acpi.efi' < efi/dist/wn-acpi.efi
$U file push Windows 'C:\wn-acpi\wn-sb.cer' < efi/dist/wn-sb.cer
$U exec Windows --cmd powershell -ExecutionPolicy Bypass -File 'C:\wn-acpi\install-wn-acpi.ps1'
```

`utmctl exec` runs the script as SYSTEM, which works too. You can run the
script again at any time. It replaces an existing `wn-acpi` entry and does not
add a second one.

**Enroll the certificate.** Setup does this. For an existing VM, enroll it
from the firmware menu. Start the VM and press Esc until the menu opens
(`python3 -m wn key esc` works too). Go to Device Manager > Secure Boot
Configuration:

1. Set Secure Boot Mode to Custom Mode.
2. Open Custom Secure Boot Options > DB Options > Enroll Signature >
   Enroll Signature Using File.
3. Pick the EFI system partition, then `EFI` > `wn` > `wn-sb.cer`. In the
   file list, a blank line sits above the folders, so press Down once more
   than you expect.
4. Choose Commit Changes and Exit. The Signature GUID field can stay empty.
5. Press Esc back to the front page and choose Reset.

Leave PK and KEK alone, and leave Secure Boot on. Secure Boot Mode shows
Standard Mode again when you come back. The new `db` entry stays. DB Options >
Delete Signature lists it, with an owner GUID of all zeros, under the
Microsoft entries.

**Add the NVDIMM.** Stop the VM first, then:

```sh
python3 -m wn update-vm-args --battery --audio-args "<your audio arguments>"
```

`update-vm-args` rebuilds the whole AdditionalArguments list, so pass your
`--audio-args` again or they are lost. It creates the battery file
`~/Library/Group Containers/WDNLXAD4W8.com.utmapp.UTM/wn-battery-<vm_name>.bin`
with a full battery on AC power. UTM's sandbox lets QEMU open files only in
that folder, so QEMU gets the bare file name. Quit UTM and open it again so
it reads the new arguments, then start the VM. Run the command again after
you change the VM's memory size in UTM.

`wn agent start` checks the battery every 5 seconds. It writes the file at
once when the status changes, and every 30 seconds otherwise. Windows sees
each write within about a second. If the file is missing, the agent logs it
and tries again. Without the launch agent below, `wn agent start` runs in the
terminal until you press Ctrl+C. `wn agent --once` prints the battery status
as JSON and writes the file once.

To start the agent when you log in, run this on the Mac. It adds a launch
agent that launchd keeps running and logs to `~/Library/Logs/wn-agent.log`:

```sh
python3 -m wn agent --install
```

To turn autostart off, run `python3 -m wn agent --uninstall`.

Control the agent with these commands:

```sh
python3 -m wn agent status
python3 -m wn agent stop
python3 -m wn agent start
```

`status` shows whether the launch agent is installed, whether the agent runs
and its PID, the QMP address and whether the VM answers there, and when the
battery file was last written. It exits with 1 when the agent is not running.
`stop` unloads the launch agent until you log in again or run `start`. If the
agent runs in a terminal, `stop` sends it SIGTERM.

Use `wn battery` to check the block by hand:

```sh
python3 -m wn battery status
python3 -m wn battery sync
python3 -m wn battery set --percent 20 --discharging
```

`status` prints the block as the guest sees it. `sync` writes the Mac's
battery status once. `set` writes a fake status once, so you can watch
Windows react. It takes `--charging`, `--discharging` (the default), or
`--ac`. A running `wn agent` writes the real status again within 30 seconds.

Windows also finds QEMU's NVDIMM bus. Device Manager shows "Storage Class
Memory Bus" under System devices with code 10 (cannot start). It does not
affect the battery, and no new disk appears in Disk Management.

To remove the battery, stop the VM and run `wn update-vm-args` without
`--battery` (with your usual `--audio-args`).

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
display window. Pass `--headless` to `update-vm-args` too for such a VM.

**QEMU arguments.** `init-vm` and `update-vm-args` take these options:

- `--audio-args ARGS` replaces the default CoreAudio block (`-audiodev`,
  `intel-hda`, and `hda-output`), for example to change the buffer sizes.
- `--extra-args ARGS` adds QEMU arguments after everything else, for example
  `-device virtio-keyboard-pci,id=vkbd`.
- `--machine-properties PROPS` (`init-vm` only) sets the QEMU machine
  properties. The default `kernel-irqchip=off` avoids the UTM 5.0.8 reboot
  hang with 2 or more vCPUs. An empty string keeps UTM's defaults.

After `update-vm-args`, quit UTM and open it again so it reads the new
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
