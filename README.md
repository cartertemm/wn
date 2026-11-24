# wn

Windows 11 on your Apple silicon Mac, fast enough to forget it is a virtual
machine. Guaranteed.

Parallels works well, but it costs money every year. UTM is free, but its default Windows
experience is slow and leaves audio, the camera, the microphone, and other Mac
hardware unfinished. Getting everything working takes more tinkering than it
should.

`wn` handles that work for you by createing a fast Windows 11 ARM virtual
machine in UTM with working audio, microphone, camera, networking, and battery
status. Setup is unattended, and the finished VM can be controlled from the
terminal.

## What you get

**Near native speed.** `wn` uses Apple's hardware virtualization with a UTM
configuration tested on Apple silicon. Published Geekbench 6 results on an M4
Mac mini put UTM at 3144 single core and 8788 multi core, compared with 3141
and 7958 in Parallels.

**Mac hardware in Windows.** Speakers, microphone, camera, keyboard, mouse,
networking, and battery status work in the guest. Audio is tuned for low
latency, including screen reader use.

**A simple unattended install.** Run setup, answer a few questions, and wait
for your Windows desktop. `wn` downloads Windows, builds the VM, installs the
drivers and guest tools, and finishes the configuration. The installation takes
about ten minutes on an M4 Pro after the downloads are complete.

**Ready for coding agents.** Start Codex or Claude Code in the cloned
repository. It can discover the CLI and control the VM without an integration
or additional instructions.

## Quick start

You need an Apple silicon Mac with macOS 15 or later, about 20 GB of free
space, [UTM 5.0.8 or later](https://mac.getutm.app) in `/Applications`, and
[uv](https://docs.astral.sh/uv/getting-started/installation/).

```sh
git clone https://github.com/cartertemm/wn
cd wn
uv run wn setup
```

Press Return to keep each default. Setup will tell you when the VM is ready.

## Unattended setup

The interactive setup lets you choose the Windows user and password, VM name,
CPU and memory, disk size, Windows edition, whether to run without a display
window, and whether to install NVDA. The defaults create a Windows 11 Pro VM
with 8 CPU cores, 8 GB of memory, a 64 GB disk, a display window, and NVDA.

For a fully unattended run, pass `--yes` with any settings you want to change:

```sh
uv run wn setup --yes --name Windows --software nvda
```

The camera and native battery are enabled by default. Use `--no-camera` or
`--no-battery` to leave either one out. Use `--headless` to create a VM without
a display window. Run `uv run wn setup --help` for all available settings.

## Use the VM

| Command | What it does |
|---|---|
| `uv run wn start` | Starts Windows. |
| `uv run wn stop` | Shuts down Windows. |
| `uv run wn status` | Shows whether the VM is running. |
| `uv run wn ip` | Prints the Windows IP address. |
| `uv run wn key ctrl+alt+del` | Sends one or more key combinations. |
| `uv run wn type 'Get-ComputerInfo'` | Types text into Windows. |
| `uv run wn mouse move 640 400 --screen 1280x800` | Moves, clicks, or scrolls the mouse. |
| `uv run wn screenshot screen.png` | Saves the Windows screen as a PNG. |
| `uv run wn grab` | Sends every Mac key press to Windows. |

Run `uv run wn <command> --help` for the options available to a command.

For everyday use, type in the UTM window. Command maps to the Windows key.
Control and Option together capture or release the keyboard.

If you prefer to grab the Mac keyboard directly, or use the VM headless, run
`uv run wn grab`. Press Control, Option, and Command together to release or
grab the keyboard.

## Shared folders

Show a folder from the Mac as a live folder in Windows:

```sh
uv run wn share ~/Projects 'C:\Users\you\Projects'
```

The Windows folder must be missing or empty, and its parent folder must exist.
Add `--read-only` if Windows should not change the Mac files. List shared
folders and their state, or stop sharing one, with:

```sh
uv run wn shares
uv run wn unshare Projects
```

Windows writes reach the Mac about five seconds after a file is closed. Mac
changes reach Windows in under a second, but Windows apps that watch folders
do not receive change notifications and may need a manual refresh. Mac
symlinks do not appear in Windows. Run `uv run wn share --help` for the full
list of file name, attribute, and path limitations.

The wn agent must be running while a folder is shared. If the Mac is asleep or
the agent is stopped, Windows file operations wait until the Mac side is
available again without losing data.

## Hardware support

### Audio and microphone

Windows uses the Mac's default speakers and microphone. Audio is configured
for low latency. The first time a Windows app records, macOS asks whether UTM
may use the microphone.

Bluetooth headphones can switch to headset mode while the microphone is
active. Sound quality returns to normal when recording stops.

### Camera

Windows sees the Mac's default camera as a standard USB webcam named
"Mac Camera." The first time it starts, macOS asks whether wn-agent may use
the camera.

The camera is active only while a Windows app is using it.

### Battery

Windows shows the Mac's charge level and power state in the taskbar, Settings,
Device Manager, and NVDA. A small macOS agent keeps it in sync and also feeds
the camera to the VM.

macOS asks whether wn-agent may access UTM's data. Choose **Allow**. If access
is denied, add wn-agent to System Settings > Privacy & Security > Full Disk
Access.

### Screen reader

NVDA is installed by default and starts when you sign in. On an M4 Pro, the
measured time from a key press to NVDA speech was about 115 ms. While most of this latency is caused by the audio stack, work is in progress to reduce it further using every hack known to man.

## Use with a coding agent

Start your coding agent from the cloned `wn` directory and ask it to use the
Windows VM. The repository contains the CLI and its help, which gives the
agent everything it needs to start Windows, inspect the screen, and send
keyboard or mouse input.

No plugin, server, or agent specific configuration is required.

## Current limitations

- There is no GPU acceleration yet. Windows renders the desktop on the CPU.
- Microsoft has only authorized Parallels Desktop for running Windows 11 Pro on a Mac. Windows installs with Microsoft's generic key and remains unactivated until you enter your own key in Settings.

## Roadmap

1. GPU acceleration.
2. Windows IoT Enterprise LTSC support.

## Development

Run the tests on macOS:

```sh
uv run python -m unittest discover -s tests
```

The main project folders are:

1. `wn/` contains the CLI, setup code, UTM configuration, camera helper, and
   VM control code.
2. `tests/` contains the automated tests.
3. `keygrab/` contains the Swift keyboard helper, in case you do not want to send keys through UTM.
4. `efi/` contains the native battery firmware driver.

## License

MIT. See [LICENSE](LICENSE).
