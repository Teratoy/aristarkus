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
import select
import struct
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

__version__ = '0.1.3'

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


class Hub:
    """Coalesces events per (kind, path) and fans batches out to every open page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}
        self.overflowed = False
        self.clients = set()
        self.total = 0
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
            batch = [[k, web_path(p), n] for (k, p), n in items.items()]
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


STATE = State()

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
        log.debug('http %s', fmt % args)

    def _local(self):
        # refuse requests whose Host is not this machine (DNS-rebinding guard)
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].strip('[]')
        if host in ('localhost', '127.0.0.1', '::1'):
            return True
        log.warning('refused request with Host %r', self.headers.get('Host'))
        self.send_error(403)
        return False

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
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._local():
            return
        # parameters go in the URL: the Android app's WebView can't read request bodies, and the
        # page is the same everywhere
        url = urlparse(self.path)
        path, q = url.path, parse_qs(url.query)
        try:
            if path == '/api/root':
                self._json({'root': web_path(STATE.set_root(os_path(q.get('path', ['~'])[0])))})
            elif path == '/api/log':
                log.warning('page: %s', q.get('message', [''])[0][:2000])
                self._json({})
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
    ap.add_argument('--root', default='~', help='start folder (default: your home folder)')
    ap.add_argument('--port', type=int, default=8787, help='first port to try (default: 8787)')
    ap.add_argument('--debug', action='store_true', help='verbose log, also written to aristarkus-debug.log')
    ap.add_argument('--no-browser', action='store_true', help="don't open the browser")
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
    STATE.set_root(args.root)
    log.info('open %s  (close this window or press Ctrl+C to quit)', url)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
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
