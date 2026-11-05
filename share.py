"""Shared folder: keep one folder mirrored with one other computer on the network.

One computer hosts: it listens on the network and shows a code. The other joins with that code.
The connection is TLS with the code as a pre-shared key, so the code both proves who is asking
and encrypts everything. The code holds 160 random bits, which rules out guessing it.

The joining computer decides everything. Each round it compares three lists, the files here, the
files there, and what they were at the last sync, then tells the host what to do. Only that
computer keeps the last-sync list, so losing it merely turns the next round into a merge.

Nothing is ever lost to a sync:
  - deleted files move to .aristarkus/trash on the computer they're deleted from by the sync
  - a file changed on both sides is kept twice, as "name (conflict from PC 1a2b3c4d).ext"
  - a file deleted on one side and changed on the other comes back
  - if a shared folder or its .aristarkus folder disappears (say, an unplugged drive), syncing
    stops instead of reading that as "everything was deleted"
  - a folder that can't be listed stops the round for the same reason

Paths are '/'-separated between computers. A name Windows can't store is reported, not synced.
"""
import base64
import ctypes
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import shutil
import socket
import ssl
import stat
import struct
import sys
import threading
import time
from collections import deque

log = logging.getLogger('aristarkus')

PROTOCOL = 1
PORT = 8788
SYSTEM_DIR = '.aristarkus'
INTERVAL = float(os.environ.get('ARISTARKUS_SHARE_INTERVAL', 5))   # seconds between rounds
SETTLE = float(os.environ.get('ARISTARKUS_SHARE_SETTLE', 3))       # a file this fresh may still be written
TRASH_DAYS = 30
CHUNK = 1 << 20
MAX_FRAME = 1 << 28
TIMEOUT = 120
RETRY = float(os.environ.get('ARISTARKUS_SHARE_RETRY', 60))        # seconds before trying a failed file again
IDLE = 30         # a connection this quiet can be replaced by a new one with the same code
CIPHERS = 'ECDHE-PSK-CHACHA20-POLY1305:ECDHE-PSK-AES256-CBC-SHA384'
SUPPORTED = hasattr(ssl.SSLContext, 'set_psk_server_callback') and getattr(ssl, 'HAS_PSK', False)
BUSY, DIR = 'busy', 'dir'   # scan values that aren't hashes

WIN_BAD = re.compile(r'[<>:"|?*\\\x00-\x1f]')
WIN_RESERVED = re.compile(r'^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$', re.I)
IGNORE_NAMES = {'thumbs.db', 'desktop.ini', '.ds_store', '.directory'}
IGNORE_RE = re.compile(r'^(~\$.*|\.~lock\..*#|.*\.(tmp|part|crdownload|swp))$', re.I)


class Lost(Exception):
    """The connection dropped. Deliberately not an OSError: those mean a local file problem."""


class Changed(Exception):
    """The file is no longer what the round saw. The next round looks again."""


class Refused(ValueError):
    """The other computer couldn't do what was asked."""


class FolderMissing(Exception):
    """The shared folder or its .aristarkus marker is gone."""


def ignored(name):
    return name.lower() in IGNORE_NAMES or IGNORE_RE.match(name) is not None


def name_problem(rel):
    """Why this path can't be mirrored, or None. Checked on every system: the other side is Windows."""
    for part in rel.split('/'):
        if part in ('', '.', '..'):
            return 'not a valid path'
        try:
            part.encode('utf-8')
        except UnicodeEncodeError:
            return 'the name is not valid text'
        if WIN_BAD.search(part) or part.endswith((' ', '.')) or WIN_RESERVED.match(part):
            return "Windows doesn't allow this name"
    return None


def is_subdir(entry):
    if not entry.is_dir(follow_symlinks=False):
        return False
    is_junction = getattr(entry, 'is_junction', None)
    return not (is_junction and is_junction())


def write_json(path, obj, private=False):
    tmp = f'{path}.{os.getpid()}.new'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o666)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(obj, f, separators=(',', ':'))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

# ---------------------------------------------------------------- the folder


