"""Smoke test: start each given program on a small temp folder, then check the scan,
the tree, the page, and that live events arrive when files change.

    python tests/smoke.py aristarkus.py
    python tests/smoke.py dist/aristarkus-linux-x64 dist/aristarkus-linux-x64-debug
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

FILES = {'a.txt': 1000, 'sub/b.png': 2000, 'sub/deeper/c.mp3': 3000}


def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def listen(url, out):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            for raw in r:
                line = raw.decode().strip()
                if line.startswith('data: '):
                    for e in json.loads(line[6:])['e']:
                        out.put(e)
    except Exception as e:     # the stream ends when the program is stopped
        out.put(['error', str(e), 0])


def stop(proc):
    """Stop the program and everything it started: a onefile .exe is a launcher that runs
    the app as a child, which a plain terminate() on Windows leaves running."""
    if proc.poll() is not None:
        return
    if sys.platform == 'win32':
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def check(program, port):
    tmp = tempfile.mkdtemp(prefix='aristarkus-smoke-')
    for rel, size in FILES.items():
        os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
        with open(os.path.join(tmp, rel), 'wb') as f:
            f.write(b'x' * size)
    cmd = [sys.executable, program] if program.endswith('.py') else [program]
    config = tempfile.mkdtemp(prefix='aristarkus-smoke-config-')    # outside the folder being counted
    record = os.path.join(config, 'opened.txt')
    env = {**os.environ, 'ARISTARKUS_OPEN_RECORD': record}
    proc = subprocess.Popen(cmd + ['--root', tmp, '--port', str(port), '--no-browser', '--config', config],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    base = f'http://127.0.0.1:{port}'
    try:
        deadline = time.time() + 60          # onefile builds unpack themselves first
        while True:
            try:
                st = json.loads(get(base + '/api/status', 2))
                if not st['scanning']:
                    break
            except OSError:
                pass
            if time.time() > deadline or proc.poll() is not None:
                raise AssertionError('server did not finish scanning')
            time.sleep(0.3)
        assert st['files'] == len(FILES), st
        assert st['bytes'] == sum(FILES.values()), st
        assert st['watches'] >= 3, st

        tree = json.loads(get(base + '/api/tree?budget=100&path=' + urllib.request.quote(st['root'])))
        names = {c['n'] for c in tree['c']}
        assert {'a.txt', 'sub'} <= names, names
        assert b'Aristarkus' in get(base + '/')
        assert len(get(base + '/d3.min.js')) > 100000

        events = queue.Queue()
        threading.Thread(target=listen, args=(base + '/api/events', events), daemon=True).start()
        time.sleep(1.5)
        with open(os.path.join(tmp, 'sub', 'new.txt'), 'w') as f:
            f.write('hello')
        with open(os.path.join(tmp, 'a.txt'), 'ab') as f:
            f.write(b'more')
        with open(os.path.join(tmp, 'sub', 'b.png'), 'rb') as f:
            f.read()

        want = {('c', 'sub/new.txt'), ('w', 'a.txt')}
        if st['reads']:
            want.add(('r', 'sub/b.png'))
        seen = set()
        end = time.time() + 10
        while want - seen and time.time() < end:
            try:
                kind, path = events.get(timeout=0.5)[:2]
            except queue.Empty:
                continue
            assert kind != 'error', path
            for k, rel in want:
                if kind == k and path.endswith('/' + rel):
                    seen.add((k, rel))
        assert not want - seen, f'missing events: {want - seen}'

        def post(path):
            req = urllib.request.Request(base + path, method='POST')
            return json.loads(urllib.request.urlopen(req, timeout=10).read())

        post('/api/log?message=' + urllib.request.quote('smoke test'))
        sub = st['root'] + '/sub'
        assert post('/api/root?path=' + urllib.request.quote(sub))['root'] == sub
        for _ in range(100):
            st2 = json.loads(get(base + '/api/status', 2))
            if not st2['scanning']:
                break
            time.sleep(0.1)
        assert st2['root'] == sub and st2['files'] == 3, st2     # b.png, c.mp3, new.txt

        with open(os.path.join(config, 'token'), encoding='ascii') as f:
            token = f.read().strip()

        def api(path, method='GET', token=token):
            req = urllib.request.Request(base + path, method=method, headers={'X-Aristarkus-Token': token})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b'{}')

        # search, from the folder being looked at
        found = api('/api/search?q=PNG&path=' + urllib.request.quote(sub))[1]
        assert [r['p'] for r in found['results']] == [sub + '/b.png'], found
        assert api('/api/search?q=png', token='wrong')[0] == 403

        # open and show in the file manager: recorded instead of done, and programs are never opened
        assert api('/api/open?path=' + urllib.request.quote(sub + '/b.png'), 'POST')[0] == 200
        assert api('/api/open?reveal=1&path=' + urllib.request.quote(sub + '/deeper'), 'POST')[0] == 200
        for name in ('run.sh', 'setup.exe', 'evil.desktop', 'script.ps1'):
            with open(os.path.join(tmp, 'sub', name), 'w') as f:
                f.write('x')
            code, reply = api('/api/open?path=' + urllib.request.quote(sub + '/' + name), 'POST')
            assert code == 400 and 'program' in reply['error'], (name, reply)
            assert api('/api/open?reveal=1&path=' + urllib.request.quote(sub + '/' + name), 'POST')[0] == 200
        assert api('/api/open?path=' + urllib.request.quote(sub + '/missing.txt'), 'POST')[0] == 400
        assert api('/api/open?path=' + urllib.request.quote(sub + '/b.png'), 'POST', token='wrong')[0] == 403
        with open(record, encoding='utf-8') as f:
            done = [line.split('\t') for line in f.read().splitlines()]
        assert [d[0] for d in done] == ['open', 'reveal'] + ['reveal'] * 4, done
        assert done[0][1].replace(os.sep, '/').endswith('sub/b.png'), done

        # which programs have a file open: this test, holding one
        with open(os.path.join(tmp, 'sub', 'b.png'), 'rb'):
            code, who = api('/api/who?path=' + urllib.request.quote(sub + '/b.png'))
        assert code == 200, who
        if who['supported']:
            assert os.getpid() in [p['pid'] for p in who['programs']], who
        if st['reads']:     # Linux names the program in the activity log too
            events = queue.Queue()
            threading.Thread(target=listen, args=(base + '/api/events', events), daemon=True).start()
            time.sleep(1)
            named = False
            with open(os.path.join(tmp, 'sub', 'held.txt'), 'w') as f:
                end = time.time() + 6
                while time.time() < end and not named:
                    f.write('x')
                    f.flush()
                    time.sleep(0.2)
                    while not events.empty():
                        e = events.get()
                        named = named or (e[1].endswith('/held.txt') and len(e) > 3 and 'python' in e[3])
            assert named, 'the activity log never named the program'

        # the stop button: only this user's browser can press it, and the program really ends
        req = urllib.request.Request(base + '/api/quit', method='POST')
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError('stopped without the token')
        except urllib.error.HTTPError as e:
            assert e.code == 403, e
        req = urllib.request.Request(base + '/api/quit', method='POST', headers={
            'X-Aristarkus-Token': token, 'Origin': 'https://evil.example', 'Host': f'127.0.0.1:{port}'})
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError('stopped from another website')
        except urllib.error.HTTPError as e:
            assert e.code == 403, e
        assert proc.poll() is None
        req = urllib.request.Request(base + '/api/quit', method='POST', headers={'X-Aristarkus-Token': token})
        urllib.request.urlopen(req, timeout=10).read()
        assert proc.wait(timeout=20) == 0, 'the program did not end cleanly'

        # it opens again on the last folder chosen
        proc = subprocess.Popen(cmd + ['--port', str(port), '--no-browser', '--config', config],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        deadline = time.time() + 60
        while True:
            try:
                st3 = json.loads(get(base + '/api/status', 2))
                break
            except OSError:
                if time.time() > deadline or proc.poll() is not None:
                    raise AssertionError('did not start again')
                time.sleep(0.3)
        assert st3['root'] == sub, st3

        if program.endswith('-debug') or program.endswith('-debug.exe'):
            logfile = os.path.join(os.path.dirname(os.path.abspath(program)), 'aristarkus-debug.log')
            assert os.path.exists(logfile), 'debug build wrote no aristarkus-debug.log'
            assert 'scanned' in open(logfile, encoding='utf-8').read()
        print(f'ok   {program}  ({st["platform"]}, live activity: {st["watchMode"]}, reads: {st["reads"]})')
    except Exception:
        stop(proc)
        print(f'FAIL {program}\n--- program output ---\n{proc.communicate(timeout=10)[0]}', file=sys.stderr)
        raise
    finally:
        stop(proc)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(config, ignore_errors=True)


if __name__ == '__main__':
    for i, program in enumerate(sys.argv[1:]):
        check(program, 18700 + i * 10)
