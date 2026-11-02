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


def check(program, port):
    tmp = tempfile.mkdtemp(prefix='aristarkus-smoke-')
    for rel, size in FILES.items():
        os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
        with open(os.path.join(tmp, rel), 'wb') as f:
            f.write(b'x' * size)
    cmd = [sys.executable, program] if program.endswith('.py') else [program]
    proc = subprocess.Popen(cmd + ['--root', tmp, '--port', str(port), '--no-browser'],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
                kind, path, _ = events.get(timeout=0.5)
            except queue.Empty:
                continue
            assert kind != 'error', path
            for k, rel in want:
                if kind == k and path.endswith('/' + rel):
                    seen.add((k, rel))
        assert not want - seen, f'missing events: {want - seen}'

        if program.endswith('-debug') or program.endswith('-debug.exe'):
            logfile = os.path.join(os.path.dirname(os.path.abspath(program)), 'aristarkus-debug.log')
            assert os.path.exists(logfile), 'debug build wrote no aristarkus-debug.log'
            assert 'scanned' in open(logfile, encoding='utf-8').read()
        print(f'ok   {program}  ({st["platform"]}, live activity: {st["watchMode"]}, reads: {st["reads"]})')
    except Exception:
        proc.terminate()
        print(f'FAIL {program}\n--- program output ---\n{proc.communicate(timeout=10)[0]}', file=sys.stderr)
        raise
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    for i, program in enumerate(sys.argv[1:]):
        check(program, 18700 + i * 10)
