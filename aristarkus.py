#!/usr/bin/env python3
"""Aristarkus: nested-circle map of a folder tree, with live file activity.

The server scans the start folder once (sizes only, one stat per file), watches it
for changes, and serves:

  /               the page (web/index.html)
  /api/status     scan progress + current start folder
  /api/root       POST ?path=  rescan from another folder
  /api/tree       ?path=&budget=  subtree, biggest folders expanded first
  /api/events     server-sent events: batches of [kind, path, count]
                  kind: r=read w=write c=create d=delete
  /api/log        POST ?message=  browser-side errors go to the debug log
  /api/quit       POST  stop the program
  /api/search     ?q=&path=  names containing q below path (a few seconds at most)
  /api/open       POST ?path=&reveal=  open a file, or show it in the file manager
  /api/who        ?path=  programs that have a file open right now
  /api/share      the shared folder (share.py). POST /api/share/host?folder=,
                  /api/share/join?folder=&invite=, /api/share/stop,
                  /api/share/configure?address=&port=&paused=

Live activity: Linux uses inotify (reads and writes). Windows uses
ReadDirectoryChangesW, which reports changes but not reads. Elsewhere the map is static.

Paths go to the page with '/' separators on every system.

Run:  python3 aristarkus.py [--root ~] [--port 8787] [--debug] [--no-browser]
"""
import argparse
import ctypes
import ctypes.util
import heapq
import json
import logging
import os
import platform
import queue
import re
import secrets
import select
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import share

__version__ = '0.2.2'

FROZEN = getattr(sys, 'frozen', False)
WEB = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))), 'web')
SKIP = {'/proc', '/sys', '/dev', '/run'}
FILE_CAP = 250        # files listed per folder; the rest merge into one "+N smaller files" circle
MAX_BATCH = 600       # events per 120 ms batch sent to the page

log = logging.getLogger('aristarkus')


def web_path(p):
    return p.replace(os.sep, '/') if os.sep != '/' else p


def os_path(p):
    return os.path.normpath(p) if os.sep != '/' else p

# ---------------------------------------------------------------- watchers


class NullWatcher:
    """No live activity on this system; the map still works."""
    reads = False
    mode = 'none'

    def __init__(self, state, root):
        self.error = f'live activity is not supported on {sys.platform}'
        self.failed = 0

    def add(self, path):
        pass

    def stop(self):
        pass

    def watched(self):
        return 0


