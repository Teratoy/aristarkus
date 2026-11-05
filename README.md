# Aristarkus

A living map of the files on your computer. Every file is a circle sized by how big it is, sitting inside the circle of its folder, folders inside folders all the way down. When a program reads or writes a file, that file lights up and a thread connects it to whatever was touched just before.

Everything runs on your own machine: a small local server scans the folder you pick and serves the map at `http://localhost:8787`. Nothing is uploaded anywhere.

## Download

Grab a build from [Releases](../../releases/latest). No Python needed.

| System | File |
|---|---|
| Windows 10/11 | `aristarkus-windows-x64.exe` |
| Linux x86-64 | `aristarkus-linux-x64` |

Each has a `-debug` twin that also writes `aristarkus-debug.log` next to itself. Send that file along when reporting a problem.

**Windows:** the builds aren't signed, so SmartScreen will warn the first time. Choose **More info → Run anyway**. A console window stays open while it runs; close it to quit.

**Linux:** make it executable first.

```bash
chmod +x aristarkus-linux-x64 && ./aristarkus-linux-x64
```

Your browser opens on the map. The first scan of a big home folder takes a few seconds to a minute.

## Using it

- **Click a folder** to dive in; click its empty space or press **Esc** to rise. The path at the top-left jumps to any parent.
- **Scroll / drag** to zoom and pan freely. Dashed circles are folders whose contents load when you zoom into them.
- **Start folder** (panel): type any path and press Scan, or select a folder and click *Start from here*.
- **Live activity**: rings show reads (cyan), writes (orange), creates (green) and deletes (red). The log lists them; click one to jump to it. Type into *hide paths with...* to mute noisy folders.
- **Organic motion** (Display panel, off by default): circles drift gently inside their folders without ever overlapping.
- **Light or dark**: follows your system setting; the switch in the top-right corner flips it.
- **Stop Aristarkus** (panel): closes the program. Closing the browser tab alone leaves it running in the background.

## Shared folder

Keep one folder mirrored with another computer on your network, Windows or Linux. Whatever either of you adds, changes or deletes reaches the other within a few seconds.

1. On one computer, type a folder in the **Shared folder** panel (or leave it for `Aristarkus Shared` in your home folder) and press **Start sharing**.
2. Send the code it shows to the other computer, by any private route.
3. On the other computer, choose a folder there, paste the code and press **Join**. Files already in both folders are merged.

Aristarkus has to be open on both computers for files to move. Changes made while one is closed arrive the next time both are open. It reconnects by itself, with no new code.

**On a different network:** open **Network settings** in the panel.
- **The joining computer** needs the host's new address. Type it there, or use a computer name such as `alita.local`, which can stay the same across networks. No new code or re-pairing is needed.
- **The hosting computer** updates its code line by itself. You can choose which of its addresses the code shows, useful with a VPN, and change the port if something else uses it.
- **Pause sharing** stops all network activity, including listening, until you resume it, and keeps the pairing. Use it on a network you don't trust.

**If it can't connect:** Windows asks to allow network access the first time a Windows computer hosts, so choose **Allow**. It's usually easier for the Linux computer to host. Guest Wi-Fi networks often stop devices reaching each other.

**What happens to your files:**
- A deleted file moves to `.aristarkus/trash` inside the shared folder on the other computer, and stays for 30 days. A deleted folder stays if it still holds a temporary file that was never mirrored.
- A file changed on both computers is kept twice: the second copy is named `name (conflict from PC 1a2b3c4d).ext`.
- A file deleted on one computer and changed on the other comes back.
- If the folder goes missing, say on an unplugged drive, syncing stops rather than deleting anything.
- Not mirrored: names Windows can't store (such as `what?.txt`) and names differing only in capitals, both listed in the panel; symlinks; and temporary files (`.tmp`, `.part`, `.crdownload`, `.swp`, `~$` Office lock files, `Thumbs.db`, `desktop.ini`, `.DS_Store`, `.directory`).
- An interrupted transfer starts that file again. Moving or renaming a file sends it again.

**Security:** the connection is encrypted, and the code is both the password and the encryption key. Anyone with the code and access to your network can read and change the shared folder, and nothing outside it. Only one computer can be connected at a time. **Stop sharing** cancels the code and leaves the files in place. The code is saved unencrypted in your user config folder, readable by your account. The sharing controls are unlocked by the link the program opens, so other accounts on your computer can't use them.

Running from source, sharing needs Python 3.13 or newer. The Android app doesn't have it.

| System | Live activity |
|---|---|
| Linux | reads, writes, creates, deletes (inotify) |
| Windows | writes, creates, deletes. Windows doesn't report reads |
| macOS (from source) | none yet. The map works, without live activity |

## Options

```
--root PATH     start folder (default: your home folder)
--port N        first port to try (default: 8787; the next free one is used if busy)
--debug         verbose log, also written to aristarkus-debug.log
--no-browser    don't open the browser
--config DIR    where to remember the shared folder
```

## Running from source

Needs Python 3.10+ (3.13+ for the shared folder) and nothing else.

```bash
python3 aristarkus.py
```

## Building

PyInstaller can't cross-compile, so build on each system:

```bash
pip install pyinstaller
python build.py              # -> dist/
python tests/smoke.py dist/*       # scan, tree, page and live events on a temp folder
python tests/share_test.py dist/*  # two copies mirroring a folder
```

The GitHub workflow builds and tests Linux and Windows on every push, and publishes a release when a `v*` tag is pushed.

## Credits

- Look and feel: the house style of [Daemon Domain](https://daemondomain.net) (monochrome glass, `web/theme.js`).
- Drawing and layout: [d3](https://d3js.org) (ISC license, see `web/d3-LICENSE.txt`).
- Font: [Tomorrow](https://github.com/MonicaRizzolli/Tomorrow) by Tony de Marco and Monica Rizzolli (SIL Open Font License, see `web/fonts/OFL.txt`).

## License

[MIT](LICENSE). The bundled d3 (ISC) and Tomorrow font (SIL OFL 1.1) keep their own licenses, listed above.
