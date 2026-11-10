#!/usr/bin/env python3
"""Run real local binaries with disposable state; no external business DSNs."""
import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = 'mdh-disposable-test-only'


def exact(stream, count):
    result = b''
    while len(result) < count:
        chunk = stream.recv(count - len(result))
        if not chunk:
            raise AssertionError('unexpected protocol EOF')
        result += chunk
    return result


def mqtt(address):
    with socket.create_connection(address, timeout=5) as stream:
        client = b'mdh-smoke'
        body = b'\x00\x04MQTT\x04\x02\x00\x0a' + struct.pack('!H', len(client)) + client
        stream.sendall(b'\x10' + bytes([len(body)]) + body)
        assert exact(stream, 4) == b'\x20\x02\x00\x00'
        stream.sendall(b'\xc0\x00')
        assert exact(stream, 2) == b'\xd0\x00'
        stream.sendall(b'\xe0\x00')


def pgwire(address):
    with socket.create_connection(address, timeout=5) as stream:
        stream.sendall(struct.pack('!II', 8, 80877103))
        assert exact(stream, 1) == b'N'
        params = b'user\x00fedengine\x00database\x00fedengine\x00\x00'
        stream.sendall(struct.pack('!II', len(params) + 8, 196608) + params)

        def message():
            kind = exact(stream, 1)
            length = struct.unpack('!I', exact(stream, 4))[0]
            assert 4 <= length <= 16 * 1024 * 1024
            body = exact(stream, length - 4)
            assert kind != b'E', 'PG returned an error'
            return kind, body

        def password(body):
            stream.sendall(b'p' + struct.pack('!I', len(body) + 4) + body)

        nonce = base64.b64encode(os.urandom(18)).decode()
        first = 'n=fedengine,r=' + nonce
        expected_signature = None
        authenticated = False
        while True:
            kind, body = message()
            if kind == b'R':
                auth = struct.unpack('!I', body[:4])[0]
                if auth == 10:
                    assert b'SCRAM-SHA-256\x00' in body[4:]
                    initial = ('n,,' + first).encode()
                    password(b'SCRAM-SHA-256\x00' + struct.pack('!I', len(initial)) + initial)
                elif auth == 11:
                    server = body[4:].decode()
                    fields = dict(v.split('=', 1) for v in server.split(','))
                    assert fields['r'].startswith(nonce) and len(fields['r']) > len(nonce)
                    rounds = int(fields['i'])
                    assert 1 <= rounds <= 1_000_000
                    salted = hashlib.pbkdf2_hmac('sha256', PASSWORD.encode(), base64.b64decode(fields['s']), rounds)
                    client_key = hmac.digest(salted, b'Client Key', 'sha256')
                    final = 'c=biws,r=' + fields['r']
                    transcript = (first + ',' + server + ',' + final).encode()
                    signature = hmac.digest(hashlib.sha256(client_key).digest(), transcript, 'sha256')
                    proof = bytes(a ^ b for a, b in zip(client_key, signature))
                    server_key = hmac.digest(salted, b'Server Key', 'sha256')
                    expected_signature = base64.b64encode(hmac.digest(server_key, transcript, 'sha256')).decode()
                    password((final + ',p=' + base64.b64encode(proof).decode()).encode())
                elif auth == 12:
                    assert body[4:].decode() == 'v=' + expected_signature
                elif auth == 0:
                    authenticated = True
                else:
                    raise AssertionError('unsupported PG authentication in smoke')
            elif kind == b'Z':
                assert authenticated
                break
        sql = b'SELECT 42::INTEGER\x00'
        stream.sendall(b'Q' + struct.pack('!I', len(sql) + 4) + sql)
        found = False
        while True:
            kind, body = message()
            if kind == b'D':
                count, length = struct.unpack('!Hi', body[:6])
                assert count == 1 and body[6:6 + length] == b'42'
                found = True
            elif kind == b'Z':
                assert found
                break
        stream.sendall(b'X' + struct.pack('!I', 4))


def management_http(address):
    connection = http.client.HTTPConnection(*address, timeout=5)
    try:
        connection.request('GET', '/api/v1/sources')
        response = connection.getresponse()
        assert response.status == 200
        json.loads(response.read())
    finally:
        connection.close()


def endpoints(event):
    result = {}
    for endpoint in event['endpoints']:
        host, port = endpoint['address'].rsplit(':', 1)
        result[endpoint['protocol']] = (host.strip('[]'), int(port))
    return result


