"""Four generic MQTT3.1.1 connection-recovery branches, independent of broker IDs."""
from collections import Counter
import select
import socket
import struct
import threading
import time
import uuid

from calibration_model import MUTATIONS, RECOVERY_BRANCHES
from faults import CutProxy
from wire import Client, ScopedTrace, packet, publication, read_frame, string

VERSION = '3.0.0'
BRANCH_NAMES = tuple(direction + '/' + phase for direction, phase in sorted(RECOVERY_BRANCHES))


def mid_bytes(mid):
    return struct.pack('!H', mid)


def session(client, present):
    client.trace.check(client.session_present is present, 'qos2-session-present', expected=present)


def send_qos2(client, topic, payload, mid):
    client.send(0x34, string(topic) + mid_bytes(mid) + payload)
    client.expect(0x50, mid_bytes(mid))
    client.send(0x62, mid_bytes(mid))
    client.expect(0x70, mid_bytes(mid))


def next_frame(client, deadline=None):
    return client.pending.popleft() if client.pending else client.recv(deadline=deadline)


class Receiver:
    """A test receiver delivers once at PUBREL, retaining state across reconnects.

    Repeated PUBREL is ACKed without redelivery. After PUBCOMP a new PUBLISH
    starts a new exchange even if the ID is reused: duplicated application
    messages cannot disappear behind a permanent Packet ID deduplication set.
    """
    def __init__(self, topic, original, health, trace):
        self.topic, self.original, self.health, self.trace = topic, original, health, trace
        self.pending, self.deliveries = {}, Counter()

    def publication(self, frame):
        value = publication(frame)
        self.trace.check(value['topic'] == self.topic and value['payload'] in {self.original, self.health}
                         and value['qos'] == 2 and not value['retain'] and value['mid'] is not None and value['mid'] > 0,
                         'qos2-publication-content')
        mid = value['mid']
        if mid in self.pending:
            old = self.pending[mid]['value']
            self.trace.check(value['dup'] and value['payload'] == old['payload'], 'qos2-inflight-retransmit')
        else:
            self.pending[mid] = dict(value=value, delivered=False)
        return value

    def release(self, client, frame, *, complete=True):
        self.trace.check(frame[0] == 0x62 and len(frame[1]) == 2 and int.from_bytes(frame[1], 'big') > 0,
                         'qos2-PUBREL')
        mid = int.from_bytes(frame[1], 'big')
        state = self.pending.get(mid)
        if state and not state['delivered']:
            payload = state['value']['payload']
            state['delivered'] = True
            self.deliveries[payload] += 1
            self.trace.event('application-delivery', packet_id=mid, topic=self.topic, payload_hex=payload.hex(),
                             count=self.deliveries[payload], connection=client.connection)
            self.trace.check(self.deliveries[payload] == 1, 'qos2-single-application-delivery')
        if complete:
            client.send(0x70, frame[1])
            self.pending.pop(mid, None)

    def step(self, client, deadline=None):
        frame = next_frame(client, deadline)
        if frame[0] >> 4 == 3:
            value = self.publication(frame)
            client.send(0x50, mid_bytes(value['mid']))
        else:
            self.release(client, frame)

    def collect(self, client):
        deadline = time.monotonic() + 5
        while self.deliveries[self.original] < 1 or self.deliveries[self.health] < 1:
            if time.monotonic() >= deadline:
                raise TimeoutError('recovery application delivery budget exceeded')
            client.stream.settimeout(max(0.01, deadline - time.monotonic()))
            self.step(client, deadline)
        start, frames = time.monotonic(), 0
        while True:
            remaining = 0.4 - (time.monotonic() - start)
            if remaining <= 0:
                break
            if not client.pending and not select.select([client.stream], [], [], remaining)[0]:
                break
            client.stream.settimeout(max(0.01, remaining))
            self.step(client, start + 0.4)
            frames += 1
            if frames > 128:
                raise RuntimeError('recovery observation frame budget exceeded')
        self.trace.event('observation', seconds_required=0.4, actual_seconds=time.monotonic() - start, completed=True)
        self.trace.check(self.deliveries[self.original] == self.deliveries[self.health] == 1 and not self.pending,
                         'qos2-health-control-and-single-delivery')


def retransmission(client, receiver, original):
    frame = next_frame(client)
    value = publication(frame)
    client.trace.check(value['mid'] == original['mid'], 'qos2-retransmit-mid')
    client.trace.check(value['dup'], 'qos2-retransmit-dup')
    client.trace.check(value['topic'] == original['topic'] and value['payload'] == original['payload']
                       and value['qos'] == 2 and not value['retain'], 'qos2-retransmit-content')
    receiver.publication(frame)
    client.send(0x50, mid_bytes(value['mid']))


