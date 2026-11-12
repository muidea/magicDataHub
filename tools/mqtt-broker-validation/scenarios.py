"""Representative network scenarios, not a complete normative inventory."""
import queue
import socket
import threading
import uuid

from wire import Blocked, Client, ScopedTrace, read_frame


def connect(endpoint, trace):
    from connection import run
    run(endpoint, trace)


def retained(endpoint, trace):
    from retained import run
    run(endpoint, trace)


def topics(endpoint, trace):
    from topic_vectors import run
    run(endpoint, trace)


def system_topics(endpoint, trace):
    from topic_vectors import run
    run(endpoint, trace, system=True)


def paho(endpoint, trace):
    import paho.mqtt.client as mqtt

    class Recorded(mqtt.Client):
        def _sock_send(self, buf):
            count = super()._sock_send(buf)
            trace.event('bytes', connection=self._client_id.decode(), direction='tx', hex=bytes(buf[:count]).hex())
            return count

        def _sock_recv(self, count):
            value = super()._sock_recv(count)
            trace.event('bytes', connection=self._client_id.decode(), direction='rx', hex=value.hex())
            return value

    clients = []

    def client():
        connected, subscriptions, messages = queue.Queue(), queue.Queue(), queue.Queue()
        value = Recorded(mqtt.CallbackAPIVersion.VERSION2,
                         client_id='mv-' + uuid.uuid4().hex[:18], protocol=mqtt.MQTTv311)
        value.on_connect = lambda _c, _u, _f, reason, _p: connected.put(reason)
        value.on_subscribe = lambda _c, _u, _m, reasons, _p: subscriptions.put(reasons)
        value.on_message = lambda _c, _u, message: messages.put(message)
        clients.append(value)
        value.connect(*endpoint, keepalive=10)
        value.loop_start()
        trace.check(connected.get(timeout=5) == 0, 'paho-CONNACK')
        return value, subscriptions, messages

    root = 'validation/' + uuid.uuid4().hex
    publisher = None
    try:
        subscriber, acks, received = client()
        subscriber.subscribe(root + '/+', qos=2)
        trace.check([x.value for x in acks.get(timeout=5)] == [2], 'paho-SUBACK')
        publisher, _, _ = client()
        for qos in (0, 1, 2):
            payload = ('payload-' + str(qos)).encode()
            sent = publisher.publish(root + '/flow', payload, qos=qos)
            sent.wait_for_publish(timeout=5)
            trace.check(sent.is_published(), 'paho-publish-handshake', qos=qos)
            message = received.get(timeout=5)
            trace.check((message.topic, message.payload, message.qos, message.retain) ==
                         (root + '/flow', payload, qos, False), 'paho-delivery', qos=qos)
        sent = publisher.publish(root + '/retained', b'retained', qos=2, retain=True)
        sent.wait_for_publish(timeout=5)
        trace.check(sent.is_published(), 'paho-retained-publish-complete')
        message = received.get(timeout=5)
        trace.check(message.payload == b'retained' and not message.retain, 'paho-retained-live')
        late, subscriptions, replay = client()
        late.subscribe(root + '/retained', qos=2)
        trace.check([x.value for x in subscriptions.get(timeout=5)] == [2], 'paho-late-SUBACK')
        message = replay.get(timeout=5)
        trace.check(message.payload == b'retained' and message.retain, 'paho-retained-replay')
        sent = publisher.publish(root + '/retained', b'', qos=1, retain=True)
        sent.wait_for_publish(timeout=5)
        trace.check(sent.is_published(), 'paho-retained-delete-complete')
        message = replay.get(timeout=5)
        trace.check(message.payload == b'' and not message.retain, 'paho-delete-live')
    finally:
        if publisher is not None and publisher.is_connected():
            sent = publisher.publish(root + '/retained', b'', qos=1, retain=True)
            sent.wait_for_publish(timeout=5)
            trace.check(sent.is_published(), 'paho-retained-cleanup')
        for value in reversed(clients):
            value.disconnect()
            value.loop_stop()


def qos2_recovery(endpoint, trace):
    from qos2 import recovery
    recovery(endpoint, trace)


def calibration(trace):
    """Real loopback peers inject single faults; exact assertions must reject."""
    scripts = [
        ('wrong-CONNACK', [(0x10, b'\x20\x02\x01\x00')]),
        ('wrong-PINGRESP', [(0x10, b'\x20\x02\x00\x00'), (0xc0, b'\xd1\x00')]),
        ('wrong-PUBREC-id', [(0x10, b'\x20\x02\x00\x00'), (0x34, b'\x50\x02\x00\x18')]),
        ('wrong-topic', [(0x10, b'\x20\x02\x00\x00'),
                         (0x82, b'\x90\x03\x00\x09\x01\x30\x0c\x00\x05wrongvalue')]),
        ('stale-retained', [(0x10, b'\x20\x02\x00\x00'),
                            (0x82, b'\x90\x03\x00\x09\x01\x31\x0a\x00\x05topicold')]),
    ]
    parent_trace = trace
    for name, steps in scripts:
        trace = ScopedTrace(parent_trace, mutation=name)
        errors = []
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(5)

            def peer():
                try:
                    stream, _ = listener.accept()
                    with stream:
                        stream.settimeout(5)
                        for expected, response in steps:
                            frame = read_frame(stream)
                            if frame[0] != expected:
                                raise RuntimeError('calibration peer unexpected input')
                            trace.event('calibration-peer', mutation=name, sent_hex=response.hex())
                            stream.sendall(response)
                        # Hold connection until the client closes, so EOF is not the mutation.
                        stream.recv(1024)
                except Exception as error:
                    errors.append(error)
            worker = threading.Thread(target=peer)
            worker.start()
            rejected = False
            try:
                with Client(listener.getsockname(), trace) as value:
                    if name == 'wrong-PINGRESP':
                        value.send(0xc0)
                        value.expect(0xd0, b'')
                    elif name == 'wrong-PUBREC-id':
                        value.publish('topic', b'x', qos=2)
                    elif name == 'wrong-topic':
                        value.subscribe('topic')
                        value.delivery('topic', b'value')
                    elif name == 'stale-retained':
                        value.subscribe('topic')
                        value.no_message()
            except AssertionError:
                rejected = True
            worker.join(timeout=6)
            trace.check(not worker.is_alive() and not errors, 'calibration-peer-completed', mutation=name)
            trace.check(rejected, 'known-mutation-detected', mutation=name)

    from qos2 import recovery_calibration
    recovery_calibration(parent_trace)


def qos2_flow(endpoint, trace):
    from qos2_flow import run
    run(endpoint, trace)


def mixed_qos(endpoint, trace):
    from mixed_qos import run
    run(endpoint, trace)


def qos2_fanout(endpoint, trace):
    from qos2_fanout import run
    run(endpoint, trace)


def qos2_duplex(endpoint, trace):
    from qos2_duplex import run
    run(endpoint, trace)
