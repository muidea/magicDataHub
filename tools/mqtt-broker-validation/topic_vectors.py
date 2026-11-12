"""Versioned Topic matching probes and duplicate-aware QoS1 receive oracle."""
import select
import socket
import struct
import time
import uuid

from wire import Blocked, Client, publication

VERSION = '2.0.0'
# Explicit expected results, independent of any product topic matcher.
GENERAL = (
    ('a/b', 'a/b', True), ('a/+', 'a/b', True), ('a/+', 'a/b/c', False),
    ('a/#', 'a', True), ('a/#', 'a/b/c', True), ('a/+/c', 'a//c', True),
    ('#', '$validation/test', False), ('+', '$validation', False),
    ('$validation/#', '$validation/test', True),
    ('a/B', 'a/b', False), ('a/b', 'a/b/', False),
    ('a/+', 'a/', True), ('a/+', 'a', False),
    ('a//b', 'a/b', False), ('caf\u00e9', 'cafe\u0301', False),
    ('a/+/+', 'a//b', True),
)
SYSTEM = (('#', '$SYS/validation', False), ('+', '$SYS', False),
          ('$SYS/#', '$SYS/validation', True))
PHASES = tuple('vector-' + str(i) for i in range(len(GENERAL)))


def materialize(row, root):
    pattern, topic, matches = row
    if not topic.startswith('$'):
        pattern, topic = root + '/' + pattern, root + '/' + topic
    elif topic not in {'$SYS', '$validation'}:
        topic += '/' + root.split('/')[1]
    # Always prove that the same candidate subscription can receive a match.
    control = topic if matches else '/'.join('probe' if p == '+' else p for p in pattern.split('/') if p != '#')
    if pattern == '#':
        control = root + '/probe'
    elif pattern == '+':
        control = root.replace('/', '-')
    return pattern, topic, matches, control


class Receiver:
    def __init__(self, client):
        self.client, self.seen = client, {}

    def consume(self, frame, expected=None):
        value = publication(frame)
        signature = (value['topic'], value['payload'])
        duplicate = value['dup'] and self.seen.get(value['mid']) == signature
        self.client.trace.check(value['qos'] == 1 and not value['retain'] and bool(value['mid'])
                                and (duplicate or signature == expected), 'topic-delivery')
        self.client.send(0x40, struct.pack('!H', value['mid']))
        self.seen[value['mid']] = signature
        return not duplicate

    def delivery(self, topic, payload):
        deadline = time.monotonic() + 5
        timeout = self.client.stream.gettimeout()
        try:
            for _ in range(128):
                if time.monotonic() >= deadline:
                    break
                frame = self.client.pending.popleft() if self.client.pending else self.client.recv(deadline=deadline)
                if self.consume(frame, (topic, payload)):
                    return
            raise TimeoutError('Topic delivery frame/time budget exceeded')
        finally:
            self.client.stream.settimeout(timeout)

    def observe(self):
        start = time.monotonic()
        deadline = start + 0.4
        timeout = self.client.stream.gettimeout()
        try:
            for _ in range(128):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self.client.pending:
                        raise TimeoutError('Topic pending frames exceed observation budget')
                    break
                if not self.client.pending and not select.select([self.client.stream], [], [], remaining)[0]:
                    break
                frame = self.client.pending.popleft() if self.client.pending else self.client.recv(deadline=deadline)
                self.consume(frame)
            else:
                raise TimeoutError('Topic observation frame budget exceeded')
            self.client.trace.check(True, 'topic-observation-completed')
            self.client.trace.event('topic-observation', connection=self.client.connection,
                                    seconds_required=0.4, actual_seconds=time.monotonic() - start, completed=True)
        finally:
            self.client.stream.settimeout(timeout)


def run(endpoint, trace, *, system=False):
    rows = SYSTEM if system else GENERAL
    trace.event('planned-topic-vectors', vectors=rows)
    for index, row in enumerate(rows):
        root = 'validation/' + uuid.uuid4().hex
        pattern, topic, matches, control = materialize(row, root)
        trace.event('topic-vector-start', scenario_version=VERSION, index=index, root=root,
                    filter=pattern, topic=topic, matches=matches, control_topic=control)
        with Client(endpoint, trace) as publisher, Client(endpoint, trace) as candidate, Client(endpoint, trace) as positive:
            trace.event('topic-vector-roles', publisher=publisher.connection,
                        candidate=candidate.connection, positive=positive.connection)
            receiver, exact = Receiver(candidate), Receiver(positive)
            candidate.subscribe(pattern)
            publisher.publish(control, ('control-' + str(index)).encode())
            try:
                receiver.delivery(control, ('control-' + str(index)).encode())
            except socket.timeout as error:
                if system and control.startswith('$SYS'):
                    raise Blocked('system Topic positive control unavailable; cannot prove namespace writable') from error
                raise
            positive.subscribe(topic)
            marker = ('topic-' + str(index)).encode()
            publisher.publish(topic, marker)
            try:
                exact.delivery(topic, marker)
            except socket.timeout as error:
                if system:
                    raise Blocked('system Topic positive control unavailable; cannot prove namespace writable') from error
                raise
            if matches:
                receiver.delivery(topic, marker)
            receiver.observe()
            exact.observe()
        trace.event('topic-vector', index=index, filter=pattern, topic=topic, matches=matches)
