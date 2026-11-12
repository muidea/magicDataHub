"""Rebuild the selected four recovery branches from actual wire evidence.

The receiver replay models the test client's delivery-at-PUBREL algorithm;
it does not force the broker to forward at a particular ACK boundary.
"""
from collections import Counter, defaultdict

from calibration_model import RECOVERY_BRANCHES
from common import InputError
from qos2 import VERSION, mid_bytes
from wire import decode_frame, packet, publication, string


def require(value, message):
    if not value:
        raise InputError('QoS2 wire proof: ' + message)


def one(events, kind):
    rows = [(i, e) for i, e in enumerate(events) if e['kind'] == kind]
    require(len(rows) == 1, 'missing or duplicate ' + kind)
    return rows[0]


def capture(events):
    connections, frames, buffers = {}, defaultdict(list), defaultdict(bytes)
    for i, event in enumerate(events):
        kind = event['kind']
        if kind == 'connection':
            connection = event['connection']
            require(connection not in connections, 'duplicate connection')
            connections[connection] = (i, event)
        elif kind in {'bytes', 'frame'}:
            connection = event['connection']
            require(connection in connections, 'bytes before connection or unknown connection')
            raw = bytes.fromhex(event['hex'])
            require(bool(raw), 'empty wire capture')
            if kind == 'bytes' and event['direction'] == 'rx':
                buffers[connection] += raw
            else:
                if kind == 'frame':
                    require(event['direction'] == 'rx' and buffers[connection] == raw, 'RX frame/raw mismatch')
                    buffers[connection] = b''
                frames[connection].append((i, event['direction'], decode_frame(raw)))
    require(not any(buffers.values()), 'partial or undecoded receive capture')
    return connections, frames


def handshake(connection, connections, frames, *, clean, present, graceful):
    rows = frames[connection]
    tx = [(i, f) for i, d, f in rows if d == 'tx']
    rx = [(i, f) for i, d, f in rows if d == 'rx']
    identity = connections[connection][1]['client_id']
    expected = (0x10, b'\x00\x04MQTT\x04' + bytes([2 if clean else 0]) + b'\x00\x1e' + string(identity))
    require(tx and rx and tx[0][1] == expected and rx[0][1] == (0x20, bytes([int(present), 0])),
            'CONNECT/CONNACK/session flags differ from role')
    require(connections[connection][0] < tx[0][0] < rx[0][0], 'connection handshake ordering')
    require(all(i > rx[0][0] for i, _ in tx[1:]), 'test client sent before CONNACK')
    require(sum(f[0] == 0x20 for _, f in rx) == 1, 'duplicate CONNACK')
    if graceful:
        require(tx[-1][1] == (0xe0, b''), 'missing DISCONNECT')
    else:
        require(all(f[0] != 0xe0 for _, f in tx), 'cut connection claims graceful DISCONNECT')
    return tx, rx


