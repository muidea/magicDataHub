"""Independently reconstruct grants, allowed copies and per-connection ACKs."""
from collections import Counter
import re

from mixed_qos import PAYLOAD, VERSION, VECTORS, WINDOW, filters
from qos2 import mid_bytes
from qos2_evidence import capture, handshake, require
from wire import publication, string


def subscriber_proof(events, rows, connection, topic, input_qos, requests, sent, input_done, end):
    cursor, grants = 2, []
    for mid, (topic_filter, requested) in enumerate(zip(filters(topic, len(requests)), requests), 9):
        request, grant = rows[cursor:cursor + 2]
        require(request[1:] == ('tx', (0x82, mid_bytes(mid) + string(topic_filter) + bytes([requested]))),
                'mixed subscription request differs')
        i, d, (k, body) = grant
        require(d == 'rx' and k == 0x90 and len(body) == 3 and body[:2] == mid_bytes(mid)
                and body[2] <= requested and i < sent, 'mixed SUBACK actual grant/barrier differs')
        grants.append(body[2]); cursor += 2
    allowed = Counter(min(input_qos, g) for g in grants)
    states, used, received = {}, Counter(), Counter()
    debt, wanted = None, None
    wire = {i: (d, f) for i, d, f in rows[cursor:-1]}
    for i, event in enumerate(events):
        if i in wire:
            direction, (kind, body) = wire[i]
            if direction == 'rx':
                require(debt is None and wanted is None, 'mixed missing own delivery/ACK before next frame')
            if direction == 'rx' and kind >> 4 == 3:
                value = publication((kind, body)); qos, mid = value['qos'], value['mid']
                require(i > sent and value['topic'] == topic and value['payload'] == PAYLOAD
                        and not value['retain'] and qos in allowed
                        and (mid not in (None, 0) if qos else not value['dup']), 'mixed output content/QoS differs')
                previous = states.get(mid) if qos else None
                if value['dup']:
                    require(previous is not None and previous['qos'] == qos
                            and (qos == 1 or not previous['complete']), 'mixed retry without matching exchange')
                else:
                    require(previous is None or previous['complete'], 'mixed active packet ID reused')
                    used[qos] += 1
                    require((input_qos == 1 and qos == 0) or used[qos] <= allowed[qos],
                            'mixed excess independent copies for matching subscriptions')
                    if qos:
                        states[mid] = dict(qos=qos, complete=False, rec=False)
                if qos == 2:
                    debt = (0x50, mid)
                else:
                    wanted = (qos, mid)
                    if qos == 1:
                        debt = (0x40, mid)
            elif direction == 'rx' and kind == 0x62:
                mid = int.from_bytes(body, 'big'); state = states.get(mid)
                require(len(body) == 2 and state is not None and state['qos'] == 2 and state['rec'],
                        'mixed PUBREL missing own PUBREC')
                if not state['complete']:
                    wanted = (2, mid)
                debt = (0x70, mid)
            else:
                # TX ACKs are checked below; all other control traffic is forbidden.
                require(direction == 'tx' and kind in (0x40, 0x50, 0x70), 'mixed unexpected subscriber frame')
        if event['kind'] == 'mixed-delivery' and event['connection'] == connection:
            require(wanted is not None and (event['qos'], event['packet_id']) == wanted
                    and event['topic'] == topic and event['payload_hex'] == PAYLOAD.hex(),
                    'mixed delivery has no matching wire application')
            received[event['qos']] += 1; wanted = None
        if i in wire and wire[i][0] == 'tx':
            kind, body = wire[i][1]
            require(debt is not None and wanted is None and (kind, body) == (debt[0], mid_bytes(debt[1])),
                    'mixed missing/mismatched per-connection ACK')
            state = states[debt[1]]
            if kind == 0x50:
                state['rec'] = True
            else:
                state['complete'] = True
            debt = None
    require(wanted is None and debt is None and all(s['complete'] for s in states.values())
            and received[max(allowed)] > 0, 'mixed maximum granted QoS delivery/exchange missing')
    starts = [(i, e) for i, e in enumerate(events) if e['kind'] == 'mixed-observation-start' and e['connection'] == connection]
    ends = [(i, e) for i, e in enumerate(events) if e['kind'] == 'mixed-observation' and e['connection'] == connection]
    require(len(starts) == len(ends) == 1, 'mixed missing/duplicate observation')
    start_i, start = starts[0]; end_i, obs = ends[0]
    require(input_done < start_i < end_i < rows[-1][0] < end and obs['completed'] is True
            and start['seconds_required'] == obs['seconds_required'] == WINDOW
            and obs['actual_seconds'] >= WINDOW and obs['seconds'] - start['seconds'] >= obs['actual_seconds']
            and all(i < end_i for i in wire), 'mixed observation/window/cleanup differs')
    prior = [e for i, e in enumerate(events) if i < start_i and e['kind'] == 'mixed-delivery' and e['connection'] == connection]
    require(any(e['qos'] == max(allowed) for e in prior), 'mixed positive maximum-QoS control before window missing')


