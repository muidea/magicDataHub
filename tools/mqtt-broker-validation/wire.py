"""Independent MQTT3.1.1 test transport; no broker implementation imports."""
import collections
import io
import json
import select
import socket
import struct
import threading
import time
import uuid
from schema_check import check
from common import InputError, decode_json


class Blocked(Exception):
    """Required environmental control is unavailable; never a passing result."""


class Trace:
    def __init__(self, path):
        self.path = path
        self.start = time.monotonic()
        self.lock = threading.Lock()
        self.sequence = 0

    def event(self, kind, **fields):
        if {'schema_version', 'sequence', 'seconds'} & fields.keys():
            raise InputError('event metadata is owned by Trace')
        with self.lock, self.path.open('a') as output:
            value = dict(kind=kind, seconds=time.monotonic() - self.start,
                         schema_version=1, sequence=self.sequence, **fields)
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            check('event', decode_json(encoded))
            output.write(encoded + '\n')
            self.sequence += 1

    def check(self, condition, assertion, **fields):
        self.event('assertion', assertion=assertion, passed=bool(condition), **fields)
        if not condition:
            raise AssertionError(assertion)


class ScopedTrace:
    def __init__(self, trace, **fields):
        self.trace, self.fields = trace, fields

    def event(self, kind, **fields):
        self.trace.event(kind, **(self.fields | fields))

    def check(self, condition, assertion, **fields):
        self.trace.check(condition, assertion, **(self.fields | fields))


def string(value):
    value = value.encode() if isinstance(value, str) else value
    return struct.pack('!H', len(value)) + value


def packet(kind, body=b''):
    size = len(body)
    remaining = bytearray()
    while True:
        byte = size % 128
        size //= 128
        remaining.append(byte | (128 if size else 0))
        if not size:
            return bytes([kind]) + remaining + body


def read_frame(stream, on_read=None, *, deadline=None):
    def exact(count):
        value = b''
        while len(value) < count:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('MQTT frame deadline exceeded')
                stream.settimeout(remaining)
            part = stream.recv(count - len(value))
            if not part:
                raise EOFError('MQTT peer closed')
            if on_read:
                on_read(part)
            value += part
        return value
    kind = exact(1)[0]
    size = 0
    for shift in (0, 7, 14, 21):
        byte = exact(1)[0]
        size += (byte & 127) << shift
        if not byte & 128:
            if size > 1024 * 1024:
                raise ValueError('test receive budget exceeded')
            return kind, exact(size)
    raise ValueError('invalid remaining length')


def decode_frame(raw):
    """Decode exactly one captured frame using the same bounded wire format."""
    class CapturedStream(io.BytesIO):
        recv = io.BytesIO.read

    stream = CapturedStream(raw)
    try:
        value = read_frame(stream)
        if stream.read():
            raise ValueError('trailing frame bytes')
        return value
    except (EOFError, ValueError) as error:
        raise InputError('invalid captured MQTT frame') from error


def publication(frame):
    kind, body = frame
    if kind >> 4 != 3 or len(body) < 2:
        raise AssertionError('expected PUBLISH')
    length = int.from_bytes(body[:2], 'big')
    position = 2 + length
    topic = body[2:position].decode('utf-8')
    qos = kind >> 1 & 3
    mid = int.from_bytes(body[position:position + 2], 'big') if qos else None
    if qos:
        position += 2
    return dict(topic=topic, payload=body[position:], qos=qos,
                retain=bool(kind & 1), dup=bool(kind & 8), mid=mid)