def alternating(frames, connection):
    rows = frames[connection]
    require([d for _, d, _ in rows] == ['tx', 'rx'] * (len(rows) // 2) + ['tx'],
            'sender/reset ACK barrier ordering')


def proxy_proof(events, initial, frames, ack, mid):
    cut_index, cut = one(events, 'fault-cut')
    raw = packet(ack, mid_bytes(mid))
    require(cut['hex'] == raw.hex() and cut['ack_kind'] == ack and cut['ack_body_hex'] == mid_bytes(mid).hex()
            and cut['forwarded'] is False, 'ACK cut frame/ID mismatch')
    streams = defaultdict(bytes)
    for event in events:
        if event['kind'] == 'proxy-bytes':
            streams[event['direction'], event['operation']] += bytes.fromhex(event['hex'])
    client_tx = b''.join(packet(*f) for _, d, f in frames[initial] if d == 'tx')
    client_rx = b''.join(packet(*f) for _, d, f in frames[initial] if d == 'rx')
    require(streams['client-to-broker', 'receive'] == streams['client-to-broker', 'forward'] == client_tx,
            'proxy client bytes were changed or omitted')
    require(streams['broker-to-client', 'receive'] == client_rx + raw
            and streams['broker-to-client', 'forward'] == client_rx, 'ACK not proven received and withheld')
    cleanup_index, cleanup = one(events, 'proxy-cleanup')
    require(cleanup['complete'] is True and not cleanup['errors'] and cut_index < cleanup_index,
            'proxy cleanup incomplete or precedes cut')
    one(events, 'proxy-target')
    return cut_index, cleanup_index


def receiver_proof(events, frames, receivers, topic, original, health):
    """Replay persistent receive state; allow repeated PUBREL and ID reuse."""
    pending, completed, deliveries = {}, set(), Counter()
    wanted = None
    expected_ack = Counter()
    by_index = {i: (connection, d, f) for connection in receivers for i, d, f in frames[connection]}
    last_delivery = -1
    for i, event in enumerate(events):
        if i in by_index:
            connection, direction, frame = by_index[i]
            kind, body = frame
            if direction == 'rx' and kind >> 4 == 3:
                value = publication(frame)
                mid = value['mid']
                require(value['topic'] == topic and value['payload'] in {original, health}
                        and value['qos'] == 2 and not value['retain'] and mid not in (None, 0), 'receiver PUBLISH content')
                if mid in pending:
                    require(value['dup'] and pending[mid]['payload'] == value['payload'], 'inflight PUBLISH changed or DUP missing')
                else:
                    pending[mid] = dict(payload=value['payload'], delivered=False, rec=False)
                expected_ack[connection, 0x50, mid] += 1
            elif direction == 'rx' and kind == 0x62:
                require(len(body) == 2 and int.from_bytes(body, 'big') > 0, 'malformed PUBREL')
                mid = int.from_bytes(body, 'big')
                state = pending.get(mid)
                require(state is not None or mid in completed, 'PUBREL has no observed exchange')
                if state:
                    require(state['rec'], 'PUBREL precedes receiver PUBREC')
                    if not state['delivered']:
                        require(wanted is None, 'missing application delivery')
                        wanted = (connection, mid, state['payload'])
                expected_ack[connection, 0x70, mid] += 1
            elif direction == 'tx' and kind in {0x50, 0x70}:
                require(len(body) == 2, 'receiver ACK malformed')
                mid = int.from_bytes(body, 'big')
                require(expected_ack[connection, kind, mid] > 0, 'receiver ACK without corresponding frame')
                expected_ack[connection, kind, mid] -= 1
                if kind == 0x50:
                    pending[mid]['rec'] = True
                else:
                    require(wanted is None, 'PUBCOMP before application delivery evidence')
                    require(mid not in pending or pending[mid]['delivered'], 'PUBCOMP before delivery')
                    pending.pop(mid, None)
                    completed.add(mid)
        if event['kind'] == 'application-delivery':
            observed = (event.get('connection'), event['packet_id'], bytes.fromhex(event['payload_hex']))
            require(wanted == observed and event['topic'] == topic, 'application label lacks matching PUBREL/content')
            deliveries[observed[2]] += 1
            require(deliveries[observed[2]] == event['count'] == 1, 'duplicate original or health delivery')
            pending[observed[1]]['delivered'] = True
            wanted, last_delivery = None, i
    require(wanted is None and not pending and deliveries == Counter({original: 1, health: 1}),
            'missing completed original/health delivery')
    # The deliberate first connection cut leaves one withheld PUBREC or PUBCOMP.
    # All other observed frames must have their own ACK; no ACK can be reused.
    return expected_ack, last_delivery


def branch_proof(events):
    _, start = one(events, 'qos2-branch-start')
    direction, phase = start['direction'], start['phase']
    require(start['scenario_version'] == VERSION, 'stale scenario version')
    identity, topic = start['client_id'], start['topic']
    original, health = bytes.fromhex(start['original_hex']), bytes.fromhex(start['health_hex'])
    require((original, health) == (b'original', b'healthy-after-recovery'), 'scenario payload mismatch')
    connections, frames = capture(events)
    persistent = [c for c, (_, e) in connections.items() if e['client_id'] == identity]
    other = set(connections) - set(persistent)
    require(len(connections) == 4 and len(persistent) == 3 and len(other) == 1, 'role connections incomplete')
    initial, resumed, reset = persistent
    helper = other.pop()
    first_tx, first_rx = handshake(initial, connections, frames, clean=False, present=False, graceful=False)
    resumed_tx, resumed_rx = handshake(resumed, connections, frames, clean=False, present=True, graceful=True)
    reset_tx, reset_rx = handshake(reset, connections, frames, clean=True, present=False, graceful=True)
    alternating(frames, reset)
    helper_tx, helper_rx = handshake(helper, connections, frames, clean=True, present=False, graceful=True)
    require([f for _, f in reset_tx] == [reset_tx[0][1], (0xe0, b'')] and len(reset_rx) == 1, 'reset handshake shape invalid')
    reset_index, reset_event = one(events, 'qos2-clean-session-reset')
    done_index, done = one(events, 'qos2-branch-completed')
    disconnect_index, disconnected = one(events, 'qos2-disconnect')
    aborts = [(i, e) for i, e in enumerate(events) if e['kind'] == 'connection-aborted']
    require(len(aborts) == 1 and aborts[0][1]['connection'] == initial, 'initial connection abort missing/mismatched')
    require(disconnected['sender_state'] == phase and reset_event['client_id'] == identity
            and done['original_deliveries'] == done['health_deliveries'] == 1, 'branch metadata mismatch')
    require(disconnect_index < aborts[0][0] < connections[resumed][0] < reset_tx[0][0]
            < reset_rx[0][0] < reset_tx[-1][0] < reset_index < done_index, 'cut/resume/reset ordering')
    sender_mid = 0x1234 if direction == 'client-to-broker' else 0x4321
    publish = lambda payload, mid, dup=False: (0x3c if dup else 0x34, string(topic) + mid_bytes(mid) + payload)
    if direction == 'client-to-broker':
        alternating(frames, initial)
        expected_tx = [publish(original, sender_mid)] + ([(0x62, mid_bytes(sender_mid))] if phase == 'AwaitPubComp' else [])
        expected_rx = [(0x50, mid_bytes(sender_mid))] if phase == 'AwaitPubComp' else []
        require([f for _, f in first_tx[1:]] == expected_tx and [f for _, f in first_rx[1:]] == expected_rx, 'initial sender cut stage')
        expected_flow = ([('tx', publish(original, sender_mid, True)), ('rx', (0x50, mid_bytes(sender_mid)))]
                         if phase == 'AwaitPubRec' else [])
        expected_flow += [('tx', (0x62, mid_bytes(sender_mid))), ('rx', (0x70, mid_bytes(sender_mid))),
                          ('tx', publish(health, sender_mid)), ('tx', publish(health, sender_mid, True)),
                          ('rx', (0x50, mid_bytes(sender_mid))), ('rx', (0x50, mid_bytes(sender_mid))),
                          ('tx', (0x62, mid_bytes(sender_mid))), ('rx', (0x70, mid_bytes(sender_mid)))]
        require([(d, f) for _, d, f in frames[resumed][2:-1]] == expected_flow,
                'resumed sender handshake/content/order')
        require(disconnected['packet_id'] == sender_mid and disconnected['ack_received_by_client'] is False, 'cut packet ID/ACK metadata')
        cut, proxy_cleanup = proxy_proof(events, initial, frames, 0x50 if phase == 'AwaitPubRec' else 0x70, sender_mid)
        require(cut < disconnect_index and proxy_cleanup < connections[resumed][0], 'cut/cleanup/resume barrier')
        receivers = [helper]
    else:
        alternating(frames, helper)
        require(not any(e['kind'].startswith('proxy-') or e['kind'] == 'fault-cut' for e in events), 'inbound branch claims proxy cut')
        expected_tx = [publish(original, sender_mid), (0x62, mid_bytes(sender_mid)),
                       publish(health, sender_mid + 1), (0x62, mid_bytes(sender_mid + 1))]
        expected_rx = [(kind, mid_bytes(mid)) for mid in [sender_mid, sender_mid + 1] for kind in [0x50, 0x70]]
        require([f for _, f in helper_tx[1:-1]] == expected_tx and [f for _, f in helper_rx[1:]] == expected_rx, 'control publisher handshake')
        first_publications = [(i, publication(f)) for i, d, f in frames[initial] if d == 'rx' and f[0] >> 4 == 3]
        require(len(first_publications) == 1, 'initial receiver PUBLISH missing or extra')
        mid = first_publications[0][1]['mid']
        require(first_publications[0][1]['payload'] == original and not first_publications[0][1]['dup'],
                'initial new sender PUBLISH content/DUP=0')
        require(disconnected['packet_id'] == mid and disconnected['withheld'] == ('PUBREC' if phase == 'AwaitPubRec' else 'PUBCOMP'), 'withheld ACK metadata')
        initial_acks = [f for _, f in first_tx if f[0] in {0x50, 0x70}]
        require(initial_acks == ([(0x50, mid_bytes(mid))] if phase == 'AwaitPubComp' else []), 'initial receiver did not withhold selected ACK')
        recovered = [(i, f) for i, d, f in frames[resumed] if d == 'rx' and f[0] != 0x20]
        require(bool(recovered), 'resumed receiver has no exchange')
        if phase == 'AwaitPubRec':
            value = publication(recovered[0][1])
            require(value['mid'] == mid and value['dup'] and value['payload'] == original, 'retransmitted PUBLISH ID/DUP/content')
        else:
            require(recovered[0][1] == (0x62, mid_bytes(mid)), 'resumed receiver expected original PUBREL')
        completed_original = [i for i, f in resumed_tx if f == (0x70, mid_bytes(mid))]
        health_send = next(i for i, f in helper_tx if f == publish(health, sender_mid + 1))
        require(completed_original and completed_original[0] < health_send, 'health control precedes recovery completion')
        receivers = [initial, resumed]
    subscriber = helper if direction == 'client-to-broker' else initial
    req = [(i, f) for i, d, f in frames[subscriber] if d == 'tx' and f[0] == 0x82]
    grants = [(i, f) for i, d, f in frames[subscriber] if d == 'rx' and f[0] == 0x90]
    require(len(req) == len(grants) == 1 and req[0][1] == (0x82, b'\0\x09' + string(topic) + b'\x02')
            and grants[0][1] == (0x90, b'\0\x09\x02') and req[0][0] < grants[0][0], 'subscription grant/ID/topic')
    for connection in receivers:
        for i, d, f in frames[connection]:
            allowed = {0x20, 0x90, 0x34, 0x3c, 0x62} if d == 'rx' else {0x10, 0x82, 0x50, 0x70, 0xe0}
            require(f[0] in allowed and (f[0] != 0x82 or connection == subscriber), 'unexpected receiver frame')
            if d == 'rx' and f[0] >> 4 == 3:
                require(i > req[0][0], 'PUBLISH before subscription request')
    withheld, last_delivery = receiver_proof(events, frames, receivers, topic, original, health)
    residual = +withheld
    if direction == 'client-to-broker':
        require(not residual, 'missing receiver ACK')
    else:
        mid = disconnected['packet_id']
        expected = Counter({(initial, 0x50 if phase == 'AwaitPubRec' else 0x70, mid): 1})
        require(residual == expected, 'additional receiver ACK missing or initial cut ACK was sent')
    obs_index, obs = one(events, 'observation')
    require(obs['completed'] is True and obs['seconds_required'] >= 0.4 and obs['actual_seconds'] >= obs['seconds_required']
            and events[obs_index]['seconds'] - events[last_delivery]['seconds'] >= obs['seconds_required']
            and last_delivery < obs_index < reset_tx[0][0], 'bounded observation lacks delivery/control barrier')


def check_evidence(events):
    starts = [(i, e) for i, e in enumerate(events) if e['kind'] == 'qos2-branch-start']
    require(len(starts) == 4 and [(e['direction'], e['phase']) for _, e in starts] == sorted(RECOVERY_BRANCHES),
            'four ordered branch starts required')
    require(len({e['topic'] for _, e in starts}) == len({e['client_id'] for _, e in starts}) == 4, 'branch namespace/identity reused')
    boundaries = [i for i, _ in starts] + [len(events)]
    all_connections = []
    for (begin, start), end in zip(starts, boundaries[1:]):
        branch = events[begin:end]
        # End labels cannot hide raw frames or other branch facts outside their
        # selected branch. Direction on bytes/proxy events describes the wire.
        done_index, _ = one(branch, 'qos2-branch-completed')
        require(not any(e['kind'] in {'connection', 'bytes', 'frame', 'application-delivery', 'fault-cut',
                                     'proxy-bytes', 'qos2-disconnect', 'qos2-clean-session-reset', 'observation'}
                        for e in branch[done_index + 1:]), 'wire facts after branch completion')
        for e in branch:
            if e['kind'] in {'bytes', 'frame', 'proxy-bytes'}:
                require(e.get('phase') == start['phase'], 'wire phase mismatch')
            if e['kind'] in {'connection', 'qos2-disconnect', 'application-delivery', 'observation',
                             'qos2-clean-session-reset', 'qos2-branch-completed'}:
                require((e.get('direction'), e.get('phase')) == (start['direction'], start['phase']), 'cross-branch event')
        branch_proof(branch)
        all_connections.extend(e['connection'] for e in branch if e['kind'] == 'connection')
    require(len(all_connections) == len(set(all_connections)) == 16, 'connection reused across branches')
    require(not any(e['kind'] in {'connection', 'bytes', 'frame', 'application-delivery', 'fault-cut'}
                    for e in events[:starts[0][0]]), 'wire evidence outside recovery branches')