class Process:
    def __init__(self, binary, config, cwd, env, *, command=None):
        self.lines = []
        self.output = queue.Queue()
        self.process = subprocess.Popen(command or [str(binary), '--config', str(config)], cwd=cwd, env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def read():
            for line in self.process.stdout:
                self.lines.append(line)
                self.output.put(line)
            self.output.put(None)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def ready(self):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            line = self.output.get(timeout=max(0.1, deadline - time.monotonic()))
            assert line is not None, 'service exited before Ready'
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get('event') == 'Ready':
                return endpoints(event)
        raise AssertionError('service startup timeout')

    def stop(self, sig):
        self.process.send_signal(sig)
        assert self.process.wait(timeout=45) == 0, 'service did not drain successfully'
        self.reader.join(timeout=5)
        assert any('GracefullyStopped' in line for line in self.lines)
        assert any('"event":"Stopping"' in line for line in self.lines)
        assert PASSWORD not in ''.join(self.lines)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        self.process.stdout.close()


def run(args):
    binaries = args.bin_dir.resolve()
    for binary, database in [('mqtt-server', False), ('fedengine-server', True), ('datahub-server', True)]:
        linked = subprocess.run(['ldd', str(binaries / binary)], capture_output=True, text=True, check=True).stdout
        assert ('libduckdb' in linked) == database, 'binary native dependency isolation failed'
    env = os.environ.copy()
    env['FEDENGINE_PASSWORD'] = PASSWORD
    if args.duckdb_library_dir:
        env['LD_LIBRARY_PATH'] = str(args.duckdb_library_dir.resolve()) + os.pathsep + env.get('LD_LIBRARY_PATH', '')
    env['RUST_LOG'] = 'info'
    for name, profile, protocols in [
        ('mqtt-server', 'mqtt-development.toml', {'mqtt'}),
        ('fedengine-server', 'fedengine-development.toml', {'pgwire', 'management'}),
        ('datahub-server', 'combined-development.toml', {'mqtt', 'pgwire', 'management'}),
    ]:
        with tempfile.TemporaryDirectory(prefix='mdh-binary-') as temporary:
            directory = Path(temporary)
            shutil.copytree(ROOT / 'config', directory / 'config')
            for file in (directory / 'config').rglob('*.toml'):
                text = file.read_text()
                for port in [1883, 2883, 15432, 15433, 25432, 25433]:
                    text = text.replace(f'127.0.0.1:{port}"', '127.0.0.1:0"')
                file.write_text(text)
            config = directory / 'config' / profile
            # Check from a foreign CWD, with no password available, and assert no resources.
            check_env = env.copy()
            check_env.pop('FEDENGINE_PASSWORD', None)
            result = subprocess.run([str(binaries / name), '--config', str(config), '--check-config'],
                                    cwd='/', env=check_env, capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, name + ' check-config failed'
            assert not (directory / 'var').exists()
            original = config.read_text()
            for invalid in [
                original + '\n[unknown]\npassword="private-canary"\n',
                original.replace('shutdown_timeout_ms = 30000', 'shutdown_timeout_ms = 30000\nmax_tasks = 1'),
                original.replace('config_file =', 'invalid_field ='),
                'x' * (1024 * 1024 + 1),
            ]:
                config.write_text(invalid)
                result = subprocess.run([str(binaries / name), '--config', str(config), '--check-config'],
                                        cwd='/', env=check_env, capture_output=True, text=True, timeout=15)
                assert result.returncode != 0
                assert 'private-canary' not in result.stdout + result.stderr
                assert not (directory / 'var').exists()
            config.write_text(original)
            for sig in (signal.SIGINT, signal.SIGTERM):
                process = Process(binaries / name, config, '/', env)
                try:
                    listeners = process.ready()
                    assert set(listeners) == protocols
                    for protocol, address in listeners.items():
                        {'mqtt': mqtt, 'pgwire': pgwire, 'management': management_http}[protocol](address)
                    process.stop(sig)
                    for address in listeners.values():
                        with socket.socket() as listener:
                            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                            listener.bind(address)
                    if name == 'mqtt-server':
                        assert not (directory / 'var').exists()
                    else:
                        assert list((directory / 'var').rglob('fedengine.duckdb'))
                finally:
                    process.close()
            print(name + ': check-config, protocol, SIGINT/SIGTERM, port release and reopen Passed', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path)
    parser.add_argument('--duckdb-library-dir', type=Path)
    args = parser.parse_args()
    if args.bin_dir is None:
        metadata = json.loads(subprocess.check_output([
            'cargo', 'metadata', '--format-version=1', '--no-deps', '--locked', '--offline',
            '--manifest-path', str(ROOT / 'Cargo.toml')], text=True))
        target = Path(metadata['target_directory'])
        if os.environ.get('CARGO_BUILD_TARGET'):
            target /= os.environ['CARGO_BUILD_TARGET']
        args.bin_dir = target / 'debug'
    if args.duckdb_library_dir is None:
        if os.environ.get('DUCKDB_LIBRARY_DIR'):
            args.duckdb_library_dir = Path(os.environ['DUCKDB_LIBRARY_DIR'])
        elif not os.environ.get('LD_LIBRARY_PATH'):
            libraries = list((args.bin_dir.parent / 'duckdb-download').glob('*/v*/libduckdb.so'))
            assert len(libraries) == 1, 'set DUCKDB_LIBRARY_DIR to the matching native library directory'
            args.duckdb_library_dir = libraries[0].parent
    run(args)
