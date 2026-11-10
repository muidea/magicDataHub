"""Bounded test-only PostgreSQL v3 client for exercising the actual host binary."""
import base64
import hashlib
import hmac
import os
import socket
import struct


def exact(stream, count):
    data = b''
    while len(data) < count:
        part = stream.recv(count - len(data))
        if not part:
            raise EOFError('PG protocol EOF')
        data += part
    return data


class PGError(Exception):
    def __init__(self, body):
        fields = {}
        for item in body.split(b'\0'):
            if item:
                fields[item[:1]] = item[1:].decode(errors='replace')
        self.sqlstate = fields.get(b'C', 'XX000')
        # Do not echo SQL, source messages, credentials or DSNs into acceptance logs.
        super().__init__('PostgreSQL SQLSTATE ' + self.sqlstate)


class Client:
    def __init__(self, address, database='fedengine', user='fedengine', password='mdh-disposable-test-only'):
        self.address = address
        self.stream = socket.create_connection(address, timeout=20)
        self.backend_key = None
        try:
            params = f'user\0{user}\0database\0{database}\0\0'.encode()
            self.stream.sendall(struct.pack('!II', len(params) + 8, 196608) + params)
            nonce = base64.b64encode(os.urandom(18)).decode()
            first = 'n=' + user.replace('=', '=3D').replace(',', '=2C') + ',r=' + nonce
            expected_signature = None
            authenticated = False
            while True:
                kind, body = self.message()
                if kind == b'R':
                    auth = struct.unpack('!I', body[:4])[0]
                    if auth == 10:
                        assert b'SCRAM-SHA-256\0' in body[4:]
                        initial = ('n,,' + first).encode()
                        self.send(b'p', b'SCRAM-SHA-256\0' + struct.pack('!I', len(initial)) + initial)
                    elif auth == 11:
                        server = body[4:].decode()
                        fields = dict(v.split('=', 1) for v in server.split(','))
                        assert fields['r'].startswith(nonce) and len(fields['r']) > len(nonce)
                        rounds = int(fields['i'])
                        assert 1 <= rounds <= 1_000_000
                        salted = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(fields['s']), rounds)
                        client_key = hmac.digest(salted, b'Client Key', 'sha256')
                        final = 'c=biws,r=' + fields['r']
                        transcript = (first + ',' + server + ',' + final).encode()
                        signature = hmac.digest(hashlib.sha256(client_key).digest(), transcript, 'sha256')
                        proof = bytes(a ^ b for a, b in zip(client_key, signature))
                        expected_signature = base64.b64encode(hmac.digest(hmac.digest(salted, b'Server Key', 'sha256'), transcript, 'sha256')).decode()
                        self.send(b'p', (final + ',p=' + base64.b64encode(proof).decode()).encode())
                    elif auth == 12:
                        assert body[4:].decode() == 'v=' + expected_signature
                    elif auth == 0:
                        authenticated = True
                    else:
                        raise AssertionError('unsupported authentication in test fixture')
                elif kind == b'K':
                    assert len(body) == 8
                    self.backend_key = body
                elif kind == b'Z':
                    assert authenticated
                    break
        except BaseException:
            self.stream.close()
            raise

    def send(self, kind, body=b''):
        self.stream.sendall(kind + struct.pack('!I', len(body) + 4) + body)

    def message(self):
        kind = exact(self.stream, 1)
        size = struct.unpack('!I', exact(self.stream, 4))[0]
        assert 4 <= size <= 16 * 1024 * 1024
        body = exact(self.stream, size - 4)
        if kind == b'E':
            raise PGError(body)
        return kind, body

    @staticmethod
    def row(body):
        count = struct.unpack('!H', body[:2])[0]
        result, offset = [], 2
        for _ in range(count):
            length = struct.unpack('!i', body[offset:offset + 4])[0]
            offset += 4
            if length == -1:
                result.append(None)
            else:
                assert 0 <= length <= len(body) - offset
                result.append(body[offset:offset + length].decode())
                offset += length
        assert offset == len(body)
        return result

    def ready(self):
        while self.message()[0] != b'Z':
            pass

    def query(self, sql):
        self.send(b'Q', sql.encode() + b'\0')
        rows = []
        try:
            while True:
                kind, body = self.message()
                if kind == b'D':
                    rows.append(self.row(body))
                elif kind == b'Z':
                    return rows
        except PGError:
            self.ready()
            raise

    def parse(self, sql, name='stmt', types=()):
        self.send(b'P', name.encode() + b'\0' + sql.encode() + b'\0' + struct.pack('!H', len(types)) + b''.join(struct.pack('!I', t) for t in types))
        self.send(b'H')
        assert self.message()[0] == b'1'

    def bind(self, values=(), portal='portal', statement='stmt'):
        body = portal.encode() + b'\0' + statement.encode() + b'\0\x00\x00' + struct.pack('!H', len(values))
        for value in values:
            data = None if value is None else str(value).encode()
            body += struct.pack('!i', -1 if data is None else len(data)) + (data or b'')
        self.send(b'B', body + b'\x00\x00')
        self.send(b'H')
        assert self.message()[0] == b'2'

    def execute(self, count=0, portal='portal'):
        self.send(b'E', portal.encode() + b'\0' + struct.pack('!I', count))
        self.send(b'H')
        rows = []
        while True:
            kind, body = self.message()
            if kind == b'D':
                rows.append(self.row(body))
            elif kind in (b's', b'C'):
                return rows, kind

    def sync(self):
        self.send(b'S')
        self.ready()

    def close_portal(self, portal='portal'):
        self.send(b'C', b'P' + portal.encode() + b'\0')
        assert self.message()[0] == b'3'
        self.sync()

    def cancel(self):
        assert self.backend_key is not None
        with socket.create_connection(self.address, timeout=5) as stream:
            stream.sendall(struct.pack('!II', 16, 80877102) + self.backend_key)

    def close(self):
        if self.stream.fileno() != -1:
            try:
                self.send(b'X')
            except OSError:
                pass
            self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
