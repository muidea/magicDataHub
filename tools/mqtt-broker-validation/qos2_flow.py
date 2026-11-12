"""Two outstanding QoS2 messages and repeated control packets, no reconnect."""
from collections import Counter
import select
import time
import uuid

from qos2 import mid_bytes, next_frame, send_qos2
from wire import Blocked, Client, publication, string

VERSION = '1.0.0'
CASE_ID = 'MQTT311-QOS2-FLOW-001'
MIDS = (0x2345, 0x2346)
PAYLOADS = (b'flow-first', b'flow-second', b'flow-healthy')
PHASES = ('input-pair-completed', 'outbound-pair-completed', 'health-completed', 'observation-completed')


def controls(client, kind, expected):
    remaining = Counter(expected)
    deadline = time.monotonic() + 5
    while +remaining:
        frame = next_frame(client, deadline)
        mid = int.from_bytes(frame[1], 'big')
        client.trace.check(frame[0] == kind and len(frame[1]) == 2 and remaining[mid] > 0,
                           'qos2-flow-control-packet')
        remaining[mid] -= 1


class Receiver:
    def __init__(self, client, topic):
        self.client, self.topic = client, topic
        self.pending, self.completed, self.deliveries = {}, set(), Counter()
        self.original_ids, self.releases = set(), Counter()

    def publish(self, frame, *, acknowledge):
        value = publication(frame)
        mid, payload = value['mid'], value['payload']
        self.client.trace.check(value['qos'] == 2 and not value['retain'] and mid not in (None, 0)
                                and value['topic'] == self.topic and payload in PAYLOADS,
                                'qos2-flow-publication')
        if mid in self.pending:
            state = self.pending[mid]
            self.client.trace.check(value['dup'] and state['payload'] == payload and not state['released'],
                                    'qos2-flow-inflight-id-content')
            state['publications'] += 1
        else:
            self.client.trace.check(not value['dup'], 'qos2-flow-new-dup0')
            self.pending[mid] = dict(payload=payload, publications=1, released=False)
            self.completed.discard(mid)
        if acknowledge:
            self.client.send(0x50, mid_bytes(mid))
        return mid

    def release(self, frame):
        kind, body = frame
        mid = int.from_bytes(body, 'big')
        self.client.trace.check(kind == 0x62 and len(body) == 2 and (mid in self.pending or mid in self.completed),
                                'qos2-flow-control-packet')
        if mid in self.pending:
            state = self.pending[mid]
            state['released'] = True
            payload = state['payload']
            self.deliveries[payload] += 1
            self.client.trace.check(self.deliveries[payload] == 1, 'qos2-flow-single-delivery')
            self.client.trace.event('application-delivery', connection=self.client.connection,
                                    packet_id=mid, topic=self.topic, payload_hex=payload.hex(), count=1)
            self.pending.pop(mid)
            self.completed.add(mid)
        self.client.send(0x70, body)
        self.releases[mid] += 1

    def step(self, deadline):
        frame = next_frame(self.client, deadline)
        if frame[0] >> 4 == 3:
            self.publish(frame, acknowledge=True)
        else:
            self.release(frame)

    def pair(self):
        deadline = time.monotonic() + 5
        for _ in range(128):
            before = self.client.trace.sequence
            try:
                frame = next_frame(self.client, deadline)
            except TimeoutError as error:
                # A serial sender is legal. Absence of this capability cannot
                # become a failed conformance claim; partial frames remain errors.
                if self.pending and self.client.trace.sequence == before:
                    raise Blocked('two simultaneous outbound QoS2 publications not established; selected probe unavailable') from error
                raise
            mid = self.publish(frame, acknowledge=False)
            self.original_ids.add(mid)
            if len(self.original_ids) == 2:
                break
        else:
            raise TimeoutError('QoS2 flow initial frame budget exceeded')
        self.client.trace.check(len(self.pending) == 2 and {s['payload'] for s in self.pending.values()} == set(PAYLOADS[:2]),
                                'qos2-flow-unique-inflight-ids')
        # Each captured PUBLISH gets a PUBREC, plus one explicit duplicate per
        # message, all queued before consuming any PUBREL.
        for mid, state in self.pending.items():
            for _ in range(state['publications'] + 1):
                self.client.send(0x50, mid_bytes(mid))
        for _ in range(128):
            if all(self.releases[mid] >= 2 for mid in self.original_ids) and not self.pending:
                return
            self.step(deadline)
        raise TimeoutError('QoS2 flow repeated release budget exceeded')

    def health(self):
        deadline = time.monotonic() + 5
        for _ in range(128):
            if self.deliveries[PAYLOADS[2]] == 1 and not self.pending:
                return
            self.step(deadline)
        raise TimeoutError('QoS2 flow health budget exceeded')

    def observe(self):
        start = time.monotonic()
        deadline = start + 0.4
        for _ in range(128):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self.client.pending:
                    raise TimeoutError('QoS2 flow pending observation budget exceeded')
                break
            if not self.client.pending and not select.select([self.client.stream], [], [], remaining)[0]:
                break
            self.step(deadline)
        else:
            raise TimeoutError('QoS2 flow observation frame budget exceeded')
        self.client.trace.check(self.deliveries == Counter(PAYLOADS) and not self.pending, 'qos2-flow-single-delivery')
        self.client.trace.event('observation', seconds_required=0.4, actual_seconds=time.monotonic() - start, completed=True)


def run(endpoint, trace):
    topic = 'validation/' + uuid.uuid4().hex + '/qos2-flow'
    trace.event('qos2-flow-start', scenario_version=VERSION, topic=topic)
    with Client(endpoint, trace) as publisher, Client(endpoint, trace) as subscriber:
        trace.event('qos2-flow-roles', publisher=publisher.connection, subscriber=subscriber.connection)
        subscriber.subscribe(topic, qos=2)
        for mid, payload in zip(MIDS, PAYLOADS):
            publisher.send(0x34, string(topic) + mid_bytes(mid) + payload)
        controls(publisher, 0x50, MIDS)
        for mid in MIDS:
            publisher.send(0x62, mid_bytes(mid))
            publisher.send(0x62, mid_bytes(mid))
        controls(publisher, 0x70, MIDS * 2)
        trace.event('qos2-flow-phase', phase=PHASES[0])
        receiver = Receiver(subscriber, topic)
        receiver.pair()
        trace.event('qos2-flow-phase', phase=PHASES[1])
        send_qos2(publisher, topic, PAYLOADS[2], MIDS[0])
        receiver.health()
        trace.event('qos2-flow-phase', phase=PHASES[2])
        receiver.observe()
        trace.event('qos2-flow-phase', phase=PHASES[3])
    trace.check(True, 'qos2-flow-completed')