class Folder:
    """One side's files: finds what's there, hashes it in the background, and applies changes."""

    def __init__(self, root, key_id):
        root = os.path.realpath(root)
        if sys.platform == 'win32' and not root.startswith('\\\\'):
            root = '\\\\?\\' + root         # paths past 260 characters
        self.root, self.key_id = root, key_id
        self.system = os.path.join(root, SYSTEM_DIR)
        self.lock = threading.Lock()
        self.alive = True
        self.todo = queue.Queue()
        self.queued = set()
        self.unreadable = {}
        self.dirty = False
        self.stats = {'files': 0, 'bytes': 0, 'busy': 0}
        self.hashes = (read_json(os.path.join(self.system, 'index.json')) or {}) if os.path.isdir(self.system) else {}
        threading.Thread(target=self._hash_worker, daemon=True).start()

    def close(self):
        self.alive = False
        self.save()

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            hashes, self.dirty = dict(self.hashes), False
        try:
            write_json(os.path.join(self.system, 'index.json'), hashes)
        except OSError as e:
            log.debug('cannot save the hash index: %s', e)

    def load_base(self):
        saved = read_json(os.path.join(self.system, 'base.json')) or {}
        return saved.get('base', {}) if saved.get('key') == self.key_id else {}

    def save_base(self, base):
        try:
            write_json(os.path.join(self.system, 'base.json'), {'key': self.key_id, 'base': base})
        except OSError as e:
            log.warning('cannot save the sync state: %s', e)

    def forget_base(self):
        try:
            os.remove(os.path.join(self.system, 'base.json'))
        except OSError:
            pass

    def guard(self):
        """Never write into a folder whose marker is gone: it may be an empty mountpoint."""
        if not os.path.isdir(self.system):
            raise FolderMissing('the shared folder is missing')

    def fs(self, rel):
        return os.path.join(self.root, *rel.split('/')) if rel else self.root

    def path(self, rel):
        """The real path of a file the other computer named. It can't leave the shared folder."""
        problem = name_problem(rel)
        if problem or rel.split('/')[0].lower() == SYSTEM_DIR:
            raise ValueError(f'refused path {rel!r}')
        full = self.fs(rel)
        parent = os.path.realpath(os.path.dirname(full))
        if parent != self.root and not parent.startswith(self.root + os.sep):
            raise ValueError(f'refused path {rel!r}')
        return full

    def scan(self):
        """rel -> DIR, a hash, or BUSY while a file settles or waits for its hash."""
        self.guard()
        found, stats = {}, {'files': 0, 'bytes': 0, 'busy': 0}
        now = time.time()
        stack = ['']
        while stack:
            rel = stack.pop()
            try:
                entries = list(os.scandir(self.fs(rel)))
            except FileNotFoundError:   # removed since its parent was listed
                if not rel:
                    raise FolderMissing('the shared folder is missing') from None
                found.pop(rel, None)
                continue
            # any other OSError stops the round: an unreadable folder must not look deleted
            for e in entries:
                child = f'{rel}/{e.name}' if rel else e.name
                if child == SYSTEM_DIR or ignored(e.name) or e.is_symlink():
                    continue
                if is_subdir(e):
                    found[child] = DIR
                    stack.append(child)
                elif e.is_file(follow_symlinks=False):
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        found[child] = BUSY
                        stats['busy'] += 1
                        continue
                    found[child] = self._known(child, st, now)
                    stats['files'] += 1
                    stats['bytes'] += st.st_size
                    stats['busy'] += found[child] == BUSY
        self.guard()        # a drive unplugged mid-walk looks like folders being removed
        with self.lock:
            gone = [rel for rel in self.hashes if rel not in found]
            for rel in gone:
                del self.hashes[rel]
            self.dirty = self.dirty or bool(gone)
            for rel in [r for r in self.unreadable if r not in found]:
                del self.unreadable[rel]
            self.stats = stats
        self.save()
        return found

    def _known(self, rel, st, now):
        with self.lock:
            c = self.hashes.get(rel)
            if c and c[0] == st.st_size and c[1] == st.st_mtime_ns:
                return c[2]
            if abs(now - st.st_mtime) >= SETTLE and rel not in self.queued:
                self.queued.add(rel)
                self.todo.put(rel)
        return BUSY

    def _hash_worker(self):
        while self.alive:
            try:
                rel = self.todo.get(timeout=0.5)
            except queue.Empty:
                continue
            path = self.fs(rel)
            try:
                before = os.stat(path, follow_symlinks=False)
                h = hashlib.sha256()
                with open(path, 'rb') as f:
                    while chunk := f.read(CHUNK):
                        h.update(chunk)
                        if not self.alive:
                            return
                after = os.stat(path, follow_symlinks=False)
            except OSError as e:
                with self.lock:
                    self.queued.discard(rel)
                    self.unreadable[rel] = e.strerror or str(e)
                continue
            with self.lock:
                self.queued.discard(rel)
                self.unreadable.pop(rel, None)
                if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
                    self.hashes[rel] = [after.st_size, after.st_mtime_ns, h.hexdigest()]
                    self.dirty = True

    def current(self, rel):
        """What's at rel right now: None, DIR, a hash, or BUSY if it changed since it was hashed."""
        try:
            st = os.stat(self.path(rel), follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISDIR(st.st_mode):
            return DIR
        with self.lock:
            c = self.hashes.get(rel)
        return c[2] if c and c[0] == st.st_size and c[1] == st.st_mtime_ns else BUSY

    def check(self, rel, expect, size=0):
        """Before receiving: is the file still what the round saw, and does the new one fit?"""
        path = self.path(rel)
        self.guard()
        if self.current(rel) != expect:
            raise Changed()
        if shutil.disk_usage(self.root).free < size + (1 << 20):
            raise OSError('not enough disk space')
        if expect and expect != DIR:
            with open(path, 'r+b'):    # on Windows this fails while another program has it open
                pass

    def receive(self, rel, expect, meta, chunks, progress=None):
        """Write a file from `chunks`, then swap it in only if it arrived whole and the old file
        hasn't changed. Always reads `chunks` to the end so the connection stays usable."""
        target = self.path(rel)
        tmpdir = os.path.join(self.system, 'tmp')
        tmp = os.path.join(tmpdir, secrets.token_hex(8))
        h = hashlib.sha256()
        failed, done, f = None, 0, None
        try:
            self.guard()
            try:
                os.mkdir(tmpdir, 0o700)
            except FileExistsError:
                pass
            f = open(tmp, 'wb')
        except (OSError, FolderMissing) as e:
            failed = e
        try:
            for chunk in chunks:
                done += len(chunk)
                h.update(chunk)
                if progress:
                    progress(done)
                if f and not failed:
                    try:
                        f.write(chunk)
                    except OSError as e:
                        failed = e
            if f and not failed:
                try:
                    f.flush()
                    os.fsync(f.fileno())    # on disk before it replaces a good copy
                except OSError as e:
                    failed = e
            if f:
                f.close()
                f = None
            if failed:
                raise failed
            if h.hexdigest() != meta['hash']:
                raise Changed()     # it changed while being sent
            if expect and expect != DIR:
                shutil.copymode(target, tmp)
            elif meta.get('mode') is not None and os.name == 'posix':
                os.chmod(tmp, meta['mode'] & 0o777)
            os.utime(tmp, ns=(meta['mtime'], meta['mtime']))
            st = os.stat(tmp)
            if self.current(rel) != expect:
                raise Changed()
            self.guard()
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(tmp, target)
            with self.lock:
                self.hashes[rel] = [st.st_size, st.st_mtime_ns, meta['hash']]
                self.dirty = True
        finally:
            if f:
                f.close()
            try:
                os.remove(tmp)
            except OSError:
                pass

    def forget(self, rel):
        """Its content turned out not to match the hash: hash it again."""
        with self.lock:
            self.hashes.pop(rel, None)
            self.dirty = True

    def rename(self, old, new):
        """Only for a change of capitals, which a new file plus a delete can't do on Windows."""
        src, dst = self.path(old), self.path(new)
        self.guard()
        if old.lower() != new.lower():
            raise ValueError(f'refused rename of {old!r}')
        if not os.path.lexists(src) or (os.path.lexists(dst) and not os.path.samefile(src, dst)):
            raise Changed()
        os.rename(src, dst)
        with self.lock:
            for rel in [r for r in self.hashes if r == old or r.startswith(old + '/')]:
                self.hashes[new + rel[len(old):]] = self.hashes.pop(rel)
            self.dirty = True

    def trash(self, rel, expect):
        path = self.path(rel)
        self.guard()
        cur = self.current(rel)
        if cur is None:
            return
        if cur != expect:
            raise Changed()
        dest = os.path.join(self.system, 'trash', time.strftime('%Y-%m-%d_%H%M%S'), *rel.split('/'))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        while os.path.exists(dest):
            dest += '~'
        os.replace(path, dest)
        with self.lock:
            self.hashes.pop(rel, None)
            self.dirty = True

    def mkdir(self, rel):
        path = self.path(rel)
        self.guard()
        os.makedirs(path, exist_ok=True)

    def rmdir(self, rel):
        """Remove a folder if it's empty apart from system clutter. A .tmp or .part file there
        never synced, so it may be the only copy: its folder stays."""
        path = self.path(rel)
        self.guard()
        try:
            names = os.listdir(path)
            if all(n.lower() in IGNORE_NAMES for n in names):
                for n in names:
                    os.remove(os.path.join(path, n))
                os.rmdir(path)
        except OSError as e:
            log.debug('kept folder %s: %s', rel, e)

    def purge_trash(self):
        shutil.rmtree(os.path.join(self.system, 'tmp'), ignore_errors=True)     # left by a crash
        trash = os.path.join(self.system, 'trash')
        cutoff = time.time() - TRASH_DAYS * 86400
        try:
            for e in list(os.scandir(trash)):
                if e.stat().st_mtime < cutoff:
                    shutil.rmtree(e.path, ignore_errors=True)
        except OSError:
            pass


def conflict_name(rel, peer, hash_):
    """Named after the content, so retrying a conflict can't make a second copy."""
    head, _, name = rel.rpartition('/')
    stem, dot, ext = name.rpartition('.')
    if not stem:        # no extension, or a name like ".bashrc"
        stem, dot, ext = name, '', ''
    peer = re.sub(r'[<>:"/|?*\\\x00-\x1f]', '', peer).strip(' .') or 'other computer'
    new = f'{stem} (conflict from {peer} {hash_[:8]}){dot}{ext}'
    return f'{head}/{new}' if head else new


def plan(local, remote, base):
    """Compare both sides with the last sync. Returns (ops, problems, waiting). `base` is updated
    for every path that already matches. Ops are (kind, path, ...) tuples in the order to run them:
    renames, then new folders, then transfers, then deletes, deepest first."""
    problems, waiting = {}, 0
    paths = set(local) | set(remote) | set(base)
    folds = {}
    for p in set(local) | set(remote):
        folds.setdefault(p.lower(), []).append(p)
    clashes = {f for f, same in folds.items() if len(same) > 1}
    renames, renaming = [], set()
    for f in sorted(clashes, key=lambda f: f.count('/')):
        parts = f.split('/')
        if len(folds[f]) == 2 and not any('/'.join(parts[:i]) in renaming for i in range(1, len(parts))):
            here, there = sorted(folds[f], key=lambda p: p in remote)
            if here in local and here not in remote and there in remote and there not in local and not (
                    name_problem(here) or name_problem(there)):
                if there in base:
                    renames.append(('rename-there', there, here))
                    renaming.add(f)
                elif here in base:
                    renames.append(('rename-here', here, there))
                    renaming.add(f)
    mkdirs, moves, deletes, rmdirs = [], [], [], []
    for p in sorted(paths):
        L, R, B = local.get(p), remote.get(p), base.get(p)
        if L is None and R is None:
            base.pop(p, None)
            continue
        why = name_problem(p)
        if why:
            problems[p] = why
            continue
        if clashes:
            parts = p.lower().split('/')
            above = {'/'.join(parts[:i + 1]) for i in range(len(parts))}
            if above & renaming:
                waiting += 1            # the next round compares them under the new name
                continue
            if above & clashes:
                problems[p] = "another name differs only in capital letters, which Windows can't keep apart"
                continue
        if L == BUSY or R == BUSY:
            waiting += 1
            continue
        if L == R:
            base[p] = L
            continue
        if L and R and (L == DIR) != (R == DIR):
            problems[p] = 'a file on one computer and a folder on the other'
            continue
        if R == B or (R is None and L != B):    # changed here (or changed here and deleted there)
            if L is None:
                (rmdirs if R == DIR else deletes).append(('rmdir-there' if R == DIR else 'trash-there', p, R))
            elif L == DIR:
                mkdirs.append(('mkdir-there', p))
            else:
                moves.append(('push', p, L, R))
        elif L == B or L is None:               # changed there (or changed there and deleted here)
            if R is None:
                (rmdirs if L == DIR else deletes).append(('rmdir-here' if L == DIR else 'trash-here', p, L))
            elif R == DIR:
                mkdirs.append(('mkdir-here', p))
            else:
                moves.append(('pull', p, R, L))
        else:                                   # changed on both
            moves.append(('conflict', p, L, R))
    rmdirs.sort(key=lambda op: -op[1].count('/'))
    return renames + mkdirs + moves + deletes + rmdirs, problems, waiting

# ---------------------------------------------------------------- the connection


def make_key():
    return base64.b32encode(secrets.token_bytes(20)).decode()


def make_context(server, key):
    psk = hashlib.sha256(b'aristarkus-share-v1:' + key.encode()).digest()
    if server:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.set_psk_server_callback(lambda identity: psk)
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE     # the pre-shared key authenticates both sides
        ctx.set_psk_client_callback(lambda hint: ('aristarkus', psk))
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2    # the version with PSK key exchange in Python's API
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(CIPHERS)
    return ctx


def format_invite(ip, port, key):
    return f'{ip}:{port}/' + '-'.join(key[i:i + 4] for i in range(0, len(key), 4))


def parse_port(text):
    if not re.fullmatch(r'\d{1,5}', str(text).strip()) or not 1024 <= int(text) <= 65535:
        raise ValueError('the port has to be a number from 1024 to 65535')
    return int(text)


def parse_invite(text):
    m = re.fullmatch(r'\s*([^\s/:]+):(\d+)/([A-Za-z2-7 -]+?)\s*', text or '')
    key = re.sub(r'[ -]', '', m.group(3)).upper() if m else ''
    if len(key) != 32:
        raise ValueError("that doesn't look like a share code: copy the whole line, address included")
    return m.group(1), parse_port(m.group(2)), key


def parse_address(text):
    """"192.168.1.13:8788", "alita.local" (the port is optional)."""
    m = re.fullmatch(r'\s*([^\s/:]+)(?::(\d+))?\s*', text or '')
    if not m:
        raise ValueError("that doesn't look like an address: try 192.168.1.13:8788")
    return m.group(1), parse_port(m.group(2)) if m.group(2) else PORT


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('192.0.2.1', 9))     # picks the outgoing address; sends nothing
            return s.getsockname()[0]
        except OSError:
            return socket.gethostbyname(socket.gethostname())