def release(client, receiver, mid, *, complete=True):
    frame = next_frame(client)
    client.trace.check(frame == (0x62, mid_bytes(mid)), 'qos2-PUBREL')
    receiver.release(client, frame, complete=complete)


def outbound_cut(endpoint, phase, identity, topic, original, health, trace):
    with Client(endpoint, trace) as subscriber:
        subscriber.subscribe(topic, qos=2)
        mid = 0x1234
        body = string(topic) + mid_bytes(mid) + original
        ack = 0x50 if phase == 'AwaitPubRec' else 0x70
        with CutProxy(endpoint, trace, ack) as proxy:
            initial = Client(proxy.address, trace, clean=False, identity=identity)
            try:
                session(initial, False)
                initial.send(0x34, body)
                if phase == 'AwaitPubComp':
                    initial.expect(0x50, mid_bytes(mid))
                    initial.send(0x62, mid_bytes(mid))
                try:
                    observed = initial.recv()
                except (EOFError, ConnectionResetError):
                    pass
                else:
                    trace.check(False, 'qos2-ACK-not-delivered', observed=packet(*observed).hex())
                trace.check(proxy.cut.wait(timeout=2) and proxy.dropped == (ack, mid_bytes(mid)), 'qos2-cut-correct-ACK')
                trace.event('qos2-disconnect', sender_state=phase, packet_id=mid, ack_received_by_client=False)
            finally:
                initial.abort()
        with Client(endpoint, trace, clean=False, identity=identity) as resumed:
            session(resumed, True)
            if phase == 'AwaitPubRec':
                resumed.send(0x3c, body)
                resumed.expect(0x50, mid_bytes(mid))
            resumed.send(0x62, mid_bytes(mid))
            resumed.expect(0x70, mid_bytes(mid))
            # New message reuses the completed incoming ID. Queue its duplicate
            # before reading PUBREC; once PUBREC is processed, advance to PUBREL.
            healthy_body = string(topic) + mid_bytes(mid) + health
            resumed.send(0x34, healthy_body)
            resumed.send(0x3c, healthy_body)
            resumed.expect(0x50, mid_bytes(mid))
            resumed.expect(0x50, mid_bytes(mid))
            resumed.send(0x62, mid_bytes(mid))
            resumed.expect(0x70, mid_bytes(mid))
            Receiver(topic, original, health, trace).collect(subscriber)


def inbound_cut(endpoint, phase, identity, topic, original, health, trace):
    receiver = Receiver(topic, original, health, trace)
    initial = Client(endpoint, trace, clean=False, identity=identity)
    try:
        session(initial, False)
        initial.subscribe(topic, qos=2)
        with Client(endpoint, trace) as publisher:
            send_qos2(publisher, topic, original, 0x4321)
            value = receiver.publication(next_frame(initial))
            trace.check(not value['dup'], 'qos2-new-publication-dup0')
            if phase == 'AwaitPubComp':
                initial.send(0x50, mid_bytes(value['mid']))
                release(initial, receiver, value['mid'], complete=False)
            trace.event('qos2-disconnect', sender_state=phase, packet_id=value['mid'],
                        withheld='PUBREC' if phase == 'AwaitPubRec' else 'PUBCOMP')
            initial.abort()
            with Client(endpoint, trace, clean=False, identity=identity) as resumed:
                session(resumed, True)
                if phase == 'AwaitPubRec':
                    retransmission(resumed, receiver, value)
                release(resumed, receiver, value['mid'])
                send_qos2(publisher, topic, health, 0x4322)
                receiver.collect(resumed)
    finally:
        initial.abort()


def recovery(endpoint, trace):
    for direction, phase in sorted(RECOVERY_BRANCHES):
        scoped = ScopedTrace(trace, direction=direction, phase=phase)
        identity = 'mv-recover-' + uuid.uuid4().hex[:16]
        topic = 'validation/' + uuid.uuid4().hex + '/recovery'
        original, health = b'original', b'healthy-after-recovery'
        scoped.event('qos2-branch-start', scenario_version=VERSION, client_id=identity, topic=topic,
                     original_hex=original.hex(), health_hex=health.hex())
        try:
            if direction == 'client-to-broker':
                outbound_cut(endpoint, phase, identity, topic, original, health, scoped)
            else:
                inbound_cut(endpoint, phase, identity, topic, original, health, scoped)
        finally:
            with Client(endpoint, scoped, clean=True, identity=identity):
                pass
            scoped.event('qos2-clean-session-reset', client_id=identity)
        scoped.event('qos2-branch-completed', original_deliveries=1, health_deliveries=1)


