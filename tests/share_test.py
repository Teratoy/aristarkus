"""Shared folder test: two copies of the program mirror a folder over the real TLS connection.

    python tests/share_test.py aristarkus.py
    python tests/share_test.py dist/aristarkus-linux-x64 dist/aristarkus-linux-x64-debug
"""
import glob
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

import share                # noqa: E402
from smoke import stop      # noqa: E402

WINDOWS = sys.platform == 'win32'
os.environ.update(ARISTARKUS_SHARE_INTERVAL='0.3', ARISTARKUS_SHARE_SETTLE='0.5', ARISTARKUS_SHARE_RETRY='1')


def until(check, what, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if check():
                return
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.1)
    raise AssertionError(f'timed out waiting for: {what}')


def free_port():
    with socket.socket() as probe:
        probe.bind(('0.0.0.0', 0))
        return probe.getsockname()[1]


def write(root, rel, data=b'x'):
    path = os.path.join(root, *rel.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)
    # backdated so the file doesn't have to settle first, and distinct per write so edits show
    write.clock += 2
    t = time.time() - 1000 + write.clock
    os.utime(path, (t, t))


write.clock = 0


def read(root, rel):
    with open(os.path.join(root, *rel.split('/')), 'rb') as f:
        return f.read()


def exists(root, rel):
    return os.path.lexists(os.path.join(root, *rel.split('/')))


def has(root, rel, data):
    return exists(root, rel) and read(root, rel) == data