if sys.platform.startswith('linux'):
    libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
    libc.inotify_init1.argtypes = [ctypes.c_int]
    libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]

    IN_ACCESS, IN_MODIFY, IN_CLOSE_WRITE, IN_CLOSE_NOWRITE = 0x1, 0x2, 0x8, 0x10
    IN_MOVED_FROM, IN_MOVED_TO, IN_CREATE, IN_DELETE = 0x40, 0x80, 0x100, 0x200
    IN_Q_OVERFLOW, IN_IGNORED, IN_ISDIR = 0x4000, 0x8000, 0x40000000
    IN_ONLYDIR, IN_DONT_FOLLOW, IN_EXCL_UNLINK = 0x01000000, 0x02000000, 0x04000000
    IN_MASK = (IN_ACCESS | IN_MODIFY | IN_CLOSE_WRITE | IN_CLOSE_NOWRITE |
               IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE)
    IN_HDR = struct.Struct('iIII')

    class InotifyWatcher:
        """One inotify instance with a watch on every folder of the current tree."""
        reads = True
        mode = 'inotify'

        def __init__(self, state, root):
            self.state = state
            self.paths = {}
            self.failed = 0
            self.error = None
            self.alive = True
            self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if self.fd < 0:
                self.error = os.strerror(ctypes.get_errno())
                log.error('inotify_init1 failed: %s', self.error)
            else:
                threading.Thread(target=self._run, daemon=True).start()

        def add(self, path):
            if self.fd < 0 or not self.alive:
                return
            wd = libc.inotify_add_watch(self.fd, os.fsencode(path),
                                        IN_MASK | IN_ONLYDIR | IN_DONT_FOLLOW | IN_EXCL_UNLINK)
            if wd >= 0:
                self.paths[wd] = path
            else:
                self.failed += 1
                err = os.strerror(ctypes.get_errno())
                if self.error is None:
                    self.error = err
                if self.failed <= 20:
                    log.debug('cannot watch %s: %s', path, err)

        def watched(self):
            return len(self.paths)

        def stop(self):
            self.alive = False      # the reader thread closes the fd

        def _run(self):
            fd = self.fd
            while self.alive:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    continue
                try:
                    data = os.read(fd, 1 << 16)
                except BlockingIOError:
                    continue
                except OSError as e:
                    log.error('inotify read failed: %s', e)
                    break
                i = 0
                while i + IN_HDR.size <= len(data):
                    wd, mask, _cookie, ln = IN_HDR.unpack_from(data, i)
                    name = data[i + IN_HDR.size:i + IN_HDR.size + ln].rstrip(b'\0')
                    i += IN_HDR.size + ln
                    self._handle(wd, mask, os.fsdecode(name))
            os.close(fd)

        def _handle(self, wd, mask, name):
            hub = self.state.hub
            if mask & IN_Q_OVERFLOW:
                hub.overflow()
                return
            if mask & IN_IGNORED:
                self.paths.pop(wd, None)
                return
            base = self.paths.get(wd)
            if base is None:
                return
            path = os.path.join(base, name) if name else base
            isdir = mask & IN_ISDIR
            if mask & (IN_CREATE | IN_MOVED_TO):
                kind = 'c'
                if isdir:
                    self.add(path)
                    self.state.add_dir(path)
            elif mask & (IN_DELETE | IN_MOVED_FROM):
                kind = 'd'
            elif isdir:
                return              # folder listings (including our own) are not shown
            elif mask & (IN_MODIFY | IN_CLOSE_WRITE):
                kind = 'w'
            elif mask & (IN_ACCESS | IN_CLOSE_NOWRITE):
                kind = 'r'
            else:
                return
            hub.push(kind, path)

    Watcher = InotifyWatcher