class Client:
    def __init__(self, endpoint, trace, *, clean=True, identity=None):
        self.trace = trace
        self.identity = identity or 'mv-' + uuid.uuid4().hex[:18]
        self.connection = uuid.uuid4().hex[:16]
        self.pending = collections.deque()
        self.stream = socket.create_connection(endpoint, timeout=5)
        self.closed = False
        self.trace.event('connection', connection=self.connection, client_id=self.identity,
                         peer=list(self.stream.getpeername()))
        try:
            self.send(0x10, b'\x00\x04MQTT\x04' + bytes([2 if clean else 0])
                      + b'\x00\x1e' + string(self.identity))
            connack = self.recv()
            self.trace.check(connack == (0x20, b'\x00\x00') if clean else
                             connack in [(0x20, b'\x00\x00'), (0x20, b'\x01\x00')],
                             'valid-success-CONNACK')
            self.session_present = bool(connack[1][0])
        except BaseException:
            self.stream.close()
            self.closed = True
            raise

    def send(self, kind, body=b''):
        value = packet(kind, body)
        self.stream.sendall(value)
        self.trace.event('bytes', connection=getattr(self, 'connection', self.identity), direction='tx', hex=value.hex())

    def recv(self, *, deadline=None):
        kind, body = read_frame(self.stream, lambda part: self.trace.event(
            'bytes', connection=getattr(self, 'connection', self.identity), direction='rx', hex=part.hex()), deadline=deadline)
        self.trace.event('frame', connection=getattr(self, 'connection', self.identity), direction='rx', hex=packet(kind, body).hex())
        return kind, body

    def expect(self, kind, body):
        # A PUBLISH may legally precede SUBACK or be interleaved with ACKs.
        while True:
            frame = self.recv()
            if frame[0] >> 4 == 3:
                self.pending.append(frame)
                continue
            self.trace.check(frame == (kind, body), 'expected-control-packet',
                             expected=packet(kind, body).hex(), observed=packet(*frame).hex())
            return

    def subscribe(self, topic, qos=1):
        self.send(0x82, b'\x00\x09' + string(topic) + bytes([qos]))
        self.expect(0x90, b'\x00\x09' + bytes([qos]))

    def publish(self, topic, payload, qos=1, retain=False):
        body = string(topic) + (b'\x00\x17' if qos else b'') + payload
        self.send(0x30 | qos << 1 | int(retain), body)
        if qos == 1:
            self.expect(0x40, b'\x00\x17')
        elif qos == 2:
            self.expect(0x50, b'\x00\x17')
            self.send(0x62, b'\x00\x17')
            self.expect(0x70, b'\x00\x17')

    def delivery(self, topic, payload, qos=None, retain=None):
        frame = self.pending.popleft() if self.pending else self.recv()
        value = publication(frame)
        self.trace.check(value['topic'] == topic and value['payload'] == payload,
                         'delivery-topic-and-payload', observed_topic=value['topic'])
        if qos is not None:
            self.trace.check(value['qos'] == qos, 'delivery-qos')
        if retain is not None:
            self.trace.check(value['retain'] == retain, 'delivery-retain')
        if value['qos'] == 1:
            self.send(0x40, struct.pack('!H', value['mid']))
        elif value['qos'] == 2:
            mid = struct.pack('!H', value['mid'])
            self.send(0x50, mid)
            self.expect(0x62, mid)
            self.send(0x70, mid)
        return value

    def no_message(self, seconds=0.4):
        self.trace.check(not self.pending, 'no-pending-forbidden-publication')
        start = time.monotonic()
        if not select.select([self.stream], [], [], seconds)[0]:
            self.trace.event('observation', seconds_required=seconds,
                             actual_seconds=time.monotonic() - start, completed=True)
            return
        # Any readable byte, partial packet or EOF invalidates silent success.
        frame = self.recv()
        self.trace.check(False, 'forbidden-packet-in-observation-window', observed=packet(*frame).hex())

    def abort(self):
        if not self.closed:
            self.trace.event('connection-aborted', connection=self.connection, client_id=self.identity)
            try:
                self.stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.stream.close()
            self.closed = True

    def close(self):
        if not self.closed:
            try:
                self.send(0xe0)
            finally:
                self.stream.close()
                self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
