"""Selected clean MQTT3.1.1 connection path and independent wire proof."""
from common import InputError
from wire import Client, decode_frame, string

VERSION = '2.0.0'
PHASES = ('connack-accepted', 'pingresp-received', 'client-disconnected')
ASSERTIONS = ['valid-success-CONNACK', 'expected-control-packet', 'connect-socket-closed']


def run(endpoint, trace):
    client = Client(endpoint, trace)
    def phase(name):
        trace.event('connect-phase', phase=name, scenario_version=VERSION, connection=client.connection)
    try:
        phase(PHASES[0])
        client.send(0xc0)
        client.expect(0xd0, b'')
        phase(PHASES[1])
        client.close()
        trace.check(client.closed and client.stream.fileno() == -1, 'connect-socket-closed')
        phase(PHASES[2])
    finally:
        client.abort()


def check_evidence(events):
    phases = [(i, e) for i, e in enumerate(events) if e['kind'] == 'connect-phase']
    if [e['phase'] for _, e in phases] != list(PHASES) or any(e['scenario_version'] != VERSION for _, e in phases):
        raise InputError('missing, duplicated or reordered connection phase')
    connections = [e for e in events if e['kind'] == 'connection']
    if len(connections) != 1:
        raise InputError('connection proof requires exactly one connection')
    connection = connections[0]['connection']
    identity = connections[0].get('client_id', '')
    if not identity or any(e['connection'] != connection for _, e in phases):
        raise InputError('connection proof identity/phase mismatch')
    # Client uses a unique nonempty ASCII ID, clean session, no credentials or
    # Will, MQTT level 4, keepalive 30. These are this scenario's inputs only.
    expected_connect = b'\x00\x04MQTT\x04\x02\x00\x1e' + string(identity)
    tx, rx, buffer = [], [], b''
    for index, event in enumerate(events):
        if event['kind'] not in {'bytes', 'frame'}:
            continue
        if event['connection'] != connection:
            raise InputError('connection wire evidence belongs to another connection')
        raw = bytes.fromhex(event['hex'])
        if event['kind'] == 'bytes':
            if not raw:
                raise InputError('empty connection wire capture')
            if event['direction'] == 'tx':
                tx.append((index, decode_frame(raw)))
            else:
                buffer += raw
        else:
            if event['direction'] != 'rx' or raw != buffer:
                raise InputError('connection frame differs from captured receive bytes')
            rx.append((index, decode_frame(raw)))
            buffer = b''
    if buffer or [f for _, f in tx] != [(0x10, expected_connect), (0xc0, b''), (0xe0, b'')]:
        raise InputError('connection CONNECT/PINGREQ/DISCONNECT proof incomplete')
    if [f for _, f in rx] != [(0x20, b'\x00\x00'), (0xd0, b'')]:
        raise InputError('connection first CONNACK or PINGRESP proof invalid')
    order = [events.index(connections[0]), tx[0][0], rx[0][0], phases[0][0],
             tx[1][0], rx[1][0], phases[1][0], tx[2][0], phases[2][0]]
    if order != sorted(set(order)):
        raise InputError('connection exchange/phase order invalid')
    closed = [i for i, e in enumerate(events) if e['kind'] == 'assertion'
              and e.get('assertion') == 'connect-socket-closed' and e.get('passed') is True]
    if len(closed) != 1 or not tx[2][0] < closed[0] < phases[2][0]:
        raise InputError('connection local socket close evidence missing')
