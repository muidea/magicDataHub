"""One TCP connection owns the same Packet ID in both QoS2 directions."""
from collections import Counter
import select
import time
import uuid

from qos2 import mid_bytes, next_frame
from wire import Client, publication, string

CASE_ID = 'MQTT311-QOS2-DUPLEX-001'
VERSION = '1.0.0'
BRANCHES = ('incoming-first', 'outgoing-first')
PHASES = ('overlap-established', 'first-direction-completed', 'both-directions-completed',
          'health-completed', 'observations-completed')
INPUTS = (b'input-original', b'input-healthy')
OUTPUTS = (b'output-original', b'output-healthy')
SOURCE_IDS = (0x4567, 0x4568)


class Receiver:
    def __init__(self, client, topic, payloads, *, enabled=True):
        self.client, self.topic, self.payloads, self.enabled = client, topic, payloads, enabled
        self.pending, self.completed, self.deliveries = {}, set(), Counter()

    def publication(self, frame):
        value = publication(frame); mid, payload = value['mid'], value['payload']
        self.client.trace.check(value['qos'] == 2 and not value['retain'] and mid not in (None, 0)
                                and value['topic'] == self.topic and payload in self.payloads,
                                'qos2-duplex-publication')
        if mid in self.pending:
            state = self.pending[mid]
            self.client.trace.check(value['dup'] and state['payload'] == payload, 'qos2-duplex-active-id')
            state['publications'] += 1
        else:
            self.client.trace.check(not value['dup'] and not self.deliveries[payload], 'qos2-duplex-new-publication')
            self.pending[mid] = dict(payload=payload, publications=1, recs=0)
            self.completed.discard(mid)
        if self.enabled:
            self.acknowledge()
        return mid

    def acknowledge(self):
        self.enabled = True
        for mid, state in self.pending.items():
            while state['recs'] < state['publications']:
                self.client.send(0x50, mid_bytes(mid)); state['recs'] += 1

    def step(self, frame):
        kind, body = frame
        if kind >> 4 == 3:
            return self.publication(frame)
        mid = int.from_bytes(body, 'big')
        self.client.trace.check(kind == 0x62 and len(body) == 2
                                and (mid in self.pending and self.pending[mid]['recs'] > 0 or mid in self.completed),
                                'qos2-duplex-output-ack')
        if mid in self.pending:
            payload = self.pending.pop(mid)['payload']; self.completed.add(mid)
            self.deliveries[payload] += 1
            self.client.trace.check(self.deliveries[payload] == 1, 'qos2-duplex-single-delivery')
            self.client.trace.event('application-delivery', connection=self.client.connection, packet_id=mid,
                                    topic=self.topic, payload_hex=payload.hex(), count=1)
        self.client.send(0x70, body)

    def first(self):
        frame = next_frame(self.client, time.monotonic() + 5)
        self.client.trace.check(frame[0] >> 4 == 3, 'qos2-duplex-publication')
        return self.publication(frame)

    def receive(self, payload):
        self.acknowledge()
        deadline = time.monotonic() + 5
        for _ in range(128):
            if self.deliveries[payload] == 1 and not self.pending:
                return
            self.step(next_frame(self.client, deadline))
        raise TimeoutError('QoS2 duplex receiver frame budget exceeded')

    def control(self, kind, mid):
        deadline = time.monotonic() + 5
        for _ in range(128):
            frame = next_frame(self.client, deadline)
            if frame[0] >> 4 == 3 or frame[0] == 0x62:
                self.step(frame)
            else:
                self.client.trace.check(frame == (kind, mid_bytes(mid)), 'qos2-duplex-input-ack')
                return
        raise TimeoutError('QoS2 duplex input ACK frame budget exceeded')

    def observe(self, branch):
        self.client.trace.event('qos2-duplex-observation-start', branch=branch,
                                connection=self.client.connection, seconds_required=0.4)
        start = time.monotonic(); deadline = start + 0.4
        for _ in range(128):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self.client.pending:
                    raise TimeoutError('QoS2 duplex pending observation budget exceeded')
                break
            if not self.client.pending and not select.select([self.client.stream], [], [], remaining)[0]:
                break
            self.step(next_frame(self.client, deadline))
        else:
            raise TimeoutError('QoS2 duplex observation frame budget exceeded')
        self.client.trace.check(self.deliveries == Counter(self.payloads) and not self.pending,
                                'qos2-duplex-single-delivery')
        self.client.trace.event('qos2-duplex-observation', branch=branch, connection=self.client.connection,
                                seconds_required=0.4, actual_seconds=time.monotonic() - start, completed=True)


def publish_start(receiver, topic, payload, mid):
    receiver.client.send(0x34, string(topic) + mid_bytes(mid) + payload)
    receiver.control(0x50, mid)


def publish_finish(receiver, mid):
    receiver.client.send(0x62, mid_bytes(mid)); receiver.control(0x70, mid)


def run(endpoint, trace):
    for branch in BRANCHES:
        topic = 'validation/' + uuid.uuid4().hex + '/qos2-duplex/' + branch
        trace.event('qos2-duplex-branch-start', branch=branch, topic=topic, scenario_version=VERSION)
        with Client(endpoint, trace) as owner, Client(endpoint, trace) as helper:
            trace.event('qos2-duplex-roles', branch=branch, owner=owner.connection, helper=helper.connection)
            owner.subscribe(topic + '/out', qos=2); helper.subscribe(topic + '/in', qos=2)
            down = Receiver(owner, topic + '/out', OUTPUTS, enabled=False)
            up = Receiver(helper, topic + '/in', INPUTS)
            # Do not require the helper's PUBCOMP before onward delivery.
            publish_start(up, topic + '/out', OUTPUTS[0], SOURCE_IDS[0])
            helper.send(0x62, mid_bytes(SOURCE_IDS[0]))
            mid = down.first()  # Learn, rather than prescribe, the Broker's ID.
            publish_start(down, topic + '/in', INPUTS[0], mid)
            trace.event('qos2-duplex-phase', branch=branch, phase=PHASES[0])
            if branch == 'incoming-first':
                publish_finish(down, mid)
            else:
                down.receive(OUTPUTS[0])
            trace.event('qos2-duplex-phase', branch=branch, phase=PHASES[1])
            if branch == 'incoming-first':
                down.receive(OUTPUTS[0])
            else:
                publish_finish(down, mid)
            trace.event('qos2-duplex-phase', branch=branch, phase=PHASES[2])
            up.control(0x70, SOURCE_IDS[0]); up.receive(INPUTS[0])
            publish_start(down, topic + '/in', INPUTS[1], mid); publish_finish(down, mid)
            up.receive(INPUTS[1])
            publish_start(up, topic + '/out', OUTPUTS[1], SOURCE_IDS[1])
            helper.send(0x62, mid_bytes(SOURCE_IDS[1]))
            down.receive(OUTPUTS[1]); up.control(0x70, SOURCE_IDS[1])
            trace.event('qos2-duplex-phase', branch=branch, phase=PHASES[3])
            down.observe(branch); up.observe(branch)
            trace.event('qos2-duplex-phase', branch=branch, phase=PHASES[4])
        trace.event('qos2-duplex-branch-completed', branch=branch)
    trace.check(True, 'qos2-duplex-completed')