_ips = (0, [])


def local_ips():
    """This computer's network addresses, the one with the default route first."""
    global _ips
    if time.monotonic() - _ips[0] < 10:     # the page asks often, and a name lookup can be slow
        return _ips[1]
    found = []

    def add(ip):
        if ip and not ip.startswith(('127.', '169.254.')) and ip not in found:
            found.append(ip)
    try:
        add(lan_ip())
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    if sys.platform.startswith('linux'):
        import fcntl
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _, name in socket.if_nameindex():
                try:
                    ifreq = struct.pack('256s', name.encode()[:15])
                    add(socket.inet_ntoa(fcntl.ioctl(s, 0x8915, ifreq)[20:24]))     # SIOCGIFADDR
                except OSError:
                    pass                    # this interface has no address
    _ips = (time.monotonic(), found)
    return found


class Conn:
    """Length-prefixed JSON messages and chunked files over one socket."""
    ABORT = 0xFFFFFFFF

    def __init__(self, sock):
        self.sock = sock
        self.reader = sock.makefile('rb')
        self.active = time.monotonic()

    def close(self):
        # shutdown first: it wakes a thread blocked reading, and closing the reader would wait for that
        for close in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.reader.close, self.sock.close):
            try:
                close()
            except (OSError, ValueError):
                pass

    def _read(self, n):
        try:
            data = self.reader.read(n)
        except (OSError, ValueError) as e:
            raise Lost(str(e)) from e
        if data is None or len(data) < n:
            raise Lost('the other computer disconnected')
        self.active = time.monotonic()
        return data

    def _write(self, data):
        try:
            self.sock.sendall(data)
            self.active = time.monotonic()
        except (OSError, ValueError) as e:
            raise Lost(str(e)) from e

    def send(self, msg):
        data = json.dumps(msg, separators=(',', ':')).encode()
        self._write(struct.pack('>I', len(data)) + data)

    def recv(self):
        n, = struct.unpack('>I', self._read(4))
        if n > MAX_FRAME:
            raise Lost('oversized message')
        try:
            return json.loads(self._read(n))
        except ValueError as e:
            raise Lost('garbled message') from e

    def send_file(self, path, progress=None):
        """Returns the hash of what was actually sent."""
        done = 0
        h = hashlib.sha256()
        try:
            with open(path, 'rb') as f:
                while chunk := f.read(CHUNK):
                    h.update(chunk)
                    self._write(struct.pack('>I', len(chunk)) + chunk)
                    done += len(chunk)
                    if progress:
                        progress(done)
        except OSError:
            self._write(struct.pack('>I', self.ABORT))
            raise
        self._write(struct.pack('>I', 0))
        return h.hexdigest()

    def chunks(self):
        while True:
            n, = struct.unpack('>I', self._read(4))
            if n == 0:
                return
            if n == self.ABORT:
                raise Changed()     # the sender couldn't finish; the next round tries again
            if n > MAX_FRAME:
                raise Lost('oversized chunk')
            yield self._read(n)

