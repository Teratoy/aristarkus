"""Build the standalone executables for this system with PyInstaller.

    pip install pyinstaller
    python build.py

Produces two single-file programs in dist/, for example on Windows:
    aristarkus-windows-x64.exe          the app
    aristarkus-windows-x64-debug.exe    same app with --debug always on (writes aristarkus-debug.log)

PyInstaller can't cross-compile: run this on each system you want a build for
(the GitHub workflow does it for Linux and Windows).
"""
import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM = {'win32': 'windows', 'darwin': 'macos'}.get(sys.platform, 'linux')
ARCH = {'amd64': 'x64', 'x86_64': 'x64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(
    platform.machine().lower(), platform.machine().lower())


def build(name, entry):
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--onefile', '--console', '--noconfirm', '--clean',
        '--name', name,
        '--add-data', f'{os.path.join(HERE, "web")}{os.pathsep}web',
        '--distpath', os.path.join(HERE, 'dist'),
        '--workpath', os.path.join(HERE, 'build'),
        '--specpath', os.path.join(HERE, 'build'),
        os.path.join(HERE, entry),
    ], check=True)


if __name__ == '__main__':
    base = f'aristarkus-{SYSTEM}-{ARCH}'
    build(base, 'aristarkus.py')
    build(f'{base}-debug', 'debug_entry.py')
    print('\nbuilt:', *sorted(os.listdir(os.path.join(HERE, 'dist'))), sep='\n  ')