class App:
    def __init__(self, program, port, base):
        self.cmd = ([sys.executable, program] if program.endswith('.py') else [program]) + [
            '--root', base, '--port', str(port), '--no-browser', '--config', os.path.join(base, 'config')]
        self.url = f'http://127.0.0.1:{port}'
        self.base = base
        self.output = os.path.join(base, 'output.log')
        self.start()

    def start(self):
        self.log = open(self.output, 'ab')
        self.proc = subprocess.Popen(self.cmd, stdout=self.log, stderr=subprocess.STDOUT)
        until(lambda: self.status() is not None, 'the program to start', 60)

    def stop(self):
        stop(self.proc)
        self.log.close()

    def token(self):
        with open(os.path.join(self.base, 'config', 'token'), encoding='ascii') as f:
            return f.read().strip()

    def status(self):
        req = urllib.request.Request(self.url + '/api/share', headers={'X-Aristarkus-Token': self.token()})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def post(self, path, headers=None, **params):
        req = urllib.request.Request(f'{self.url}{path}?{urllib.parse.urlencode(params)}', method='POST',
                                     headers={'X-Aristarkus-Token': self.token(), **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, json.loads(body) if body.startswith(b'{') else {}


def check_folder():
    """The guards in the folder itself, some of which need timing a real run can't arrange."""
    base = tempfile.mkdtemp(prefix='aristarkus-folder-')
    root = os.path.join(base, 'root')
    system = os.path.join(root, share.SYSTEM_DIR)
    os.makedirs(system)
    folder = share.Folder(root, 'key')
    h = hashlib.sha256(b'data').hexdigest()
    meta = {'hash': h, 'mtime': 10**18, 'mode': 0o640}
    try:
        # nothing is written once the marker is gone, like an empty mountpoint after a drive drops
        folder.receive('kept.txt', None, meta, iter([b'data']))
        assert read(root, 'kept.txt') == b'data'
        if not WINDOWS:
            assert stat.S_IMODE(os.stat(os.path.join(root, 'kept.txt')).st_mode) == 0o640, 'new file lost its mode'
            os.chmod(os.path.join(root, 'kept.txt'), 0o600)
            h2 = hashlib.sha256(b'more').hexdigest()
            folder.receive('kept.txt', h, {'hash': h2, 'mtime': 10**18 + 1, 'mode': 0o644}, iter([b'more']))
            assert stat.S_IMODE(os.stat(os.path.join(root, 'kept.txt')).st_mode) == 0o600, 'an edit opened up the mode'
            os.chmod(os.path.join(root, 'kept.txt'), 0o644)
            h = h2
        moved = system + '-gone'
        os.rename(system, moved)
        for action in (folder.scan, lambda: folder.check('a', None), lambda: folder.mkdir('a'),
                       lambda: folder.rmdir('a'), lambda: folder.trash('kept.txt', h),
                       lambda: folder.rename('kept.txt', 'KEPT.txt'),
                       lambda: folder.receive('a', None, meta, iter([b'data']))):
            try:
                action()
            except share.FolderMissing:
                continue
            raise AssertionError(f'{action} ran without the marker')
        assert not os.path.exists(system), 'recreated the marker'
        assert not exists(root, 'a') and exists(root, 'kept.txt')
        os.rename(moved, system)

        # the marker vanishing mid-walk must not read as folders being removed
        os.makedirs(os.path.join(root, 'sub'))
        real_scandir = os.scandir

        def unplug(path):
            if path.endswith('sub'):
                os.rename(system, moved)
                raise FileNotFoundError(path)
            return real_scandir(path)
        os.scandir = unplug
        try:
            folder.scan()
        except share.FolderMissing:
            pass
        else:
            raise AssertionError('a partial scan was believed')
        finally:
            os.scandir = real_scandir
        os.rename(moved, system)
        shutil.rmtree(root + '/sub')

        # a folder keeps anything that never synced, and goes if only clutter is left
        write(root, 'keep/thesis.tmp', b'only copy')
        write(root, 'clutter/Thumbs.db', b'junk')
        folder.rmdir('keep')
        folder.rmdir('clutter')
        assert read(root, 'keep/thesis.tmp') == b'only copy', 'deleted a file that never synced'
        assert not exists(root, 'clutter')
    finally:
        folder.close()
        shutil.rmtree(base, ignore_errors=True)


def check_plan():
    h1, h2, h3 = 'a' * 64, 'b' * 64, 'c' * 64

    def kinds(local, remote, base):
        return [op[0] for op in share.plan(local, remote, dict(base))[0]]

    assert kinds({'f': h1}, {}, {}) == ['push']
    assert kinds({}, {'f': h1}, {}) == ['pull']
    assert kinds({'f': h1}, {'f': h1}, {}) == []
    assert kinds({'f': h2}, {'f': h1}, {'f': h1}) == ['push']
    assert kinds({'f': h1}, {'f': h2}, {'f': h1}) == ['pull']
    assert kinds({}, {'f': h1}, {'f': h1}) == ['trash-there']
    assert kinds({'f': h1}, {}, {'f': h1}) == ['trash-here']
    assert kinds({'f': h2}, {'f': h3}, {'f': h1}) == ['conflict']
    assert kinds({'f': h2}, {'f': h3}, {}) == ['conflict']
    assert kinds({}, {'f': h2}, {'f': h1}) == ['pull']              # changed there beats deleted here
    assert kinds({'f': h2}, {}, {'f': h1}) == ['push']              # changed here beats deleted there
    assert kinds({'f': share.BUSY}, {'f': h1}, {'f': h1}) == []     # still being written: wait
    assert kinds({}, {}, {'f': h1}) == []
    assert kinds({'d': share.DIR, 'd/f': h1}, {}, {}) == ['mkdir-there', 'push']
    ops = kinds({}, {'a': share.DIR, 'a/b': share.DIR, 'a/b/f': h1},
                {'a': share.DIR, 'a/b': share.DIR, 'a/b/f': h1})
    assert ops == ['trash-there', 'rmdir-there', 'rmdir-there'], ops    # deepest folder first

    ops, problems, _ = share.plan({'f': h1}, {'f': share.DIR}, {})
    assert not ops and 'f' in problems
    ops, problems, _ = share.plan({'a?.txt': h1}, {}, {})
    assert not ops and 'a?.txt' in problems
    ops, problems, _ = share.plan({'Same': h1, 'same': h2}, {}, {})
    assert not ops and len(problems) == 2
    ops, problems, _ = share.plan({'D': share.DIR, 'D/f': h1}, {'d': share.DIR}, {})
    assert not ops and 'D/f' in problems

    # a change of capitals is a rename, including everything below a renamed folder
    assert kinds({'New.txt': h1}, {'new.txt': h1}, {'new.txt': h1}) == ['rename-there']
    assert kinds({'new.txt': h1}, {'New.txt': h1}, {'new.txt': h1}) == ['rename-here']
    ops, problems, waiting = share.plan({'Docs': share.DIR, 'Docs/f': h1}, {'docs': share.DIR, 'docs/f': h1},
                                        {'docs': share.DIR, 'docs/f': h1})
    assert ops == [('rename-there', 'docs', 'Docs')] and not problems and waiting, (ops, problems)
    base = {'docs': share.DIR}
    assert share.plan({'Docs': share.DIR}, {'Docs': share.DIR}, base)[:2] == ([], {}), 'a leftover name clashed'
    assert base == {'Docs': share.DIR}

    for bad in ('CON', 'a/nul.txt', 'trailing.', 'trailing ', 'a:b', '../x', 'a//b', '', 'a\\b', 'com1.log'):
        assert share.name_problem(bad), bad
    for good in ('a.txt', 'dir/ñandú ☃.png', '.hidden', 'console.txt'):
        assert not share.name_problem(good), good
    assert share.parse_invite(share.format_invite('10.0.0.2', 8788, 'A' * 32)) == ('10.0.0.2', 8788, 'A' * 32)
    assert share.parse_address(' alita.local ') == ('alita.local', share.PORT)
    assert share.parse_address('10.0.0.2:9000') == ('10.0.0.2', 9000)
    for bad in ('', 'hello', '10.0.0.2:8788', '10.0.0.2:8788/SHORT', '10.0.0.2/' + 'A' * 32):
        try:
            share.parse_invite(bad)
        except ValueError:
            continue
        raise AssertionError(f'accepted the invite {bad!r}')
    assert share.conflict_name('d/a.tar.gz', 'P<C', h1) == f'd/a.tar (conflict from PC {h1[:8]}).gz'
    assert share.conflict_name('.bashrc', 'PC', h1) == f'.bashrc (conflict from PC {h1[:8]})'


def check_program(program, port):
    base = tempfile.mkdtemp(prefix='aristarkus-share-')
    base_a, base_b = os.path.join(base, 'a'), os.path.join(base, 'b')
    os.makedirs(base_a)
    os.makedirs(base_b)
    folder_a, folder_b = os.path.join(base_a, 'shared'), os.path.join(base_b, 'shared')
    a = App(program, port, base_a)
    b = App(program, port + 1, base_b)
    try:
        run_scenario(a, b, folder_a, folder_b, base)
        print(f'ok   {program}')
    except Exception:
        for app in (a, b):
            stop(app.proc)
            app.log.close() if not app.log.closed else None
            print(f'--- {app.url} output ---', file=sys.stderr)
            print(open(app.output, encoding='utf-8', errors='replace').read()[-6000:], file=sys.stderr)
        raise
    finally:
        for app in (a, b):
            stop(app.proc)
            if not app.log.closed:
                app.log.close()
        shutil.rmtree(base, ignore_errors=True)


def run_scenario(a, b, folder_a, folder_b, base):
    assert a.status() == {'supported': True, 'role': None}

    # without the token, other accounts on this computer get nothing
    req = urllib.request.Request(a.url + '/api/share')
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.read()) == {'locked': True}
    code, reply = a.post('/api/share/host', headers={'X-Aristarkus-Token': 'wrong'}, folder=folder_a)
    assert code == 403 and not os.path.exists(folder_a), reply
    if not WINDOWS:
        assert stat.S_IMODE(os.stat(os.path.join(a.base, 'config', 'token')).st_mode) == 0o600

    # guards before anything is shared
    code, reply = a.post('/api/share/host', folder=os.path.expanduser('~'))
    assert code == 400 and 'home' in reply['error'], reply
    code, _ = a.post('/api/share/host', headers={'Origin': 'https://evil.example'}, folder=folder_a)
    assert code == 403, 'a website could start sharing'
    assert not os.path.exists(folder_a)

    # each side starts with its own files, and joining merges them
    write(folder_a, 'from-a.txt', b'made on a')
    write(folder_b, 'from-b.txt', b'made on b')
    write(folder_a, 'same.txt', b'identical')
    write(folder_b, 'same.txt', b'identical')
    code, reply = a.post('/api/share/host', folder=folder_a)
    assert code == 200 and reply['role'] == 'host', reply
    port = int(reply['invite'].split('/')[0].split(':')[1])
    invite = f'127.0.0.1:{port}/' + reply['invite'].split('/')[1]
    code, _ = a.post('/api/share/host', folder=folder_a)
    assert code == 400, 'hosted twice'

    # a wrong code gets nothing and changes nothing
    wrong_dir = os.path.join(b.base, 'wrong')
    code, reply = b.post('/api/share/join', folder=wrong_dir, invite=f'127.0.0.1:{port}/' + 'A' * 32)
    assert code == 400 and 'refused' in reply['error'], reply
    assert not os.path.exists(wrong_dir) and b.status()['role'] is None
    code, reply = b.post('/api/share/join', folder=wrong_dir, invite=f'127.0.0.1:{free_port()}/' + 'A' * 32)
    assert code == 400 and 'reach' in reply['error'], reply
    code, reply = b.post('/api/share/join', folder=wrong_dir, invite='garbage')
    assert code == 400 and 'code' in reply['error'], reply

    code, reply = b.post('/api/share/join', folder=folder_b, invite=invite)
    assert code == 200 and reply['role'] == 'guest', reply
    until(lambda: has(folder_b, 'from-a.txt', b'made on a'), 'a to b on joining')
    until(lambda: has(folder_a, 'from-b.txt', b'made on b'), 'b to a on joining')
    until(lambda: a.status()['link'] == 'connected', 'host to see the connection')
    until(lambda: b.status()['syncedAt'], 'the guest to report up to date')
    assert not [p for p in glob.glob(folder_a + '/*') if 'conflict' in p], 'identical files conflicted'

    # another computer with the code can't push the connected one off
    ip, code_port, key = share.parse_invite(invite)
    tls = share.make_context(False, key).wrap_socket(socket.create_connection((ip, code_port), timeout=10))
    second = share.Conn(tls)
    second.send({'op': 'hello', 'v': share.PROTOCOL, 'name': 'second'})
    reply = second.recv()
    assert not reply['ok'] and reply['busy'], reply
    second.close()
    time.sleep(1)
    assert b.status()['link'] == 'connected' and a.status()['peer'] != 'second'

    # both directions: new files, nested folders, empty folders, odd names, unchanged times
    write(folder_a, 'deep/er/ñandú ☃.txt', b'unicode')
    until(lambda: has(folder_b, 'deep/er/ñandú ☃.txt', b'unicode'), 'nested unicode a to b')
    assert (os.stat(os.path.join(folder_a, 'deep', 'er', 'ñandú ☃.txt')).st_mtime_ns // 10**7 ==
            os.stat(os.path.join(folder_b, 'deep', 'er', 'ñandú ☃.txt')).st_mtime_ns // 10**7), 'time not kept'
    os.makedirs(os.path.join(folder_b, 'empty', 'inner'))
    until(lambda: os.path.isdir(os.path.join(folder_a, 'empty', 'inner')), 'empty folder b to a')
    write(folder_b, 'from-b.txt', b'edited on b')
    until(lambda: has(folder_a, 'from-b.txt', b'edited on b'), 'edit b to a')
    write(folder_a, 'from-b.txt', b'edited on a')
    until(lambda: has(folder_b, 'from-b.txt', b'edited on a'), 'edit a to b')

    # a big file arrives whole, and a long path works
    big = os.urandom(1 << 16) * 512     # 32 MB
    write(folder_a, 'big.bin', big)
    until(lambda: exists(folder_b, 'big.bin') and
          hashlib.sha256(read(folder_b, 'big.bin')).digest() == hashlib.sha256(big).digest(), 'big file', 90)
    long_rel = '/'.join(['long-folder-name-' + str(i).zfill(3) * 10 for i in range(7)]) + '/end.txt'
    assert len(long_rel) > 260
    path = os.path.join(folder_b, *long_rel.split('/'))
    if WINDOWS:
        path = '\\\\?\\' + os.path.abspath(path)
    os.makedirs(os.path.dirname(path))
    with open(path, 'wb') as f:
        f.write(b'long')
    os.utime(path, (time.time() - 100, time.time() - 100))
    until(lambda: read_long(folder_a, long_rel) == b'long', 'long path b to a')

    # a change of capitals arrives as a rename, for a file and for a folder with files in it
    write(folder_a, 'caps.txt', b'caps')
    write(folder_a, 'Folder/inside.txt', b'inside')
    until(lambda: has(folder_b, 'Folder/inside.txt', b'inside') and exists(folder_b, 'caps.txt'), 'files to rename')
    until(lambda: b.status()['syncedAt'], 'sync')
    time.sleep(1)
    os.rename(os.path.join(folder_a, 'caps.txt'), os.path.join(folder_a, 'CAPS.txt'))
    os.rename(os.path.join(folder_b, 'Folder'), os.path.join(folder_b, 'folder'))

    def renamed(folder):
        names = set(os.listdir(folder))
        return 'CAPS.txt' in names and 'caps.txt' not in names and 'folder' in names and 'Folder' not in names
    until(lambda: renamed(folder_a) and renamed(folder_b), 'renames on both computers')
    assert has(folder_a, 'folder/inside.txt', b'inside') and has(folder_b, 'CAPS.txt', b'caps')
    until(lambda: not b.status()['problems'] and not a.status()['problems'], 'no problems after renaming')
    assert not glob.glob(os.path.join(folder_a, '.aristarkus', 'trash', '*', 'caps.txt'))

    # a file that can't be replaced is reported and retried, and goes through once it's free
    write(folder_a, 'stuck.txt', b'first')
    until(lambda: has(folder_b, 'stuck.txt', b'first'), 'file to get stuck')
    until(lambda: b.status()['syncedAt'], 'sync')
    time.sleep(1)
    stuck = os.path.join(folder_b, 'stuck.txt')
    os.chmod(stuck, stat.S_IREAD)
    write(folder_a, 'stuck.txt', b'second')
    until(lambda: 'stuck.txt' in {p['path'] for p in b.status()['problems']}, 'the stuck file to be reported')
    assert read(folder_b, 'stuck.txt') == b'first'
    os.chmod(stuck, stat.S_IREAD | stat.S_IWRITE)
    until(lambda: has(folder_b, 'stuck.txt', b'second'), 'the file once it was free')

    # a folder that still holds an unsynced file isn't removed with it
    write(folder_a, 'work/a.txt', b'a')
    write(folder_a, 'work/thesis.tmp', b'only copy')
    until(lambda: exists(folder_b, 'work/a.txt'), 'folder to delete')
    until(lambda: b.status()['syncedAt'], 'sync')
    time.sleep(1)
    shutil.rmtree(os.path.join(folder_b, 'work'))
    until(lambda: not exists(folder_a, 'work/a.txt'), 'the delete to arrive')
    time.sleep(1)
    assert has(folder_a, 'work/thesis.tmp', b'only copy'), 'deleted a file that never synced'
    os.remove(os.path.join(folder_a, 'work', 'thesis.tmp'))

    # a deleted file reaches the other side's trash, not oblivion
    write(folder_a, 'doomed.txt', b'keep me somewhere')
    until(lambda: exists(folder_b, 'doomed.txt'), 'file to delete')
    until(lambda: b.status()['syncedAt'], 'sync')
    time.sleep(1)
    os.remove(os.path.join(folder_b, 'doomed.txt'))
    until(lambda: not exists(folder_a, 'doomed.txt'), 'delete b to a')
    trashed = glob.glob(os.path.join(folder_a, '.aristarkus', 'trash', '*', 'doomed.txt'))
    assert trashed and open(trashed[0], 'rb').read() == b'keep me somewhere', trashed
    shutil.rmtree(os.path.join(folder_a, 'empty'))
    until(lambda: not exists(folder_b, 'empty'), 'folder delete a to b')

    # changed on both sides: both versions survive on both computers
    write(folder_a, 'both.txt', b'original')
    until(lambda: has(folder_b, 'both.txt', b'original'), 'file to edit')
    time.sleep(1.5)
    write(folder_a, 'both.txt', b'version a')
    write(folder_b, 'both.txt', b'version b')

    def both_kept(folder):
        contents = {read(folder, os.path.relpath(p, folder).replace(os.sep, '/'))
                    for p in glob.glob(os.path.join(folder, 'both*.txt'))}
        return contents == {b'version a', b'version b'}
    until(lambda: both_kept(folder_a) and both_kept(folder_b), 'both versions on both computers')
    assert read(folder_a, 'both.txt') == read(folder_b, 'both.txt'), 'the main copies differ'
    assert len(glob.glob(os.path.join(folder_a, 'both*.txt'))) == 2

    # deleted on one side, edited on the other: the edit wins
    write(folder_a, 'argued.txt', b'first')
    until(lambda: has(folder_b, 'argued.txt', b'first'), 'file to argue about')
    time.sleep(1.5)
    os.remove(os.path.join(folder_a, 'argued.txt'))
    write(folder_b, 'argued.txt', b'edited')
    until(lambda: has(folder_a, 'argued.txt', b'edited'), 'the edit to come back')

    # a file still being written doesn't travel until it's finished
    path = os.path.join(folder_a, 'growing.bin')
    with open(path, 'wb') as f:
        end = time.time() + 2.5
        while time.time() < end:
            f.write(b'x' * 1000)
            f.flush()
            os.utime(path)
            time.sleep(0.05)
            assert not exists(folder_b, 'growing.bin'), 'sent a file that was still being written'
    size = os.path.getsize(path)
    until(lambda: exists(folder_b, 'growing.bin') and os.path.getsize(os.path.join(folder_b, 'growing.bin')) == size,
          'the finished file')

    # ignored files stay local
    write(folder_a, 'scratch.tmp', b'temp')
    write(folder_a, 'after-tmp.txt', b'marker')
    until(lambda: exists(folder_b, 'after-tmp.txt'), 'marker file')
    assert not exists(folder_b, 'scratch.tmp')

    if not WINDOWS:     # names only Linux can make: reported, never sent
        write(folder_a, 'what?.txt', b'q')
        write(folder_a, 'Clash.txt', b'1')
        write(folder_a, 'clash.txt', b'2')

        def reported():
            paths = {p['path'] for p in b.status()['problems']}
            return {'what?.txt', 'Clash.txt', 'clash.txt'} <= paths
        until(reported, 'bad names to be reported')
        assert a.status()['problemCount'] >= 3, 'the host should show them too'
        assert not exists(folder_b, 'what?.txt') and not exists(folder_b, 'clash.txt')
        for name in ('what?.txt', 'Clash.txt', 'clash.txt'):
            os.remove(os.path.join(folder_a, name))
        until(lambda: not b.status()['problems'], 'problems to clear')

    # a missing folder stops the sync: it must not read as "everything was deleted"
    write(folder_a, 'precious.txt', b'precious')
    until(lambda: has(folder_b, 'precious.txt', b'precious'), 'file to protect')
    until(lambda: b.status()['syncedAt'], 'sync')
    moved = folder_a + '-unplugged'
    a.stop()            # Windows won't move a folder that's being watched
    os.rename(folder_a, moved)
    a.start()
    until(lambda: a.status()['link'] == 'error', 'host to notice its folder is gone')
    until(lambda: b.status()['link'] in ('error', 'offline'), 'guest to notice', 40)
    time.sleep(2)
    assert has(folder_b, 'precious.txt', b'precious'), 'a missing folder deleted files'
    assert not os.path.exists(folder_a), 'recreated the missing folder'
    a.stop()
    os.rename(moved, folder_a)
    a.start()
    until(lambda: a.status()['link'] == 'connected', 'the folder coming back')
    write(folder_b, 'after-return.txt', b'back')
    until(lambda: has(folder_a, 'after-return.txt', b'back'), 'sync after the folder returned')
    assert has(folder_a, 'precious.txt', b'precious')

    # changes while one computer is off arrive after a restart, deletes included, with no new code
    write(folder_a, 'gone-offline.txt', b'bye')
    until(lambda: exists(folder_b, 'gone-offline.txt'), 'file before going offline')
    until(lambda: b.status()['syncedAt'], 'sync')
    time.sleep(1)
    b.stop()
    until(lambda: a.status()['link'] == 'waiting', 'host to notice the guest left')
    os.remove(os.path.join(folder_a, 'gone-offline.txt'))
    write(folder_a, 'while-offline.txt', b'hello later')
    b.start()
    until(lambda: has(folder_b, 'while-offline.txt', b'hello later'), 'file added while offline')
    until(lambda: not exists(folder_b, 'gone-offline.txt'), 'delete made while offline')
    assert glob.glob(os.path.join(folder_b, '.aristarkus', 'trash', '*', 'gone-offline.txt'))

    # moving to another network: the settings change the address, with no new code and no merge
    nowhere = f'127.0.0.1:{free_port()}'
    code, reply = b.post('/api/share/configure', address=nowhere)
    assert code == 200 and reply['address'] == nowhere, reply
    until(lambda: b.status()['link'] == 'offline', 'guest to lose the host at the old address')
    assert 'reach' in b.status()['message']
    write(folder_a, 'moved.txt', b'after the move')
    time.sleep(1.5)
    assert not exists(folder_b, 'moved.txt')
    for app, params in ((b, {'address': 'not an address'}), (b, {'address': '10.0.0.2:99999'}), (b, {'address': 'x/y'}),
                        (a, {'address': 'with:port'}), (a, {'port': 'abc'}), (a, {'port': '80'}), (a, {'port': '70000'})):
        code, reply = app.post('/api/share/configure', **params)
        assert code == 400 and reply['error'], params
    code, reply = b.post('/api/share/configure', address=f'127.0.0.1:{port}')
    assert code == 200, reply
    until(lambda: has(folder_b, 'moved.txt', b'after the move'), 'sync after updating the address')
    conflicts = len(glob.glob(os.path.join(folder_b, '*conflict*')))
    assert conflicts == 1, 'moving networks should not make conflicts'

    # the host changes port: its code follows, a busy port is refused out loud, and it survives a restart
    with socket.socket() as probe:
        probe.bind(('0.0.0.0', 0))
        busy = probe.getsockname()[1]
        probe.listen(1)
        code, reply = a.post('/api/share/configure', port=busy)
        assert code == 200, reply
        until(lambda: a.status()['link'] == 'error', 'the busy port to be reported', 15)
        assert str(busy) in a.status()['message']
    port = free_port()
    code, reply = a.post('/api/share/configure', port=port)
    assert code == 200 and f':{port}/' in reply['invite'], reply
    invite = f'127.0.0.1:{port}/' + reply['invite'].split('/')[1]
    until(lambda: b.status()['link'] == 'offline', 'guest to lose the old port')
    code, reply = b.post('/api/share/configure', address=invite)    # the whole code line works too
    assert code == 200 and reply['address'] == f'127.0.0.1:{port}', reply
    until(lambda: a.status()['link'] == 'connected', 'guest on the new port')
    a.stop()
    a.start()
    until(lambda: a.status()['link'] == 'connected', 'the new port after a restart')
    assert f':{port}/' in a.status()['invite']

    # the address in the code can be set by hand and cleared again
    code, reply = a.post('/api/share/configure', address='alita.local')
    assert code == 200 and reply['invite'].startswith(f'alita.local:{port}/'), reply
    code, reply = a.post('/api/share/configure', address='')
    assert code == 200 and not reply['address'] and not reply['invite'].startswith('alita'), reply
    until(lambda: a.status()['link'] == 'connected', 'guest after the host restarted its listener')

    # a paired port is kept: if something holds it at startup the host waits, rather than move
    a.stop()
    with socket.socket() as probe:
        if not WINDOWS:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('0.0.0.0', port))
        probe.listen(1)
        a.start()
        until(lambda: a.status()['link'] == 'error', 'the taken port to be reported', 15)
        assert f':{port}/' in a.status()['invite'], 'moved to another port'
    until(lambda: a.status()['link'] == 'connected', 'the host once its port was free')

    # paused: nothing moves in either direction, until it resumes, and it stays paused across a restart
    code, reply = b.post('/api/share/configure', paused='1')
    assert code == 200 and reply['paused'] and reply['link'] == 'paused', reply
    # a change hashing can't see, while the guest is away: same size, same time
    write(folder_a, 'stale.txt', b'old!')
    until(lambda: a.status()['files'] and not a.status()['waiting'], 'the host to hash it')
    before = os.stat(os.path.join(folder_a, 'stale.txt'))
    with open(os.path.join(folder_a, 'stale.txt'), 'wb') as f:
        f.write(b'new!')
    os.utime(os.path.join(folder_a, 'stale.txt'), ns=(before.st_atime_ns, before.st_mtime_ns))
    write(folder_a, 'paused-a.txt', b'wait')
    write(folder_b, 'paused-b.txt', b'wait')
    time.sleep(1.5)
    assert not exists(folder_b, 'paused-a.txt') and not exists(folder_a, 'paused-b.txt')
    b.stop()
    b.start()
    assert b.status()['link'] == 'paused'
    code, reply = b.post('/api/share/configure', paused='0')
    assert code == 200 and not reply['paused'], reply
    until(lambda: exists(folder_b, 'paused-a.txt') and exists(folder_a, 'paused-b.txt'), 'sync after resuming')
    until(lambda: has(folder_b, 'stale.txt', b'new!'), 'a stale hash to correct itself')
    code, reply = a.post('/api/share/configure', paused='1')
    assert code == 200 and reply['link'] == 'paused', reply
    until(lambda: b.status()['link'] == 'offline', 'guest to see the host pause')

    def closed():
        with socket.socket() as probe:
            return probe.connect_ex(('127.0.0.1', port)) != 0
    until(closed, 'a paused host to stop listening', 5)
    code, reply = a.post('/api/share/configure', paused='0')
    until(lambda: a.status()['link'] == 'connected', 'guest after the host resumed')
    assert f':{port}/' in a.status()['invite']

    # someone holding the code still can't leave the folder
    b.post('/api/share/stop')
    until(lambda: a.status()['link'] == 'waiting', 'guest to leave')
    check_paths(a, folder_a, invite, base)

    # stopping forgets the code and leaves the files
    a.post('/api/share/stop')
    assert a.status()['role'] is None and b.status()['role'] is None
    assert has(folder_a, 'precious.txt', b'precious') and has(folder_b, 'precious.txt', b'precious')
    assert not os.path.exists(os.path.join(a.base, 'config', 'share.json'))
    a.stop()
    a.start()
    assert a.status()['role'] is None


def read_long(folder, rel):
    path = os.path.join(folder, *rel.split('/'))
    if WINDOWS:
        path = '\\\\?\\' + os.path.abspath(path)
    with open(path, 'rb') as f:
        return f.read()


def check_paths(a, folder, invite, base):
    """Ask the host directly for things a real client never would."""
    ip, port, key = share.parse_invite(invite)

    # without TLS the host says nothing, least of all what's in the folder
    plain = socket.create_connection((ip, port), timeout=10)
    plain.sendall(b'\x00\x00\x00\x0d{"op":"list"}')
    try:
        leaked = plain.recv(65536)
    except OSError:
        leaked = b''
    plain.close()
    assert b'.txt' not in leaked and b'entries' not in leaked, leaked

    tls = share.make_context(False, key).wrap_socket(socket.create_connection((ip, port), timeout=10))
    assert tls.version() == 'TLSv1.2' and 'ECDHE-PSK' in tls.cipher()[0], tls.cipher()
    conn = share.Conn(tls)
    conn.send({'op': 'hello', 'v': share.PROTOCOL, 'name': 'attacker'})
    assert conn.recv()['ok']
    outside = os.path.join(base, 'outside')
    os.makedirs(outside)
    bad = ['../escaped.txt', 'a/../../escaped.txt', '/etc/escaped.txt', 'C:/escaped.txt', 'a\\..\\..\\escaped.txt',
           '.aristarkus/index.json', '.ARISTARKUS/x', '', None, 7]
    if not WINDOWS:
        os.symlink(outside, os.path.join(folder, 'link'))
        bad.append('link/escaped.txt')
    for path in bad:
        conn.send({'op': 'put', 'path': path, 'expect': None, 'size': 1, 'mtime': 0, 'hash': 'x' * 64})
        reply = conn.recv()
        assert not reply.get('ok'), f'put accepted {path!r}'
        conn.send({'op': 'trash', 'path': path, 'expect': None})
        assert not conn.recv().get('ok'), f'trash accepted {path!r}'
        conn.send({'op': 'get', 'path': path, 'expect': 'x' * 64})
        assert not conn.recv().get('ok'), f'get accepted {path!r}'
    assert not os.listdir(outside)
    assert not os.path.exists(os.path.join(os.path.dirname(folder), 'escaped.txt'))

    # a file too big for the receiver isn't sent at all
    entries = (conn.send({'op': 'list'}), conn.recv())[1]['entries']
    conn.send({'op': 'get', 'path': 'precious.txt', 'expect': entries['precious.txt'], 'free': 0})
    reply = conn.recv()
    assert not reply['ok'] and 'space' in reply['why'], reply

    # a corrupted upload is thrown away and the connection survives
    conn.send({'op': 'put', 'path': 'corrupt.txt', 'expect': None, 'size': 5, 'mtime': 0, 'hash': 'x' * 64})
    assert conn.recv()['ok']
    conn._write(b'\x00\x00\x00\x05hello\x00\x00\x00\x00')
    assert conn.recv().get('changed')
    assert not exists(folder, 'corrupt.txt')
    assert not os.listdir(os.path.join(folder, '.aristarkus', 'tmp'))
    conn.send({'op': 'nonsense'})
    assert not conn.recv()['ok']
    conn.close()
    if not WINDOWS:
        os.remove(os.path.join(folder, 'link'))


if __name__ == '__main__':
    if not share.SUPPORTED:
        raise SystemExit(share.unsupported_reason())
    check_plan()
    check_folder()
    print('ok   plan and folder guards')
    for i, program in enumerate(sys.argv[1:]):
        check_program(program, 18800 + i * 10)
