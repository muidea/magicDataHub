"""QoS2 duplicate input fanned out once to each of two clean subscribers."""
from collections import Counter
import select
import time
import uuid

from qos2 import mid_bytes, next_frame
from wire import Client, publication, string

CASE_ID = 'MQTT311-QOS2-FANOUT-001'
VERSION = '1.0.0'
MID = 0x3456
PAYLOADS = (b'fanout-original', b'fanout-healthy')
PHASES = ('original-input-completed', 'original-fanout-completed',
          'health-input-completed', 'health-fanout-completed', 'observations-completed')


def send_input(client, topic, payload, *, repeat):
    body = string(topic) + mid_bytes(MID) + payload
    client.send(0x34, body)
    if repeat:
        client.send(0x3c, body)
    deadline = time.monotonic() + 5
    for _ in range(1 + int(repeat)):
        client.trace.check(next_frame(client, deadline) == (0x50, mid_bytes(MID)), 'qos2-fanout-input-ack')
    client.send(0x62, mid_bytes(MID))
    client.trace.check(next_frame(client, deadline) == (0x70, mid_bytes(MID)), 'qos2-fanout-input-ack')


class Receiver:
    def __init__(self, client, topic):
        self.client, self.topic = client, topic
        self.pending, self.completed, self.deliveries = {}, set(), Counter()

    def step(self, deadline):
        kind, body = next_frame(self.client, deadline)
        if kind >> 4 == 3:
            value = publication((kind, body))
            mid, payload = value['mid'], value['payload']
            self.client.trace.check(value['qos'] == 2 and not value['retain'] and mid not in (None, 0)
                                    and value['topic'] == self.topic and payload in PAYLOADS,
                                    'qos2-fanout-publication')
            if mid in self.pending:
                self.client.trace.check(value['dup'] and self.pending[mid] == payload,
                                        'qos2-fanout-active-id')
            else:
                self.client.trace.check(not value['dup'] and not self.deliveries[payload],
                                        'qos2-fanout-new-publication')
                self.pending[mid] = payload
                self.completed.discard(mid)
            self.client.send(0x50, mid_bytes(mid))
        else:
            mid = int.from_bytes(body, 'big')
            self.client.trace.check(kind == 0x62 and len(body) == 2
                                    and (mid in self.pending or mid in self.completed),
                                    'qos2-fanout-output-ack')
            if mid in self.pending:
                payload = self.pending.pop(mid)
                self.deliveries[payload] += 1
                self.client.trace.check(self.deliveries[payload] == 1, 'qos2-fanout-single-delivery')
                self.client.trace.event('application-delivery', connection=self.client.connection,
                                        packet_id=mid, topic=self.topic, payload_hex=payload.hex(), count=1)
                self.completed.add(mid)
            self.client.send(0x70, body)

    def receive(self, payload):
        deadline = time.monotonic() + 5
        for _ in range(128):
            if self.deliveries[payload] == 1 and not self.pending:
                return
            self.step(deadline)
        raise TimeoutError('QoS2 fanout receive frame budget exceeded')

    def observe(self):
        self.client.trace.event('qos2-fanout-observation-start', connection=self.client.connection, seconds_required=0.4)
        start = time.monotonic()
        deadline = start + 0.4
        for _ in range(128):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self.client.pending:
                    raise TimeoutError('QoS2 fanout pending observation budget exceeded')
                break
            if not self.client.pending and not select.select([self.client.stream], [], [], remaining)[0]:
                break
            self.step(deadline)
        else:
            raise TimeoutError('QoS2 fanout observation frame budget exceeded')
        self.client.trace.check(self.deliveries == Counter(PAYLOADS) and not self.pending,
                                'qos2-fanout-single-delivery')
        self.client.trace.event('qos2-fanout-observation', connection=self.client.connection,
                                seconds_required=0.4, actual_seconds=time.monotonic() - start, completed=True)


def run(endpoint, trace):
    topic = 'validation/' + uuid.uuid4().hex + '/qos2-fanout'
    trace.event('qos2-fanout-start', scenario_version=VERSION, topic=topic)
    with Client(endpoint, trace) as publisher, Client(endpoint, trace) as a, Client(endpoint, trace) as b:
        trace.event('qos2-fanout-roles', publisher=publisher.connection, subscribers=[a.connection, b.connection])
        for client in [a, b]:
            client.subscribe(topic, qos=2)
        receivers = [Receiver(client, topic) for client in [a, b]]
        send_input(publisher, topic, PAYLOADS[0], repeat=True)
        trace.event('qos2-fanout-phase', phase=PHASES[0])
        for receiver in receivers:
            receiver.receive(PAYLOADS[0])
        trace.event('qos2-fanout-phase', phase=PHASES[1])
        send_input(publisher, topic, PAYLOADS[1], repeat=False)
        trace.event('qos2-fanout-phase', phase=PHASES[2])
        for receiver in reversed(receivers):
            receiver.receive(PAYLOADS[1])
        trace.event('qos2-fanout-phase', phase=PHASES[3])
        for receiver in receivers:
            receiver.observe()
        trace.event('qos2-fanout-phase', phase=PHASES[4])
    trace.check(True, 'qos2-fanout-completed')
