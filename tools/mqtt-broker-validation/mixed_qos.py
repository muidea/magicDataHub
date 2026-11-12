"""Finite QoS negotiation matrix for independent subscriber sessions."""
from collections import Counter
import select
import time
import uuid

from qos2 import mid_bytes, next_frame
from wire import Blocked, Client, publication, string

CASE_ID = 'MQTT311-MIXED-QOS-001'
VERSION = '1.0.0'
VECTORS = [(p, (s,)) for p in range(3) for s in range(3)]
PAYLOAD = b'mixed-qos-message'
WINDOW = 0.4


def filters(topic, count):
    if count != 1:
        raise ValueError('overlapping filters are kept in a separate diagnostic, not the required matrix')
    return [topic]


class Receiver:
    def __init__(self, client, topic, input_qos, grants):
        self.client, self.topic, self.input_qos = client, topic, input_qos
        self.capacity = Counter(min(input_qos, g) for g in grants)
        self.expected = max(self.capacity)
        self.used, self.received, self.states = Counter(), Counter(), {}

    def deliver(self, qos, mid):
        self.received[qos] += 1
        self.client.trace.event('mixed-delivery', connection=self.client.connection, qos=qos,
                                packet_id=mid, topic=self.topic, payload_hex=PAYLOAD.hex())

    def step(self, deadline):
        kind, body = next_frame(self.client, deadline)
        if kind >> 4 == 3:
            value = publication((kind, body))
            qos, mid = value['qos'], value['mid']
            self.client.trace.check(value['topic'] == self.topic and value['payload'] == PAYLOAD
                                    and not value['retain'] and qos in self.capacity
                                    and (mid not in (None, 0) if qos else not value['dup']), 'mixed-publication')
            prior = self.states.get(mid) if qos else None
            retry = bool(value['dup'])
            if retry:
                self.client.trace.check(prior is not None and prior['qos'] == qos
                                        and (qos == 1 or prior['phase'] == 'publish'), 'mixed-retry')
            else:
                self.client.trace.check(prior is None or prior['phase'] == 'done', 'mixed-active-id')
                self.used[qos] += 1
                # QoS1 -> QoS0 explicitly permits duplicate copies (§3.8.4).
                self.client.trace.check((self.input_qos == 1 and qos == 0)
                                        or self.used[qos] <= self.capacity[qos], 'mixed-copy-budget')
                if qos:
                    self.states[mid] = dict(qos=qos, phase='publish')
            if qos == 2:
                self.client.send(0x50, mid_bytes(mid))
            else:
                self.deliver(qos, mid)
                if qos == 1:
                    self.client.send(0x40, mid_bytes(mid))
                    self.states[mid]['phase'] = 'done'
        else:
            mid = int.from_bytes(body, 'big')
            state = self.states.get(mid)
            self.client.trace.check(kind == 0x62 and len(body) == 2 and state is not None
                                    and state['qos'] == 2, 'mixed-output-ack')
            if state['phase'] != 'done':
                self.deliver(2, mid)
                state['phase'] = 'done'
            self.client.send(0x70, body)

    def run(self):
        deadline = time.monotonic() + 5
        for _ in range(128):
            if self.received[self.expected]:
                break
            # A missing best-effort delivery cannot prove a protocol violation.
            if not self.client.pending and not select.select(
                    [self.client.stream], [], [], max(0, deadline - time.monotonic()))[0]:
                if self.expected == 0:
                    raise Blocked('selected best-effort positive delivery not observed')
                self.client.trace.check(False, 'mixed-required-qos-positive-control')
            self.step(deadline)
        else:
            raise TimeoutError('mixed QoS receive frame budget exceeded')
        self.client.trace.event('mixed-observation-start', connection=self.client.connection, seconds_required=WINDOW)
        start = time.monotonic()
        deadline = start + WINDOW
        for _ in range(128):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self.client.pending:
                    raise TimeoutError('mixed QoS pending observation budget exceeded')
                break
            if not self.client.pending and not select.select([self.client.stream], [], [], remaining)[0]:
                break
            self.step(deadline)
        else:
            raise TimeoutError('mixed QoS observation frame budget exceeded')
        self.client.trace.check(self.received[self.expected] > 0 and all(
            s['phase'] == 'done' for s in self.states.values()), 'mixed-delivery-complete')
        self.client.trace.event('mixed-observation', connection=self.client.connection, seconds_required=WINDOW,
                                actual_seconds=time.monotonic() - start, completed=True)


def run(endpoint, trace):
    for index, (input_qos, requests) in enumerate(VECTORS):
        topic = 'validation/' + uuid.uuid4().hex + '/mixed-qos/item'
        trace.event('mixed-vector-start', index=index, scenario_version=VERSION, topic=topic)
        with Client(endpoint, trace) as publisher, Client(endpoint, trace) as subscriber:
            trace.event('mixed-roles', index=index, publisher=publisher.connection, subscriber=subscriber.connection)
            grants = []
            for mid, (topic_filter, requested) in enumerate(zip(filters(topic, len(requests)), requests), 9):
                subscriber.send(0x82, mid_bytes(mid) + string(topic_filter) + bytes([requested]))
                kind, body = next_frame(subscriber, time.monotonic() + 5)
                trace.check(kind == 0x90 and len(body) == 3 and body[:2] == mid_bytes(mid)
                            and (body[2] == 0x80 or body[2] <= requested), 'mixed-suback')
                if body[2] == 0x80:
                    raise Blocked('selected subscription rejected; positive QoS matrix unavailable')
                grants.append(body[2])
            publisher.publish(topic, PAYLOAD, qos=input_qos)
            trace.event('mixed-input-completed', index=index)
            Receiver(subscriber, topic, input_qos, grants).run()
        trace.event('mixed-vector-completed', index=index)
    trace.check(True, 'mixed-matrix-completed')
