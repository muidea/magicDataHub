#!/usr/bin/env python3
"""MQTT3.1.1 first-profile acceptance against the real mqtt-server binary."""
import argparse
import importlib.util
import os
import json
import subprocess
from pathlib import Path
import signal
import socket
import struct
import tempfile
import time

spec = importlib.util.spec_from_file_location('smoke', Path(__file__).with_name('smoke-services.py'))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def string(value):
    value = value.encode() if isinstance(value, str) else value
    return struct.pack('!H', len(value)) + value


class Client:
    def __init__(self, address, name, clean=True, will=None):
        self.stream = socket.create_connection(address, timeout=5)
        flags = (2 if clean else 0) | (12 if will else 0)  # Will QoS1
        body = b'\x00\x04MQTT\x04' + bytes([flags]) + b'\x00\x0a' + string(name)
        if will:
            body += string(will[0]) + string(will[1])
        self.send(0x10, body)
        kind, body = self.receive()
        assert kind == 0x20 and body[1] == 0
        self.present = body[0]

    def send(self, kind, body=b''):
        size, remaining = len(body), b''
        while True:
            byte = size % 128
            size //= 128
            remaining += bytes([byte | (128 if size else 0)])
            if not size:
                break
        self.stream.sendall(bytes([kind]) + remaining + body)

    def receive(self):
        kind = smoke.exact(self.stream, 1)[0]
        size, shift = 0, 0
        for _ in range(4):
            byte = smoke.exact(self.stream, 1)[0]
            size += (byte & 127) << shift
            if not byte & 128:
                assert size <= 1024 * 1024
                return kind, smoke.exact(self.stream, size)
            shift += 7
        raise AssertionError('invalid MQTT remaining length')

    def subscribe(self, topic, qos=2):
        self.send(0x82, b'\x00\x01' + string(topic) + bytes([qos]))
        assert self.receive() == (0x90, b'\x00\x01' + bytes([qos]))

    def publish(self, topic, payload, qos, retain=False):
        body = string(topic) + (b'\x00\x02' if qos else b'') + payload
        self.send(0x30 | (qos << 1) | int(retain), body)
        if qos == 1:
            assert self.receive() == (0x40, b'\x00\x02')
        elif qos == 2:
            assert self.receive() == (0x50, b'\x00\x02')
            self.send(0x62, b'\x00\x02')
            assert self.receive() == (0x70, b'\x00\x02')

    def delivery(self, topic, payload, qos, retained=False):
        kind, body = self.receive()
        assert kind & 0xf0 == 0x30 and (kind >> 1) & 3 == qos
        assert bool(kind & 1) == retained
        length = struct.unpack('!H', body[:2])[0]
        assert body[2:length + 2].decode() == topic
        position = length + 2
        if qos:
            packet = body[position:position + 2]
            position += 2
        assert body[position:] == payload
        if qos == 1:
            self.send(0x40, packet)
        elif qos == 2:
            self.send(0x50, packet)
            assert self.receive() == (0x62, packet)
            self.send(0x70, packet)

    def ping(self):
        self.send(0xc0)
        assert self.receive() == (0xd0, b'')

    def close(self, graceful=True):
        if graceful:
            self.send(0xe0)
        self.stream.close()


def verify(address):
    subscriber = Client(address, 'subscriber', clean=False)
    publisher = Client(address, 'publisher')
    clients = [subscriber, publisher]
    try:
        subscriber.subscribe('acceptance/flow')
        for qos in (0, 1, 2):
            publisher.publish('acceptance/flow', bytes([qos]), qos)
            subscriber.delivery('acceptance/flow', bytes([qos]), qos)
        # A third connection is rejected while the configured two are live.
        with socket.create_connection(address, timeout=5) as excess:
            assert excess.recv(1) == b''
        subscriber.ping()
        publisher.publish('acceptance/retained', b'retained', 1, retain=True)
        subscriber.subscribe('acceptance/retained')
        subscriber.delivery('acceptance/retained', b'retained', 1, retained=True)
        publisher.publish('acceptance/retained', b'', 1, retain=True)
        subscriber.delivery('acceptance/retained', b'', 1)
        # Persistent session survives a connection loss, queues a reliable publish.
        subscriber.close()
        time.sleep(0.05)
        publisher.publish('acceptance/flow', b'offline', 1)
        subscriber = Client(address, 'subscriber', clean=False)
        clients.append(subscriber)
        assert subscriber.present == 1
        subscriber.delivery('acceptance/flow', b'offline', 1)
        # Clean reconnect removes subscriptions and must not replay the deleted retained value.
        subscriber.close()
        time.sleep(0.05)
        subscriber = Client(address, 'subscriber', clean=True)
        clients.append(subscriber)
        assert subscriber.present == 0
        subscriber.subscribe('acceptance/retained')
        subscriber.ping()  # PINGRESP must be next; retained record was deleted.
        subscriber.subscribe('acceptance/will')
        publisher.close()
        time.sleep(0.05)
        publisher = Client(address, 'will-publisher', will=('acceptance/will', b'last-will'))
        clients.append(publisher)
        publisher.close(graceful=False)
        subscriber.delivery('acceptance/will', b'last-will', 1)
        subscriber.ping()
    finally:
        for client in clients:
            client.stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path)
    args = parser.parse_args()
    if args.binary is None:
        graph = json.loads(subprocess.check_output([
            'cargo', 'metadata', '--format-version=1', '--no-deps', '--locked', '--offline',
            '--manifest-path', str(smoke.ROOT / 'Cargo.toml')], text=True))
        target = Path(graph['target_directory'])
        if os.environ.get('CARGO_BUILD_TARGET'):
            target /= os.environ['CARGO_BUILD_TARGET']
        args.binary = target / 'debug/mqtt-server'
    with tempfile.TemporaryDirectory(prefix='mdh-mqtt-acceptance-') as temporary:
        directory = Path(temporary)
        (directory / 'host.toml').write_text('[application]\nname="mqtt-acceptance"\n[mqtt]\nconfig_file="mqtt.toml"\n')
        (directory / 'mqtt.toml').write_text('[mqtt.core]\nenabled=true\nlistener="127.0.0.1:0"\nmax_connections=2\nmax_sessions=4\n[mqtt.security]\nanonymous_access=true\n')
        process = smoke.Process(args.binary.resolve(), directory / 'host.toml', '/', dict(os.environ, RUST_LOG='info'))
        try:
            address = process.ready()['mqtt']
            verify(address)
            process.stop(signal.SIGTERM)
            with socket.socket() as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(address)
            assert sorted(p.name for p in directory.iterdir()) == ['host.toml', 'mqtt.toml']
        finally:
            process.close()
    print('mqtt-server QoS0/1/2, subscription, retained/delete, Will, persistent/clean reconnect, connection quota and drain: Passed')


if __name__ == '__main__':
    main()
