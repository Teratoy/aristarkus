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
```

## Running from source

Needs Python 3.10+ and nothing else.

```bash
python3 aristarkus.py
```

## Building

PyInstaller can't cross-compile, so build on each system:

```bash
pip install pyinstaller
python build.py              # -> dist/
python tests/smoke.py dist/* # scan, tree, page and live events on a temp folder
```

The GitHub workflow builds and tests Linux and Windows on every push, and publishes a release when a `v*` tag is pushed.

## Credits

- Look and feel: the house style of [Daemon Domain](https://daemondomain.net) (monochrome glass, `web/theme.js`).
- Drawing and layout: [d3](https://d3js.org) (ISC license, see `web/d3-LICENSE.txt`).
- Font: [Tomorrow](https://github.com/MonicaRizzolli/Tomorrow) by Tony de Marco and Monica Rizzolli (SIL Open Font License, see `web/fonts/OFL.txt`).

## License

[MIT](LICENSE). The bundled d3 (ISC) and Tomorrow font (SIL OFL 1.1) keep their own licenses, listed above.
