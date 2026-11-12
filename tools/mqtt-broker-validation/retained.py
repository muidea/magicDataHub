"""QoS1 Retained scenario v2 and bounded, duplicate-aware receive oracle."""
import select
import struct
import time
import uuid

from common import InputError
from wire import Client, decode_frame, publication

VERSION = '2.0.0'
PHASES = ('old-replayed', 'non-retained-preserved', 'delete-delivered',
          'late-control-delivered', 'post-control-empty', 'cleanup-completed')
PAYLOADS = (b'old', b'transient-before-delete', b'', b'live-control', b'post-control')


class Receiver:
    def __init__(self, client, topic):
        self.client, self.topic = client, topic
        self.seen = {}

    def consume(self, frame, expected=None):
        value = publication(frame)
        signature = (value['topic'], value['payload'], value['qos'], value['retain'])
        previous = self.seen.get(value['mid'])
        duplicate = value['dup'] and previous == signature
        # A first-observed DUP=1 is legal; Packet IDs may be reused for a new
        # exchange. Only an identical known exchange is treated as a duplicate.
        valid = (value['topic'] == self.topic and value['qos'] == 1 and value['mid'] not in (None, 0)
                 and (duplicate or expected is not None and (value['payload'], value['retain']) == expected))
        self.client.trace.check(valid, 'retained-delivery', observed_topic=value['topic'])
        self.client.send(0x40, struct.pack('!H', value['mid']))
        if not duplicate:
            self.seen[value['mid']] = signature
        return not duplicate

    def delivery(self, payload, retain):
        deadline = time.monotonic() + 5
        timeout = self.client.stream.gettimeout()
        try:
            while time.monotonic() < deadline:
                frame = self.client.pending.popleft() if self.client.pending else self.client.recv(deadline=deadline)
                if self.consume(frame, (payload, retain)):
                    return
            raise TimeoutError('Retained delivery deadline exceeded')
        finally:
            self.client.stream.settimeout(timeout)

    def no_new_delivery(self, seconds=0.4):
        started = time.monotonic()
        deadline = started + seconds
        timeout = self.client.stream.gettimeout()
        try:
            while True:
                if self.client.pending:
                    self.consume(self.client.pending.popleft())
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.client.stream], [], [], remaining)[0]:
                    break
                # Partial frames, EOF and timeouts cannot become silent PASS.
                self.consume(self.client.recv(deadline=deadline))
            self.client.trace.check(True, 'retained-observation-completed')
            self.client.trace.event('retained-observation', connection=self.client.connection, topic=self.topic,
                                    seconds_required=seconds, actual_seconds=time.monotonic() - started, completed=True)
        finally:
            self.client.stream.settimeout(timeout)


def run(endpoint, trace):
    topic = 'validation/' + uuid.uuid4().hex + '/retained'
    def phase(name, client):
        trace.event('retained-phase', phase=name, scenario_version=VERSION, topic=topic, connection=client.connection)
    with Client(endpoint, trace) as publisher, Client(endpoint, trace) as early:
        receiver = Receiver(early, topic)
        try:
            publisher.publish(topic, PAYLOADS[0], retain=True)
            early.subscribe(topic)
            receiver.delivery(PAYLOADS[0], True)
            phase(PHASES[0], early)
            publisher.publish(topic, PAYLOADS[1], retain=False)
            receiver.delivery(PAYLOADS[1], False)
            with Client(endpoint, trace) as probe:
                probe.subscribe(topic)
                Receiver(probe, topic).delivery(PAYLOADS[0], True)
                phase(PHASES[1], probe)
            publisher.publish(topic, b'', retain=True)
            receiver.delivery(b'', False)
            phase(PHASES[2], early)
            with Client(endpoint, trace) as late:
                late_receiver = Receiver(late, topic)
                late.subscribe(topic)
                publisher.publish(topic, PAYLOADS[3], retain=False)
                late_receiver.delivery(PAYLOADS[3], False)
                receiver.delivery(PAYLOADS[3], False)
                late_receiver.no_new_delivery()
                phase(PHASES[3], late)
                with Client(endpoint, trace) as post:
                    post_receiver = Receiver(post, topic)
                    post.subscribe(topic)
                    publisher.publish(topic, PAYLOADS[4], retain=False)
                    post_receiver.delivery(PAYLOADS[4], False)
                    late_receiver.delivery(PAYLOADS[4], False)
                    receiver.delivery(PAYLOADS[4], False)
                    post_receiver.no_new_delivery()
                    phase(PHASES[4], post)
        finally:
            publisher.publish(topic, b'', retain=True)
            phase(PHASES[5], publisher)