# ---------------------------------------------------------------- the service


class Share:
    def __init__(self, config_dir):
        self.config_file = os.path.join(config_dir, 'share.json')
        self.config_dir = config_dir
        self.lock = threading.Lock()
        self.gen = 0
        self.cfg = None
        self.folder = None
        self.listener = None
        self.conn = None
        self.wake = threading.Event()
        self._reset()

    def _reset(self):
        self.link, self.message, self.peer = 'off', None, None
        self.ops_left, self.waiting, self.problems = 0, 0, {}
        self.backoff = {}
        self.transfer = None
        self.synced_at = None
        self.recent = deque(maxlen=8)

    def start(self):
        cfg = read_json(self.config_file)
        if SUPPORTED and isinstance(cfg, dict) and {'role', 'folder', 'key', 'port'} <= set(cfg):
            self.cfg = cfg
            log.info('shared folder: %s (%s)', self.cfg['folder'], self.cfg['role'])
            self._launch()

    def _launch(self):
        self.gen += 1
        gen = self.gen
        self.wake.clear()
        self._reset()
        cfg = self.cfg
        self.folder = Folder(cfg['folder'], hashlib.sha256(cfg['key'].encode()).hexdigest()[:16])
        self.folder.purge_trash()
        if cfg.get('paused'):
            self.link = 'paused'
            return
        self.link = 'connecting' if cfg['role'] == 'guest' else 'waiting'
        target = self._guest if cfg['role'] == 'guest' else self._host
        threading.Thread(target=target, args=(gen,), daemon=True).start()

    def _prepare(self, path):
        """Check the folder the user chose, and mark it as shared."""
        path = os.path.realpath(os.path.expanduser((path or '').strip() or '~/Aristarkus Shared'))
        home = os.path.realpath(os.path.expanduser('~'))
        if path == home or home.startswith(path.rstrip(os.sep) + os.sep) or path == os.path.dirname(path):
            raise ValueError('choose a folder of its own, not your home folder or a whole drive')
        if SYSTEM_DIR in path.split(os.sep):
            raise ValueError(f'{SYSTEM_DIR} is where sharing keeps its own files')
        os.makedirs(path, exist_ok=True)
        marker = os.path.join(path, SYSTEM_DIR)
        os.makedirs(marker, exist_ok=True)
        if sys.platform == 'win32':
            ctypes.windll.kernel32.SetFileAttributesW(marker, 2)    # hidden
        return path

    def _save(self):
        os.makedirs(self.config_dir, exist_ok=True)
        write_json(self.config_file, self.cfg, private=True)

    def host(self, path):
        with self.lock:
            self._require_off()
            path = self._prepare(path)
            self.cfg = {'role': 'host', 'folder': path, 'key': make_key(), 'port': PORT}
            self._save()
            log.info('sharing %s', path)
            self._launch()

    def join(self, path, invite):
        with self.lock:
            self._require_off()
            ip, port, key = parse_invite(invite)
            conn, peer = self._connect(ip, port, key)    # a bad code stops here, with nothing changed
            conn.close()
            path = self._prepare(path)
            self.cfg = {'role': 'guest', 'folder': path, 'key': key, 'ip': ip, 'port': port}
            self._save()
            log.info('joined %s, sharing %s', peer, path)
            self._launch()

    def stop(self):
        """Stop sharing and forget the code. The files stay where they are."""
        with self.lock:
            folder = self._halt()
            if folder:
                folder.forget_base()    # a later pairing starts as a merge, not from this one's history
            self.cfg = None
            self._reset()
            try:
                os.remove(self.config_file)
            except OSError:
                pass

    def configure(self, address=None, port=None, paused=None):
        """Change the network settings, for example after moving to another network. The code and
        the sync history stay, so the two computers just carry on. Takes None for "no change"."""
        with self.lock:
            if not self.cfg:
                raise ValueError('not sharing')
            new = dict(self.cfg)
            if paused is not None:
                new['paused'] = paused
            if new['role'] == 'guest':
                if address is not None:
                    if '/' in address:      # a whole code line: possibly a new code too
                        new['ip'], new['port'], new['key'] = parse_invite(address)
                    else:
                        new['ip'], new['port'] = parse_address(address)
            else:
                if address is not None:
                    address = address.strip()
                    if address and not re.fullmatch(r'[^\s/:]+', address):
                        raise ValueError('type just the address or computer name, without a port')
                    new['address'] = address or None
                if port is not None and parse_port(port) != new['port']:
                    new['port'], new['fixed_port'] = parse_port(port), True
            if new == self.cfg:
                return
            self._halt()
            self.cfg = new
            self._save()
            log.info('share: network settings changed')
            self._launch()

    def shutdown(self):
        """The program is closing. Sharing picks up again the next time it opens."""
        with self.lock:
            self._halt()

    def _halt(self):
        """Stop every thread and connection, keeping the settings. Returns the closed folder."""
        self.gen += 1
        self.wake.set()
        folder, self.folder = self.folder, None
        self._close_network()
        if folder:
            folder.close()
        return folder

    def _close_network(self):
        conn, self.conn = self.conn, None
        if conn:
            conn.close()
        listener, self.listener = self.listener, None
        if listener:
            listener.close()

    def _require_off(self):
        if not SUPPORTED:
            raise ValueError(unsupported_reason())
        if self.cfg:
            raise ValueError('already sharing: stop first')

    def status(self):
        if not SUPPORTED:
            return {'supported': False, 'why': unsupported_reason()}
        out = {'supported': True, 'role': None}
        cfg, folder = self.cfg, self.folder
        if not cfg or not folder:
            return out
        problems = dict(self.problems)
        with folder.lock:
            for rel, why in folder.unreadable.items():
                problems.setdefault(rel, f"can't read it: {why}")
        out.update(role=cfg['role'], folder=cfg['folder'].replace(os.sep, '/'), link=self.link, message=self.message,
                   peer=self.peer, transfer=self.transfer, waiting=self.waiting + folder.stats['busy'],
                   left=self.ops_left, syncedAt=self.synced_at, recent=list(self.recent), **folder.stats,
                   problemCount=len(problems),
                   problems=[{'path': p, 'why': w} for p, w in sorted(problems.items())[:20]])
        out.update(paused=bool(cfg.get('paused')), port=cfg['port'])
        if cfg['role'] == 'host':
            ips = local_ips()
            out.update(address=cfg.get('address'), addresses=ips, invite=None)
            if cfg.get('address') or ips:
                out['invite'] = format_invite(cfg.get('address') or ips[0], cfg['port'], cfg['key'])
            elif not out['message'] and self.link != 'paused':
                out['message'] = "this computer isn't on a network"
        else:
            out['address'] = f"{cfg['ip']}:{cfg['port']}"
        return out

    def _note(self, direction, rel):
        self.recent.appendleft([direction, rel, time.time()])

    def _progress(self, direction, rel, size):
        self.transfer = {'dir': direction, 'path': rel, 'size': size, 'done': 0}

        def update(done):
            self.transfer = {'dir': direction, 'path': rel, 'size': size, 'done': done}
        return update

    # ---- the hosting computer: carry out what the other one asks

    def _host(self, gen):
        cfg = self.cfg
        ctx = make_context(True, cfg['key'])
        listener = socket.socket()
        if sys.platform != 'win32':     # on Windows this flag would let others take the port
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # the first time, any free port will do: the code shows it. After that the other computer
        # has it saved, so it's kept, and waited for if something else holds it for a while.
        spare = [] if cfg.get('fixed_port') else list(range(PORT, PORT + 20))
        ports = iter([cfg['port']] * 8 + spare)     # repeated: a restart may still be releasing it
        while gen == self.gen:
            port = next(ports, None)
            if port is None:
                port = cfg['port']
                self.link = 'error'
                self.message = f'port {port} is in use: waiting for it, or choose another in the network settings'
            try:
                listener.bind(('0.0.0.0', port))
                break
            except OSError:
                log.debug('port %d busy', port)
                time.sleep(0.25)
        else:
            listener.close()
            return
        if port != cfg['port'] or not cfg.get('fixed_port'):
            cfg['port'], cfg['fixed_port'] = port, True
            self._save()
        if self.link == 'error':
            self.link, self.message = 'waiting', None
        listener.listen(4)
        listener.settimeout(1)
        self.listener = listener
        threading.Thread(target=self._keep_scanning, args=(gen,), daemon=True).start()
        log.info('share: listening on port %d', port)
        while gen == self.gen:
            try:
                sock, addr = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._accept, args=(gen, ctx, sock, addr), daemon=True).start()
        listener.close()

    def _keep_scanning(self, gen):
        """Hash new files and notice a missing folder while nobody is connected."""
        while gen == self.gen:
            if not self.conn:
                try:
                    self.folder.scan()
                    if self.link == 'error':
                        self.link, self.message = 'waiting', None
                except FolderMissing as e:
                    self.link, self.message = 'error', str(e)
                except OSError as e:
                    log.debug('scan failed: %s', e)
            self.wake.wait(INTERVAL)

    def _accept(self, gen, ctx, sock, addr):
        try:
            sock.settimeout(10)
            tls = ctx.wrap_socket(sock, server_side=True)
            tls.settimeout(TIMEOUT)
        except OSError as e:    # includes ssl.SSLError: the wrong code ends here
            log.warning('share: refused a connection from %s: %s', addr[0], e)
            sock.close()
            return
        conn = Conn(tls)
        try:
            hello = conn.recv()
            if hello.get('op') != 'hello':
                raise Lost('unexpected first message')
            ok = hello.get('v') == PROTOCOL
            old = self.conn
            busy = ok and old and time.monotonic() - old.active < IDLE
            conn.send({'ok': ok and not busy, 'busy': bool(busy), 'v': PROTOCOL, 'name': socket.gethostname()})
            if not ok or busy:
                raise Lost('different protocol version' if not ok else 'another computer tried the code')
            self.conn = conn
            if old:             # silent for a while, so probably dead: a dropped network doesn't say goodbye
                old.close()
            self.peer, self.link, self.message = str(hello.get('name'))[:100], 'connected', None
            log.info('share: %s connected from %s', self.peer, addr[0])
            while gen == self.gen:
                self._serve(conn, conn.recv())
        except Lost as e:
            log.info('share: connection ended: %s', e)
        finally:
            conn.close()
            if self.conn is conn and gen == self.gen:
                self.conn, self.link, self.transfer = None, 'waiting', None

    def _serve(self, conn, m):
        op, rel, folder = m.get('op'), m.get('path'), self.folder
        try:
            if op == 'list':
                entries = folder.scan()
                if self.link == 'error':
                    self.link, self.message = 'connected', None
                conn.send({'ok': True, 'entries': entries})
            elif op == 'get':
                if folder.current(rel) != m['expect'] or m['expect'] in (None, BUSY, DIR):
                    raise Changed()
                st = os.stat(folder.path(rel))
                if st.st_size + (1 << 20) > m.get('free', st.st_size + (1 << 20)):
                    raise OSError('not enough disk space on the receiving computer')
                conn.send({'ok': True, 'size': st.st_size, 'mtime': st.st_mtime_ns, 'hash': m['expect'],
                           'mode': st.st_mode if os.name == 'posix' else None})
                try:
                    if conn.send_file(folder.path(rel), self._progress('up', rel, st.st_size)) != m['expect']:
                        folder.forget(rel)
                    self._note('up', rel)
                except OSError as e:
                    log.debug('could not send %s: %s', rel, e)
                self.transfer = None
            elif op == 'put':
                folder.check(rel, m['expect'], m['size'])
                conn.send({'ok': True})
                try:
                    folder.receive(rel, m['expect'], m, conn.chunks(), self._progress('down', rel, m['size']))
                    self._note('down', rel)
                    conn.send({'ok': True})
                finally:
                    self.transfer = None
            elif op == 'trash':
                folder.trash(rel, m['expect'])
                self._note('deleted', rel)
                conn.send({'ok': True})
            elif op == 'rename':
                folder.rename(rel, m['to'])
                conn.send({'ok': True})
            elif op == 'mkdir':
                folder.mkdir(rel)
                conn.send({'ok': True})
            elif op == 'rmdir':
                folder.rmdir(rel)
                conn.send({'ok': True})
            elif op == 'round':
                self.ops_left, self.waiting = m.get('left', 0), m.get('waiting', 0)
                self.problems = m.get('problems', {})
                if not self.ops_left and not self.problems:
                    self.synced_at = time.time()
                conn.send({'ok': True})
            else:
                conn.send({'ok': False, 'why': f'unknown request {op!r}'})
        except FolderMissing as e:
            self.link, self.message = 'error', str(e)
            conn.send({'ok': False, 'missing': True, 'why': str(e)})
        except Changed:
            conn.send({'ok': False, 'changed': True})
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning('share: %s %r failed: %s', op, rel, e)
            conn.send({'ok': False, 'why': getattr(e, 'strerror', None) or str(e)})

    # ---- the joining computer: decide what to do, and do it

    def _connect(self, ip, port, key):
        try:
            sock = socket.create_connection((ip, port), timeout=10)
        except OSError as e:
            raise ValueError(f"can't reach the other computer at {ip}:{port}. Is Aristarkus open on it, "
                             f'and are you on the same network? ({e})') from e
        try:
            tls = make_context(False, key).wrap_socket(sock)
        except ssl.SSLError as e:
            sock.close()
            raise ValueError('the other computer refused this code: it may have stopped sharing '
                             'or started again with a new code') from e
        except OSError as e:
            sock.close()
            raise ValueError(f"can't reach the other computer at {ip}:{port} ({e})") from e
        tls.settimeout(TIMEOUT)
        conn = Conn(tls)
        try:
            conn.send({'op': 'hello', 'v': PROTOCOL, 'name': socket.gethostname()})
            reply = conn.recv()
        except Lost as e:
            conn.close()
            raise ValueError(f'the other computer dropped the connection ({e})') from e
        if not reply.get('ok'):
            conn.close()
            raise ValueError('another computer is already connected with this code' if reply.get('busy') else
                             'update Aristarkus: the two computers run different versions')
        return conn, str(reply.get('name'))[:100]

    def _guest(self, gen):
        cfg = self.cfg
        said = None
        while gen == self.gen:
            conn = None
            try:
                conn, peer = self._connect(cfg['ip'], cfg['port'], cfg['key'])
                self.conn, self.peer, self.link, self.message = conn, peer, 'connected', None
                log.info('share: connected to %s', peer)
                said = None
                base = self.folder.load_base()
                while gen == self.gen:
                    busy = self._round(conn, base)
                    self.wake.wait(0.3 if busy else INTERVAL)
            except ValueError as e:
                self.link, self.message = 'offline', str(e)
            except Lost as e:
                self.link, self.message = 'offline', f'lost the connection: {e}'
            except FolderMissing as e:
                self.link, self.message = 'error', str(e)
            except OSError as e:
                self.link, self.message = 'error', f"can't read the shared folder: {e}"
            finally:
                self.transfer = None
                if conn:
                    conn.close()
            if gen == self.gen:
                if self.message != said:
                    said = self.message
                    log.info('share: %s', said)
                self.wake.wait(INTERVAL)

    def _ask(self, conn, **msg):
        conn.send(msg)
        reply = conn.recv()
        if reply.get('ok'):
            return reply
        if reply.get('changed'):
            raise Changed()
        if reply.get('missing'):
            raise FolderMissing(f"the other computer can't find its shared folder: {reply.get('why')}")
        raise Refused(reply.get('why', 'refused'))

    def _round(self, conn, base):
        """One comparison and everything it calls for. True if anything was done."""
        folder = self.folder
        local = folder.scan()
        reply = self._ask(conn, op='list')
        before = dict(base)
        ops, problems, waiting = plan(local, reply['entries'], base)
        self.waiting, self.ops_left = waiting, len(ops)
        done = 0
        for i, op in enumerate(ops):
            self.ops_left = len(ops) - i
            rel = op[1]
            held = self.backoff.get(rel)
            if held and time.monotonic() < held[0]:     # don't resend a big file every few seconds
                problems[rel] = held[1]
                continue
            try:
                self._run(conn, op, base)
                self.backoff.pop(rel, None)
                done += 1
            except Changed:
                waiting += 1
            except (OSError, ValueError) as e:
                problems[rel] = getattr(e, 'strerror', None) or str(e)
                self.backoff[rel] = (time.monotonic() + RETRY, problems[rel])
                log.debug('share: %s failed: %s', op[0], e)
            if i % 50 == 49:
                folder.save_base(base)
        self.problems, self.ops_left, self.waiting = problems, 0, waiting
        if base != before:
            folder.save_base(base)
        if not ops and not problems and not waiting:
            self.synced_at = time.time()
        shown = dict(sorted(problems.items())[:20])
        self._ask(conn, op='round', left=0 if not done else 1, waiting=waiting, problems=shown)
        return done > 0

    def _run(self, conn, op, base):
        kind, rel = op[0], op[1]
        folder = self.folder
        if kind == 'rename-here':
            folder.rename(rel, op[2])
            self._note('renamed', op[2])
            base[op[2]] = base.pop(rel, None)
        elif kind == 'rename-there':
            self._ask(conn, op='rename', path=rel, to=op[2])
            self._note('renamed', op[2])
            base[op[2]] = base.pop(rel, None)
        elif kind == 'mkdir-here':
            folder.mkdir(rel)
            base[rel] = DIR
        elif kind == 'mkdir-there':
            self._ask(conn, op='mkdir', path=rel)
            base[rel] = DIR
        elif kind == 'rmdir-here':
            folder.rmdir(rel)
            base.pop(rel, None)
        elif kind == 'rmdir-there':
            self._ask(conn, op='rmdir', path=rel)
            base.pop(rel, None)
        elif kind == 'trash-here':
            folder.trash(rel, op[2])
            self._note('deleted', rel)
            base.pop(rel, None)
        elif kind == 'trash-there':
            self._ask(conn, op='trash', path=rel, expect=op[2])
            self._note('deleted there', rel)
            base.pop(rel, None)
        elif kind == 'pull':
            self._pull(conn, rel, op[2], rel, op[3])
            base[rel] = op[2]
        elif kind == 'push':
            self._push(conn, rel, op[2], op[3])
            base[rel] = op[2]
        elif kind == 'conflict':
            mine, theirs = op[2], op[3]
            # keep their version beside mine, then mine goes to both: no version is lost
            copy = conflict_name(rel, self.peer or '', theirs)
            if folder.current(copy) != theirs:
                self._pull(conn, rel, theirs, copy, None)
            self._push(conn, rel, mine, theirs)
            base[rel] = mine

    def _pull(self, conn, rel, hash_, dest, expect):
        folder = self.folder
        folder.check(dest, expect)
        meta = self._ask(conn, op='get', path=rel, expect=hash_, free=shutil.disk_usage(folder.root).free)
        try:
            folder.receive(dest, expect, meta, conn.chunks(), self._progress('down', dest, meta['size']))
            self._note('down', dest)
        finally:
            self.transfer = None

    def _push(self, conn, rel, hash_, expect):
        folder = self.folder
        if folder.current(rel) != hash_:
            raise Changed()
        path = folder.path(rel)
        st = os.stat(path)
        self._ask(conn, op='put', path=rel, expect=expect, size=st.st_size, mtime=st.st_mtime_ns, hash=hash_,
                  mode=st.st_mode if os.name == 'posix' else None)
        failed = None
        try:
            if conn.send_file(path, self._progress('up', rel, st.st_size)) != hash_:
                folder.forget(rel)
        except OSError as e:
            failed = e              # the other side was told, and still answers
        finally:
            self.transfer = None
        reply = conn.recv()
        if failed:
            raise failed
        if reply.get('changed'):
            raise Changed()
        if not reply.get('ok'):
            raise Refused(reply.get('why', 'refused'))
        self._note('up', rel)


def load_token(config_dir):
    """The secret that separates this user's browser from anything else that can reach
    127.0.0.1, such as other accounts on this computer. Readable only by this user."""
    path = os.path.join(config_dir, 'token')
    try:
        with open(path, encoding='ascii') as f:
            token = f.read().strip()
        if token:
            return token
    except (OSError, ValueError):
        pass
    token = secrets.token_urlsafe(32)
    try:
        os.makedirs(config_dir, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='ascii') as f:
            f.write(token)
    except OSError as e:
        log.warning('cannot save the access token: %s', e)
    return token


def unsupported_reason():
    return f'sharing needs Python 3.13 or newer; this is {sys.version.split()[0]}'


def default_config_dir():
    if sys.platform == 'win32':
        return os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'Aristarkus')
    return os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'aristarkus')
