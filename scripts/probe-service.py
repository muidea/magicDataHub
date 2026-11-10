#!/usr/bin/env python3
"""External loopback protocol probe for the first delivery profile (Python standard library)."""
import argparse
import http.client
import ipaddress
import json
import os
import socket
import struct
import sys
import uuid

from acceptance.pg import Client


def address(value):
    host, port = value.rsplit(':', 1)
    host = host.strip('[]')
    if not ipaddress.ip_address(host).is_loopback or not 1 <= int(port) <= 65535:
        raise argparse.ArgumentTypeError('probe requires a numeric loopback address and valid port')
    return host, int(port)


def exact(stream, count):
    data = b''
    while len(data) < count:
        chunk = stream.recv(count - len(data))
        if not chunk:
            raise ValueError('probe protocol EOF')
        data += chunk
    return data


def mqtt(endpoint):
    with socket.create_connection(endpoint, timeout=5) as stream:
        client = ('mdh-probe-' + uuid.uuid4().hex[:12]).encode()
        body = b'\x00\x04MQTT\x04\x02\x00\x0a' + struct.pack('!H', len(client)) + client
        stream.sendall(b'\x10' + bytes([len(body)]) + body)
        assert exact(stream, 4) == b'\x20\x02\x00\x00'
        stream.sendall(b'\xc0\x00')
        assert exact(stream, 2) == b'\xd0\x00'
        stream.sendall(b'\xe0\x00')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mqtt', type=address)
    parser.add_argument('--pgwire', type=address)
    parser.add_argument('--management', type=address)
    parser.add_argument('--database', default='fedengine')
    parser.add_argument('--user', default='fedengine')
    parser.add_argument('--password-env', default='FEDENGINE_PASSWORD')
    args = parser.parse_args()
    assert any([args.mqtt, args.pgwire, args.management]), 'select at least one protocol'
    if args.mqtt:
        mqtt(args.mqtt)
    if args.pgwire:
        password = os.environ[args.password_env]
        assert password, 'password environment variable is empty'
        with Client(args.pgwire, database=args.database, user=args.user, password=password) as client:
            assert client.query('SELECT 1') == [['1']]
    if args.management:
        connection = http.client.HTTPConnection(*args.management, timeout=5)
        try:
            connection.request('GET', '/api/v1/sources')
            response = connection.getresponse()
            assert response.status == 200
            body = response.read(1024 * 1024 + 1)
            assert len(body) <= 1024 * 1024 and isinstance(json.loads(body), list)
        finally:
            connection.close()
    print('Ready: selected protocol probes passed')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not print remote bodies, SQL, credentials, or the environment.
        print('NotReady: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