def check_evidence(events):
    starts = [(i, e) for i, e in enumerate(events) if e['kind'] == 'mixed-vector-start']
    ends = [(i, e) for i, e in enumerate(events) if e['kind'] == 'mixed-vector-completed']
    roles = [(i, e) for i, e in enumerate(events) if e['kind'] == 'mixed-roles']
    done = [(i, e) for i, e in enumerate(events) if e['kind'] == 'mixed-input-completed']
    for values in (starts, ends, roles, done):
        require([e['index'] for _, e in values] == list(range(len(VECTORS))), 'mixed matrix missing/duplicate/reordered vector')
    connections, frames = capture(events)
    require(len(connections) == 18 and len({e['client_id'] for _, e in connections.values()}) == 18,
            'mixed matrix requires eighteen distinct clean connections')
    assigned = set()
    for index, (input_qos, requests) in enumerate(VECTORS):
        start_i, start = starts[index]; end_i, _ = ends[index]; roles_i, role = roles[index]; done_i, _ = done[index]
        require(start['scenario_version'] == VERSION and re.fullmatch(r'validation/[0-9a-f]{32}/mixed-qos/item', start['topic'])
                and (index == 0 or ends[index - 1][0] < start_i), 'mixed version/namespace/vector boundary differs')
        publisher, subscriber = role['publisher'], role['subscriber']
        require(publisher != subscriber and not {publisher, subscriber} & assigned, 'mixed connection roles reused')
        assigned.update((publisher, subscriber))
        for c in (publisher, subscriber):
            require(c in connections, 'mixed role connection missing')
            handshake(c, connections, frames, clean=True, present=False, graceful=True)
            require(start_i < connections[c][0] < frames[c][1][0] < roles_i < done_i < frames[c][-1][0] < end_i,
                    'mixed connection/role/cleanup barrier differs')
        topic = start['topic']; body = string(topic) + (b'\0\x17' if input_qos else b'') + PAYLOAD
        expected = [('tx', (0x30 | input_qos << 1, body))]
        if input_qos == 1:
            expected += [('rx', (0x40, b'\0\x17'))]
        elif input_qos == 2:
            expected += [('rx', (0x50, b'\0\x17')), ('tx', (0x62, b'\0\x17')), ('rx', (0x70, b'\0\x17'))]
        actual = frames[publisher][2:-1]
        require([(d, f) for _, d, f in actual] == expected and roles_i < actual[0][0] <= actual[-1][0] < done_i,
                'mixed original input/ACK completion differs')
        subscriber_proof(events, frames[subscriber], subscriber, topic, input_qos, requests,
                         actual[0][0], done_i, end_i)
    require(assigned == set(connections) and not any(e['kind'] == 'connection-aborted' for e in events),
            'mixed extra/aborted connection')
    subscribers = {e['subscriber'] for _, e in roles}
    for kind in ('mixed-delivery', 'mixed-observation-start', 'mixed-observation'):
        require(all(e['connection'] in subscribers for e in events if e['kind'] == kind), 'mixed event outside subscriber roles')