elif sys.platform == 'win32':
    from ctypes import wintypes

    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                          ctypes.c_void_p, ctypes.c_void_p]
    k32.ReadDirectoryChangesW.restype = wintypes.BOOL
    k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenThread.restype = wintypes.HANDLE
    k32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    INVALID_HANDLE = wintypes.HANDLE(-1).value
    FILE_LIST_DIRECTORY, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS = 0x1, 3, 0x02000000
    FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
    THREAD_TERMINATE = 0x1
    # file name, folder name, size, last write. Last-access is left out: Windows reports it as a
    # plain "modified" (indistinguishable from a write) and often doesn't update it at all.
    WIN_FILTER = 0x1 | 0x2 | 0x8 | 0x10
    WIN_ACTIONS = {1: 'c', 2: 'd', 3: 'w', 4: 'd', 5: 'c'}
    WIN_HDR = struct.Struct('III')

    class WindowsWatcher:
        """ReadDirectoryChangesW on the start folder with its whole subtree: one handle."""
        reads = False
        mode = 'ReadDirectoryChangesW'

        def __init__(self, state, root):
            self.state, self.root = state, root
            self.alive = True
            self.error = None
            self.failed = 0
            self.tid = None
            self.handle = k32.CreateFileW(root, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None,
                                          OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
            if not self.handle or self.handle == INVALID_HANDLE:
                self.handle = None
                self.error = ctypes.FormatError(ctypes.get_last_error()).strip()
                log.error('cannot watch %s: %s', root, self.error)
            else:
                threading.Thread(target=self._run, daemon=True).start()

        def add(self, path):
            pass                    # the root handle already covers every subfolder

        def watched(self):
            return self.state.progress['dirs'] if self.handle else 0

        def stop(self):
            self.alive = False
            if self.tid:            # unblock the pending ReadDirectoryChangesW call
                h = k32.OpenThread(THREAD_TERMINATE, False, self.tid)
                if h:
                    k32.CancelSynchronousIo(h)
                    k32.CloseHandle(h)

        def _run(self):
            self.tid = threading.get_native_id()
            buf = ctypes.create_string_buffer(1 << 16)
            got = wintypes.DWORD()
            hub = self.state.hub
            while self.alive:
                ok = k32.ReadDirectoryChangesW(self.handle, buf, len(buf), True, WIN_FILTER,
                                               ctypes.byref(got), None, None)
                if not ok:
                    if self.alive:
                        self.error = ctypes.FormatError(ctypes.get_last_error()).strip()
                        log.error('ReadDirectoryChangesW failed: %s', self.error)
                    break
                if got.value == 0:  # buffer overflowed; changes were lost
                    hub.overflow()
                    continue
                raw, i = buf.raw[:got.value], 0
                while True:
                    nxt, action, ln = WIN_HDR.unpack_from(raw, i)
                    name = raw[i + WIN_HDR.size:i + WIN_HDR.size + ln].decode('utf-16-le', 'replace')
                    self._handle(action, os.path.join(self.root, name))
                    if not nxt:
                        break
                    i += nxt
            k32.CloseHandle(self.handle)

        def _handle(self, action, path):
            kind = WIN_ACTIONS.get(action)
            if kind is None:
                return
            if kind in 'cw' and os.path.isdir(path):
                if kind == 'w':
                    return          # a folder "changes" whenever something inside it does
                self.state.add_dir(path)
            self.state.hub.push(kind, path)

    Watcher = WindowsWatcher

else:
    Watcher = NullWatcher


class OpenFiles:
    """Which programs have which files open, read from /proc on Linux. Nothing records who
    touched a file without administrator rights, so the activity log names the programs that had
    the file open when it was looked at, at most once a second. A quick open-read-close is missed."""
    supported = sys.platform.startswith('linux')

    def __init__(self):
        self.taken = 0
        self.owners = {}

    @staticmethod
    def scan(only=None):
        owners = {}
        me, uid = os.getpid(), os.getuid()
        for pid in os.listdir('/proc'):
            if not pid.isdigit() or int(pid) == me:
                continue
            try:
                if os.stat(f'/proc/{pid}').st_uid != uid:
                    continue
                fds = os.listdir(f'/proc/{pid}/fd')
                with open(f'/proc/{pid}/comm', encoding='utf-8', errors='replace') as f:
                    name = f.read().strip()
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(f'/proc/{pid}/fd/{fd}')
                except OSError:
                    continue
                if only is None or target == only:
                    owners.setdefault(target, {})[int(pid)] = name
        return owners

    def lookup(self):
        if time.monotonic() - self.taken > 1:
            self.owners, self.taken = self.scan(), time.monotonic()
        return self.owners


def who_has_open(path):
    """[(pid, program)] holding `path` open right now, or None if this system can't tell."""
    if OpenFiles.supported:
        return sorted(OpenFiles.scan(os.path.realpath(path)).get(os.path.realpath(path), {}).items())
    if sys.platform == 'win32':
        return restart_manager_list(path)
    return None


if sys.platform == 'win32':
    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [('dwProcessId', wintypes.DWORD), ('ProcessStartTime', wintypes.FILETIME)]

    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [('Process', RM_UNIQUE_PROCESS), ('strAppName', wintypes.WCHAR * 256),
                    ('strServiceShortName', wintypes.WCHAR * 64), ('ApplicationType', ctypes.c_int),
                    ('AppStatus', wintypes.ULONG), ('TSSessionId', wintypes.DWORD), ('bRestartable', wintypes.BOOL)]

    def restart_manager_list(path):
        """Windows' Restart Manager: the API installers use to find what holds a file open."""
        rm = ctypes.WinDLL('rstrtmgr')
        session, key = wintypes.DWORD(), ctypes.create_unicode_buffer(64)
        if rm.RmStartSession(ctypes.byref(session), 0, key):
            return None
        try:
            files = (wintypes.LPCWSTR * 1)(path)
            if rm.RmRegisterResources(session, 1, files, 0, None, 0, None):
                return None
            needed, count, reasons = wintypes.UINT(), wintypes.UINT(0), wintypes.DWORD()
            info = None
            for _ in range(3):      # the list can grow between asking for its size and reading it
                r = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), info, ctypes.byref(reasons))
                if r == 0:
                    break
                if r != 234:        # anything but ERROR_MORE_DATA
                    return None
                info = (RM_PROCESS_INFO * needed.value)()
                count = wintypes.UINT(needed.value)
            if info is None:
                return []
            return sorted((p.Process.dwProcessId, p.strAppName) for p in info[:count.value])
        finally:
            rm.RmEndSession(session)