def resume_probe(endpoint, phase, trace):
    """Same inbound recovery assertions exercised against controlled test peers."""
    identity = 'mv-cal-' + uuid.uuid4().hex[:16]
    receiver = Receiver('topic', b'original', b'health', trace)
    initial = Client(endpoint, trace, clean=False, identity=identity)
    try:
        session(initial, False)
        initial.subscribe('topic', qos=2)
        value = receiver.publication(next_frame(initial))
        if phase == 'AwaitPubComp':
            initial.send(0x50, mid_bytes(value['mid']))
            release(initial, receiver, value['mid'], complete=False)
        initial.abort()
        resumed = Client(endpoint, trace, clean=False, identity=identity)
        try:
            session(resumed, True)
            if phase == 'AwaitPubRec':
                retransmission(resumed, receiver, value)
            release(resumed, receiver, value['mid'])
            receiver.collect(resumed)
        finally:
            resumed.abort()
    finally:
        initial.abort()


def recovery_peer(listener, mutation, phase, trace):
    """A small wire peer, with no Broker implementation or production imports."""
    mid = 513
    def expect(stream, kind):
        frame = read_frame(stream)
        if frame[0] != kind:
            raise RuntimeError('calibration peer unexpected input')
        return frame
    def emit(stream, kind, body):
        raw = packet(kind, body)
        trace.event('calibration-peer', sent_hex=raw.hex())
        stream.sendall(raw)
    def drain(stream):
        while stream.recv(1024):
            pass
    def handshake(stream, identifier, payload):
        emit(stream, 0x34, string('topic') + mid_bytes(identifier) + payload)
        expect(stream, 0x50)
        emit(stream, 0x62, mid_bytes(identifier))
        expect(stream, 0x70)
    first, _ = listener.accept()
    with first:
        first.settimeout(5)
        expect(first, 0x10)
        emit(first, 0x20, b'\0\0')
        expect(first, 0x82)
        emit(first, 0x90, b'\0\x09\x02')
        emit(first, 0x34, string('topic') + mid_bytes(mid) + b'original')
        if phase == 'AwaitPubComp':
            expect(first, 0x50)
            emit(first, 0x62, mid_bytes(mid))
        drain(first)
    second, _ = listener.accept()
    with second:
        second.settimeout(5)
        expect(second, 0x10)
        emit(second, 0x20, b'\0\0' if mutation == 'recovery-session-lost' else b'\x01\0')
        if mutation == 'recovery-session-lost':
            return drain(second)
        if phase == 'AwaitPubRec':
            new_mid = mid + 1 if mutation == 'recovery-wrong-mid' else mid
            payload = b'changed' if mutation == 'recovery-wrong-payload' else b'original'
            emit(second, 0x34 if mutation == 'recovery-missing-dup' else 0x3c,
                 string('topic') + mid_bytes(new_mid) + payload)
            if mutation in {'recovery-wrong-mid', 'recovery-missing-dup', 'recovery-wrong-payload'}:
                return drain(second)
            expect(second, 0x50)
        emit(second, 0x62, mid_bytes(mid + 1 if mutation == 'recovery-wrong-pubrel-mid' else mid))
        if mutation == 'recovery-wrong-pubrel-mid':
            return drain(second)
        expect(second, 0x70)
        handshake(second, 900, b'health')
        if mutation == 'recovery-duplicate-delivery':
            emit(second, 0x34, string('topic') + mid_bytes(901) + b'original')
            expect(second, 0x50)
            emit(second, 0x62, mid_bytes(901))
        drain(second)


def recovery_calibration(trace):
    for mutation in MUTATIONS:
        if not mutation.startswith('recovery-'):
            continue
        scoped, errors = ScopedTrace(trace, mutation=mutation), []
        phase = 'AwaitPubComp' if mutation == 'recovery-wrong-pubrel-mid' else 'AwaitPubRec'
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(2)
            listener.settimeout(5)
            def peer():
                try:
                    recovery_peer(listener, mutation, phase, scoped)
                except Exception as error:
                    errors.append(type(error).__name__)
            worker = threading.Thread(target=peer)
            worker.start()
            rejected = False
            try:
                resume_probe(listener.getsockname(), phase, scoped)
            except AssertionError as error:
                rejected = str(error) == MUTATIONS[mutation]
            finally:
                worker.join(timeout=6)
            scoped.check(not worker.is_alive() and not errors, 'calibration-peer-completed')
            scoped.check(rejected, 'known-mutation-detected')