def check_evidence(events):
    """Rebuild scoped facts from captured bytes, not successful assertion labels."""
    phases = [event for event in events if event['kind'] == 'retained-phase']
    if [p['phase'] for p in phases] != list(PHASES) or any(p['scenario_version'] != VERSION for p in phases):
        raise InputError('missing, duplicated or reordered Retained phase')
    topic = phases[0]['topic']
    if any(p['topic'] != topic for p in phases):
        raise InputError('Retained phase topic mismatch')
    early, probe, _, late, post, publisher = [p['connection'] for p in phases]
    if phases[2]['connection'] != early or len({early, probe, late, post, publisher}) != 5:
        raise InputError('Retained role/connection mismatch')
    connections = {event['connection'] for event in events if event['kind'] == 'connection'}
    if {early, probe, late, post, publisher} != connections:
        raise InputError('Retained role has no captured connection')
    rx_buffers, frames, tx = {}, {}, {}
    for index, event in enumerate(events):
        kind = event['kind']
        if kind == 'bytes':
            connection = event['connection']
            raw = bytes.fromhex(event['hex'])
            if event['direction'] == 'rx':
                rx_buffers[connection] = rx_buffers.get(connection, b'') + raw
            else:
                tx.setdefault(connection, []).append((index, decode_frame(raw)))
        elif kind == 'frame':
            connection, raw = event['connection'], bytes.fromhex(event['hex'])
            if rx_buffers.get(connection) != raw:
                raise InputError('Retained decoded frame differs from captured receive bytes')
            rx_buffers[connection] = b''
            frames.setdefault(connection, []).append((index, decode_frame(raw)))
    if any(rx_buffers.values()):
        raise InputError('Retained evidence has incomplete/undecoded receive bytes')
    if any(sum(frame == (0x20, b'\x00\x00') for _, frame in frames.get(c, [])) != 1 for c in [early, probe, late, post, publisher]):
        raise InputError('Retained roles lack clean successful CONNACK')
    sent = [(index, publication(frame)) for index, frame in tx.get(publisher, []) if frame[0] >> 4 == 3]
    expected_sent = [(p, r) for p, r in zip(PAYLOADS, [True, False, True, False, False])] + [(b'', True)]
    if [(v['payload'], v['retain']) for _, v in sent] != expected_sent or any(v['topic'] != topic or v['qos'] != 1 for _, v in sent):
        raise InputError('Retained publication sequence differs from scenario')
    pubacks = [(i, body) for i, (kind, body) in frames[publisher] if kind == 0x40]
    if len(pubacks) != len(sent) or any(body != struct.pack('!H', v['mid']) or i <= j for (j, v), (i, body) in zip(sent, pubacks)):
        raise InputError('Retained publisher handshake evidence incomplete')
    if any(ack[0] >= sent[index + 1][0] for index, ack in enumerate(pubacks[:-1])):
        raise InputError('Retained publisher advanced before its PUBACK')
    expected = {early: [(PAYLOADS[0], True), (PAYLOADS[1], False), (b'', False), (PAYLOADS[3], False), (PAYLOADS[4], False)],
                probe: [(PAYLOADS[0], True)], late: [(PAYLOADS[3], False), (PAYLOADS[4], False)], post: [(PAYLOADS[4], False)]}
    receipts = {}
    # Ignore the final cleanup delivery, but include every inbound publication
    # from subscription start through each receiver's completed proof window.
    cutoff = events.index(phases[5])
    for connection, allowed in expected.items():
        received, known = [], {}
        acknowledgments = [(i, body) for i, (kind, body) in tx.get(connection, []) if kind == 0x40]
        used_acknowledgments = set()
        grants = [(i, body) for i, (kind, body) in frames.get(connection, []) if kind == 0x90]
        requests = [(i, body) for i, (kind, body) in tx.get(connection, []) if kind == 0x82]
        if len(grants) != 1 or len(requests) != 1 or grants[0][1] != b'\x00\x09\x01' or requests[0][1] != b'\x00\x09' + struct.pack('!H', len(topic.encode())) + topic.encode() + b'\x01':
            raise InputError('Retained subscription/grant evidence mismatch')
        for index, frame in frames.get(connection, []):
            if index >= cutoff or frame[0] >> 4 != 3:
                continue
            value = publication(frame)
            signature = (value['topic'], value['payload'], value['qos'], value['retain'])
            duplicate = value['dup'] and known.get(value['mid']) == signature
            if value['topic'] != topic or value['qos'] != 1 or not value['mid'] or index <= requests[0][0]:
                raise InputError('Retained received publication is outside its subscription')
            acknowledgment = next((i for i, body in acknowledgments
                                   if i > index and i not in used_acknowledgments
                                   and body == struct.pack('!H', value['mid'])), None)
            if acknowledgment is None:
                raise InputError('Retained receiver PUBACK evidence missing')
            used_acknowledgments.add(acknowledgment)
            if not duplicate:
                received.append((index, (value['payload'], value['retain'])))
                known[value['mid']] = signature
        if [value for _, value in received] != allowed:
            raise InputError('Retained replay/live/delete/health sequence mismatch')
        receipts[connection] = received
    bounds = [receipts[early][0][0], receipts[probe][0][0], receipts[early][2][0],
              receipts[late][0][0], receipts[post][0][0], pubacks[-1][0]]
    if any(bound >= events.index(phase) for bound, phase in zip(bounds, phases)):
        raise InputError('Retained phase precedes its wire evidence')
    late_subscribe = next(i for i, (k, _) in tx[late] if k == 0x82)
    probe_subscribe = next(i for i, (k, _) in tx[probe] if k == 0x82)
    post_subscribe = next(i for i, (k, _) in tx[post] if k == 0x82)
    if not max(receipts[early][2][0], pubacks[2][0]) < late_subscribe:
        raise InputError('late Retained subscription lacks deletion-delivery barrier')
    if not max(receipts[early][1][0], pubacks[1][0]) < probe_subscribe:
        raise InputError('Retained preservation probe lacks live-delivery barrier')
    if not max(receipts[late][0][0], pubacks[3][0]) < post_subscribe:
        raise InputError('Retained post-control probe precedes its non-Retained control')
    observations = [event for event in events if event['kind'] == 'retained-observation']
    if [event['connection'] for event in observations] != [late, post]:
        raise InputError('Retained bounded observation missing or duplicated')
    for event, connection, phase in zip(observations, [late, post], phases[3:5]):
        if (event['topic'] != topic or event['completed'] is not True or event['seconds_required'] < 0.4
                or event['actual_seconds'] < event['seconds_required']
                or event['seconds'] - events[receipts[connection][0][0]]['seconds'] < event['seconds_required']
                or not receipts[connection][0][0] < events.index(event) < events.index(phase)):
            raise InputError('Retained bounded observation lacks valid positive control')