class Hub:
    """Coalesces events per (kind, path) and fans batches out to every open page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}
        self.overflowed = False
        self.clients = set()
        self.total = 0
        self.open_files = OpenFiles() if OpenFiles.supported else None
        threading.Thread(target=self._flush, daemon=True).start()

    def push(self, kind, path):
        with self.lock:
            key = (kind, path)
            self.pending[key] = self.pending.get(key, 0) + 1

    def overflow(self):
        log.warning('event queue overflowed; some activity was lost')
        with self.lock:
            self.overflowed = True

    def subscribe(self):
        q = queue.Queue(maxsize=40)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def _flush(self):
        reported = time.monotonic()
        while True:
            time.sleep(0.12)
            if log.isEnabledFor(logging.DEBUG) and time.monotonic() - reported > 10:
                log.debug('activity: %d events in the last 10 s, %d page(s) listening',
                          self.total, len(self.clients))
                self.total, reported = 0, time.monotonic()
            with self.lock:
                if not self.pending and not self.overflowed:
                    continue
                items, self.pending = self.pending, {}
                overflowed, self.overflowed = self.overflowed, False
                clients = list(self.clients)
            owners = self.open_files.lookup() if self.open_files and clients else {}
            batch = []
            for (k, p), n in items.items():
                progs = sorted(set(owners.get(p, {}).values()))
                batch.append([k, web_path(p), n, ', '.join(progs)] if progs else [k, web_path(p), n])
            self.total += len(batch)
            batch.sort(key=lambda e: e[0] == 'r')   # writes/creates/deletes survive the cap first
            dropped = max(0, len(batch) - MAX_BATCH)
            msg = json.dumps({'e': batch[:MAX_BATCH], 'drop': dropped, 'overflow': overflowed})
            for q in clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

# ---------------------------------------------------------------- tree


class Dir:
    __slots__ = ('name', 'parent', 'dirs', 'size', 'files', 'count')

    def __init__(self, name, parent):
        self.name, self.parent = name, parent
        self.dirs = {}
        self.size = 0       # bytes below this folder
        self.files = 0      # files directly inside
        self.count = 0      # files below this folder


def is_subdir(entry):
    """Real subfolder: not a symlink, and not a Windows junction (those loop or double-count)."""
    if not entry.is_dir(follow_symlinks=False):
        return False
    is_junction = getattr(entry, 'is_junction', None)
    return not (is_junction and is_junction())


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.hub = Hub()
        self.gen = 0
        self.root_path = None
        self.root = None
        self.scanning = False
        self.progress = {'dirs': 0, 'files': 0, 'bytes': 0}
        self.watcher = None

    def set_root(self, path):
        path = os.path.realpath(os.path.expanduser(path))
        if not os.path.isdir(path):
            raise ValueError(f'not a folder: {path}')
        with self.lock:
            self.gen += 1
            if self.watcher:
                self.watcher.stop()
            self.root_path, self.root, self.scanning = path, None, True
            self.progress = {'dirs': 0, 'files': 0, 'bytes': 0}
            self.watcher = Watcher(self, path)
            args = (self.gen, path, self.watcher, self.progress)
        log.info('scanning %s (live activity: %s)', path, self.watcher.mode)
        threading.Thread(target=self._scan, args=args, daemon=True).start()
        return path

    def _scan(self, gen, path, watcher, prog):
        started = time.monotonic()
        errors = 0
        root = Dir(os.path.basename(path) or path, None)
        order, stack = [], [(root, path)]
        while stack:
            if gen != self.gen:
                log.debug('scan of %s abandoned', path)
                return
            d, p = stack.pop()
            order.append(d)
            watcher.add(p)
            try:
                with os.scandir(p) as it:
                    for e in it:
                        try:
                            if is_subdir(e):
                                if e.path not in SKIP:
                                    c = d.dirs[e.name] = Dir(e.name, d)
                                    stack.append((c, e.path))
                            else:
                                s = e.stat(follow_symlinks=False).st_size
                                d.size += s
                                d.files += 1
                                prog['files'] += 1
                                prog['bytes'] += s
                        except OSError as err:
                            errors += 1
                            if errors <= 20:
                                log.debug('skipped %s: %s', e.path, err)
            except OSError as err:
                errors += 1
                if errors <= 20:
                    log.debug('cannot list %s: %s', p, err)
            prog['dirs'] += 1
        for d in reversed(order):           # children come after parents in `order`
            d.count += d.files
            if d.parent:
                d.parent.size += d.size
                d.parent.count += d.count
        with self.lock:
            if gen == self.gen:
                self.root, self.scanning = root, False
        log.info('scanned %s: %d files, %d folders, %.1f GB in %.1f s (%d unreadable, %d unwatched)',
                 path, prog['files'], prog['dirs'], prog['bytes'] / 1e9, time.monotonic() - started,
                 errors, watcher.failed)

    def find(self, path):
        if self.root is None:
            return None
        try:
            rel = os.path.relpath(path, self.root_path)
        except ValueError:          # different drive on Windows
            return None
        if rel.startswith('..'):
            return None
        d = self.root
        if rel != '.':
            for part in rel.split(os.sep):
                d = d.dirs.get(part)
                if d is None:
                    return None
        return d

    def add_dir(self, path):
        with self.lock:
            parent = self.find(os.path.dirname(path))
            name = os.path.basename(path)
            if parent is not None and name not in parent.dirs:
                parent.dirs[name] = Dir(name, parent)

    def subtree(self, path, budget):
        """Folder at `path`, expanding the biggest folders first until `budget` circles are used.
        Folders left unexpanded come back as kind 'c' and the page fetches them when zoomed into."""
        with self.lock:
            d = self.find(path)
            if d is None:
                return None

        def stub(dd, name):
            return {'n': name, 'k': 'c', 's': dd.size, 'm': dd.count}

        top = stub(d, os.path.basename(path) or path)
        used, tick = 0, 0
        heap = [(-d.size, tick, d, top, path)]
        while heap:
            _, _, dd, node, p = heapq.heappop(heap)
            need = len(dd.dirs) + min(dd.files, FILE_CAP + 1)
            if node is not top and used + need > budget:
                break
            files = []
            try:
                with os.scandir(p) as it:
                    for e in it:
                        try:
                            if not is_subdir(e):
                                s = e.stat(follow_symlinks=False).st_size
                                if s > 0:
                                    files.append((s, e.name))
                        except OSError:
                            pass
            except OSError:
                pass
            files.sort(reverse=True)
            kids = []
            for name, sub in list(dd.dirs.items()):
                if sub.size > 0:
                    c = stub(sub, name)
                    kids.append(c)
                    tick += 1
                    heapq.heappush(heap, (-sub.size, tick, sub, c, os.path.join(p, name)))
            kids += [{'n': n, 'k': 'f', 's': s} for s, n in files[:FILE_CAP]]
            rest = files[FILE_CAP:]
            if rest:
                kids.append({'n': f'+{len(rest)} smaller files', 'k': 'o',
                             's': sum(s for s, _ in rest), 'm': len(rest)})
            kids.sort(key=lambda c: -c['s'])
            node['k'], node['c'] = 'd', kids
            used += len(kids)
        return top

    def status(self):
        w = self.watcher
        return {'version': __version__, 'platform': sys.platform,
                'root': web_path(self.root_path), 'home': web_path(os.path.expanduser('~')),
                'gen': self.gen, 'scanning': self.scanning, **self.progress,
                'watchMode': w.mode, 'reads': w.reads, 'watches': w.watched(),
                'watchFailed': w.failed, 'watchError': w.error}


def search(base, query, limit=100, budget=4.0):
    """Names containing `query` (any case) below `base`. Walks the disk rather than the scan, which
    only kept folder totals. Stops after `limit` hits or `budget` seconds."""
    q = query.casefold()
    found, stack, looked = [], [base], 0
    started = time.monotonic()
    partial = False
    while stack:
        if time.monotonic() - started > budget or len(found) >= limit:
            partial = True
            break
        try:
            with os.scandir(stack.pop()) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            looked += 1
            try:
                folder = is_subdir(e)
                if folder and e.path not in SKIP:
                    stack.append(e.path)
                if q in e.name.casefold():
                    found.append({'p': web_path(e.path), 'k': 'd' if folder else 'f',
                                  's': 0 if folder else e.stat(follow_symlinks=False).st_size})
            except OSError:
                pass
    found.sort(key=lambda r: (r['p'].count('/'), r['p'].casefold()))
    return {'results': found[:limit], 'partial': partial, 'looked': looked}


# Opening one of these would run it. The map only ever shows them in the file manager.
RUNNABLE = {'.exe', '.com', '.bat', '.cmd', '.msi', '.msc', '.msp', '.scr', '.pif', '.lnk', '.url', '.cpl', '.hta',
            '.jar', '.jse', '.js', '.vbs', '.vbe', '.wsf', '.wsh', '.ps1', '.psm1', '.reg', '.inf', '.application',
            '.appref-ms', '.settingcontent-ms', '.diagcab', '.desktop', '.appimage', '.sh', '.run', '.command',
            '.app', '.pkg', '.deb', '.rpm', '.flatpakref', '.py', '.pyw', '.pl', '.rb'}


def runnable(path):
    ext = os.path.splitext(path)[1].lower()
    pathext = {e.lower() for e in os.environ.get('PATHEXT', '').split(';') if e}
    return ext in RUNNABLE | pathext or (os.name == 'posix' and os.path.isfile(path) and os.access(path, os.X_OK))


def child_env():
    """The environment to start other programs in. A Linux standalone build points the library
    path at its own bundled libraries, which would break whatever it starts."""
    env = dict(os.environ)
    if FROZEN and 'LD_LIBRARY_PATH' in env:
        orig = env.pop('LD_LIBRARY_PATH_ORIG', None)
        if orig:
            env['LD_LIBRARY_PATH'] = orig
        else:
            del env['LD_LIBRARY_PATH']
    return env


def open_path(path, reveal):
    path = os.path.abspath(path)
    if not os.path.lexists(path):
        raise ValueError(f'no longer there: {path}')
    if not reveal and (os.path.isdir(path) or runnable(path)):
        raise ValueError("this would run a program: use Show in folder")
    record = os.environ.get('ARISTARKUS_OPEN_RECORD')     # the tests write here instead of opening
    if record:
        with open(record, 'a', encoding='utf-8') as f:
            f.write(f"{'reveal' if reveal else 'open'}\t{path}\n")
        return
    quiet = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    if sys.platform == 'win32':
        if reveal:
            subprocess.Popen(['explorer', f'/select,{path}'], **quiet)
        else:
            os.startfile(path)
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', path] if reveal else ['open', path], **quiet)
    else:
        env = child_env()
        if reveal:      # the freedesktop file-manager interface selects the file; failing that, open its folder
            shown = subprocess.run(['dbus-send', '--session', '--dest=org.freedesktop.FileManager1', '--type=method_call',
                                    '/org/freedesktop/FileManager1', 'org.freedesktop.FileManager1.ShowItems',
                                    f'array:string:{Path(path).as_uri()}', 'string:'], env=env, timeout=5, **quiet)
            if shown.returncode == 0:
                return
            path = os.path.dirname(path)
        subprocess.Popen(['xdg-open', path], env=env, start_new_session=True, **quiet)


STATE = State()
SHARE = None
TOKEN = ''
CONFIG = ''


def remember_root(path):
    try:
        share.write_json(os.path.join(CONFIG, 'settings.json'), {'root': path})
    except OSError as e:
        log.debug('cannot remember the start folder: %s', e)


def remembered_root():
    saved = share.read_json(os.path.join(CONFIG, 'settings.json')) or {}
    root = saved.get('root') if isinstance(saved, dict) else None
    return root if isinstance(root, str) and os.path.isdir(root) else '~'

# ---------------------------------------------------------------- http

STATIC = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/d3.min.js': ('d3.min.js', 'text/javascript; charset=utf-8'),
    '/theme.js': ('theme.js', 'text/javascript; charset=utf-8'),
    '/fonts/Tomorrow-Regular.ttf': ('fonts/Tomorrow-Regular.ttf', 'font/ttf'),
    '/fonts/Tomorrow-Bold.ttf': ('fonts/Tomorrow-Bold.ttf', 'font/ttf'),
}


class Handler(BaseHTTPRequestHandler):
    server_version = f'Aristarkus/{__version__}'

    def log_message(self, fmt, *args):
        # the share code in a join request must stay out of a log people send around
        log.debug('http %s', re.sub(r'(invite|address)=[^&\s]*', r'\1=(hidden)', fmt % args))

    def _local(self):
        # refuse requests whose Host is not this machine (DNS-rebinding guard)
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]')
        if host in ('localhost', '127.0.0.1', '::1'):
            return True
        log.warning('refused request with Host %r', self.headers.get('Host'))
        self.send_error(403)
        return False

    def _same_origin(self):
        # any website can send POSTs to localhost, and browsers say where they came from
        origin = self.headers.get('Origin')
        if origin is None or urlparse(origin).netloc == self.headers.get('Host'):
            return True
        log.warning('refused a request from %r', origin)
        self.send_error(403)
        return False

    def _has_token(self):
        # sharing can send any folder over the network, so being on this machine isn't enough:
        # other accounts here can reach 127.0.0.1 too. Only this user's browser was given the token.
        sent = (self.headers.get('X-Aristarkus-Token') or '').encode()
        return secrets.compare_digest(sent, TOKEN.encode())

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, separators=(',', ':')).encode(), 'application/json', code)

    def do_GET(self):
        if not self._local():
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            with open(os.path.join(WEB, name), 'rb') as f:
                self._send(f.read(), ctype)
        elif url.path == '/api/status':
            self._json(STATE.status())
        elif url.path == '/api/tree':
            if STATE.scanning:
                return self._json({'error': 'scanning'}, 409)
            path = os_path(q['path'][0]) if 'path' in q else STATE.root_path
            budget = max(50, min(200000, int(q.get('budget', ['20000'])[0])))
            started = time.monotonic()
            tree = STATE.subtree(path, budget)
            if tree is None:
                log.debug('tree: %s not found', path)
                return self._json({'error': 'not found'}, 404)
            log.debug('tree %s (budget %d) in %.2f s', path, budget, time.monotonic() - started)
            self._json(tree)
        elif url.path == '/api/events':
            self._events()
        elif url.path == '/api/share':
            self._json(SHARE.status() if self._has_token() else {'locked': True})
        elif url.path == '/api/share/trash':
            if not self._has_token():
                return self._json({'error': 'locked'}, 403)
            self._json({'items': SHARE.trash_list()})
        elif url.path == '/api/search':
            if not self._has_token():
                return self._json({'error': 'locked'}, 403)
            base = os_path(q['path'][0]) if 'path' in q else STATE.root_path
            self._json(search(base, q.get('q', [''])[0]) if q.get('q', [''])[0].strip() else {'results': []})
        elif url.path == '/api/who':
            if not self._has_token():
                return self._json({'error': 'locked'}, 403)
            found = who_has_open(os_path(q.get('path', [''])[0]))
            self._json({'supported': found is not None,
                        'programs': [{'pid': pid, 'name': name} for pid, name in found or []]})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._local() or not self._same_origin():
            return
        # parameters go in the URL: the Android app's WebView can't read request bodies, and the
        # page is the same everywhere
        url = urlparse(self.path)
        path, q = url.path, parse_qs(url.query)
        if (path.startswith('/api/share/') or path in ('/api/quit', '/api/open')) and not self._has_token():
            log.warning('refused %s without the access token', path)
            return self._json({'error': 'open Aristarkus from its shortcut or the link it shows when it starts'}, 403)
        try:
            if path == '/api/root':
                chosen = STATE.set_root(os_path(q.get('path', ['~'])[0]))
                remember_root(chosen)
                self._json({'root': web_path(chosen)})
            elif path == '/api/open':
                open_path(os_path(q.get('path', [''])[0]), q.get('reveal', ['0'])[0] == '1')
                self._json({})
            elif path == '/api/share/restore':
                self._json({'path': SHARE.restore(q.get('item', [''])[0])})
            elif path == '/api/log':
                log.warning('page: %s', q.get('message', [''])[0][:2000])
                self._json({})
            elif path == '/api/quit':
                self._json({})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif path == '/api/share/host':
                SHARE.host(q.get('folder', [''])[0])
                self._json(SHARE.status())
            elif path == '/api/share/join':
                SHARE.join(q.get('folder', [''])[0], q.get('invite', [''])[0])
                self._json(SHARE.status())
            elif path == '/api/share/configure':
                q = parse_qs(url.query, keep_blank_values=True)     # a blank address means automatic
                paused = q['paused'][0] == '1' if 'paused' in q else None
                SHARE.configure(q.get('address', [None])[0], q.get('port', [None])[0], paused)
                self._json(SHARE.status())
            elif path == '/api/share/stop':
                SHARE.stop()
                self._json(SHARE.status())
            else:
                self.send_error(404)
        except (ValueError, OSError) as e:
            log.warning('%s failed: %s', path, e)
            self._json({'error': str(e)}, 400)

    def _events(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        q = STATE.hub.subscribe()
        try:
            while True:
                try:
                    msg = f'data: {q.get(timeout=15)}\n\n'
                except queue.Empty:
                    msg = ': ping\n\n'
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            STATE.hub.unsubscribe(q)

# ---------------------------------------------------------------- main


def log_file_path():
    base = os.path.dirname(sys.executable) if FROZEN else os.getcwd()
    return os.path.join(base, 'aristarkus-debug.log')


def setup_logging(debug):
    handlers = [logging.StreamHandler()]
    if debug:
        handlers.append(logging.FileHandler(log_file_path(), mode='w', encoding='utf-8'))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers,
                        format='%(asctime)s %(levelname)-7s %(message)s', datefmt='%H:%M:%S')


def bind(port):
    for p in range(port, port + 20):
        try:
            return ThreadingHTTPServer(('127.0.0.1', p), Handler)
        except OSError:
            log.debug('port %d busy', p)
    raise SystemExit(f'no free port between {port} and {port + 19}')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='aristarkus', description=__doc__.split('\n')[0])
    ap.add_argument('--root', help='start folder (default: the last one, or your home folder)')
    ap.add_argument('--port', type=int, default=8787, help='first port to try (default: 8787)')
    ap.add_argument('--debug', action='store_true', help='verbose log, also written to aristarkus-debug.log')
    ap.add_argument('--no-browser', action='store_true', help="don't open the browser")
    ap.add_argument('--config', help='where to remember the shared folder (default: your user config folder)')
    ap.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = ap.parse_args(argv)
    setup_logging(args.debug)
    log.info('Aristarkus %s on %s, Python %s%s', __version__, platform.platform(),
             platform.python_version(), ' (standalone)' if FROZEN else '')
    if args.debug:
        log.info('debug log: %s', log_file_path())
    server = bind(args.port)
    server.daemon_threads = True
    url = f'http://localhost:{server.server_address[1]}'
    global SHARE, TOKEN, CONFIG
    config = CONFIG = args.config or share.default_config_dir()
    STATE.set_root(args.root or remembered_root())
    SHARE = share.Share(config)
    TOKEN = share.load_token(config)
    SHARE.start()
    log.info('open %s  (close this window or press Ctrl+C to quit)', url)
    link = f'{url}/#token={TOKEN}'
    print(f'Link with the sharing controls unlocked, for you only: {link}', flush=True)     # not in the log file
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (link,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    SHARE.shutdown()
    log.info('bye')


def run(argv=None):
    """Entry point for the standalone builds: keep the console open if startup fails,
    so a double-clicked .exe doesn't vanish before the error can be read."""
    try:
        main(argv)
    except SystemExit:
        raise
    except Exception:
        log.exception('Aristarkus crashed')
        if FROZEN and sys.platform == 'win32':
            input('\nPress Enter to close...')
        raise


if __name__ == '__main__':
    run()
