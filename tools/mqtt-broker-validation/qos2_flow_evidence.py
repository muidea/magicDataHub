"""Parent proof for the bounded two-inflight / repeated-control probe."""
from collections import Counter
import re

from qos2 import mid_bytes
from qos2_evidence import capture, handshake, require, one
from qos2_flow import MIDS, PAYLOADS, PHASES, VERSION
from wire import publication, string


def publisher_proof(rows, topic):
    rows = rows[2:-1]
    publish = lambda payload, mid: (0x34, string(topic) + mid_bytes(mid) + payload)
    require(len(rows) == 16 and [(d, f) for _, d, f in rows[:2]] ==
            [('tx', publish(payload, mid)) for payload, mid in zip(PAYLOADS, MIDS)], 'flow two input PUBLISH required')
    for selected, direction, kind, expected in [(rows[2:4], 'rx', 0x50, Counter(MIDS)),
                                               (rows[4:8], 'tx', 0x62, Counter(MIDS * 2)),
                                               (rows[8:12], 'rx', 0x70, Counter(MIDS * 2))]:
        require(all(d == direction and f[0] == kind and len(f[1]) == 2 for _, d, f in selected)
                and Counter(int.from_bytes(f[1], 'big') for _, _, f in selected) == expected,
                'flow input ACK/duplicate PUBREL/ID/barrier mismatch')
    require([(d, f) for _, d, f in rows[12:]] ==
            [('tx', publish(PAYLOADS[2], MIDS[0])), ('rx', (0x50, mid_bytes(MIDS[0]))),
             ('tx', (0x62, mid_bytes(MIDS[0]))), ('rx', (0x70, mid_bytes(MIDS[0])))], 'flow health/reused input ID exchange')
    return {payload: rows[i][0] for payload, i in zip(PAYLOADS, [0, 1, 12])}, rows[11][0]


def subscriber_proof(events, rows, connection, topic, sent, phases):
    requests = [(i, f) for i, d, f in rows if d == 'tx' and f[0] == 0x82]
    grants = [(i, f) for i, d, f in rows if d == 'rx' and f[0] == 0x90]
    require(len(requests) == len(grants) == 1 and requests[0][1] == (0x82, b'\0\x09' + string(topic) + b'\x02')
            and grants[0][1] == (0x90, b'\0\x09\x02') and requests[0][0] < grants[0][0] < min(sent.values()),
            'flow subscription/grant/publish barrier')
    current, exchanges, initial, delivered = {}, [], {}, Counter()
    debt, wanted = [], None
    first_rec, first_rel = None, None
    last_delivery = -1
    by_index = {i: (d, f) for i, d, f in rows[2:-1]}
    for i, event in enumerate(events):
        if i in by_index:
            direction, (kind, body) = by_index[i]
            allowed = {0x82, 0x50, 0x70} if direction == 'tx' else {0x90, 0x34, 0x3c, 0x62}
            require(kind in allowed, 'flow unexpected subscriber frame')
            if direction == 'rx' and kind >> 4 == 3:
                value = publication((kind, body))
                mid, payload = value['mid'], value['payload']
                require(value['topic'] == topic and value['qos'] == 2 and not value['retain'] and mid not in (None, 0)
                        and payload in PAYLOADS and i > sent[payload], 'flow received PUBLISH content/send barrier')
                state = current.get(mid)
                if state and not state['complete']:
                    require(not state['released'] and value['dup'] and state['payload'] == payload,
                            'flow active ID reused/content changed/PUBLISH after PUBREL')
                    state['publications'] += 1
                else:
                    require(not value['dup'], 'flow new Broker PUBLISH requires DUP=0')
                    state = dict(mid=mid, payload=payload, publications=1, recs=[], rels=[], comps=[],
                                 released=False, complete=False, delivered=False)
                    current[mid] = state; exchanges.append(state)
                    if payload in PAYLOADS[:2]:
                        require(payload not in initial and first_rec is None and i < phases[1],
                                'flow two outstanding original PUBLISH required before any PUBREC')
                        initial[payload] = (i, mid, state)
                    else:
                        require(phases[1] < sent[payload], 'flow health input precedes pair completion')
            elif direction == 'tx' and kind == 0x50:
                mid = int.from_bytes(body, 'big')
                require(len(body) == 2 and mid in current and not current[mid]['released'] and len(initial) == 2,
                        'flow PUBREC before both originals or after release/unknown ID')
                current[mid]['recs'].append(i)
                if first_rec is None: first_rec = i
            elif direction == 'rx' and kind == 0x62:
                mid = int.from_bytes(body, 'big')
                require(len(body) == 2 and mid in current and current[mid]['recs'], 'flow PUBREL without known PUBREC/ID')
                state = current[mid]
                state['released'] = True; state['rels'].append(i)
                if first_rel is None: first_rel = i
                if not state['delivered']:
                    require(wanted is None, 'flow missing prior application delivery')
                    wanted = (mid, state)
                debt.append((mid, state))
            elif direction == 'tx' and kind == 0x70:
                mid = int.from_bytes(body, 'big')
                require(len(body) == 2 and debt and debt[0][0] == mid and wanted is None,
                        'flow PUBCOMP missing/mismatched/reused or before delivery')
                _, state = debt.pop(0)
                state['comps'].append(i)
                state['complete'] = True
        if event['kind'] == 'application-delivery':
            require(wanted is not None, 'flow application label has no matching PUBREL')
            mid, state = wanted
            payload = bytes.fromhex(event['payload_hex'])
            require(event['connection'] == connection and event['packet_id'] == mid and event['topic'] == topic
                    and payload == state['payload'] and event['count'] == 1, 'flow application identity/content mismatch')
            delivered[payload] += 1
            require(delivered[payload] == 1, 'flow duplicate application delivery')
            state['delivered'] = True; wanted = None; last_delivery = i
    require(wanted is None and not debt and delivered == Counter(PAYLOADS) and len(exchanges) == 3,
            'flow incomplete exchanges/deliveries')
    require(len(initial) == 2 and len({v[1] for v in initial.values()}) == 2, 'flow simultaneous Broker IDs collide')
    for state in exchanges:
        original = state['payload'] in PAYLOADS[:2]
        require(state['complete'] and state['delivered'] and len(state['recs']) == state['publications'] + int(original)
                and len(state['rels']) >= len(state['recs']) and len(state['comps']) == len(state['rels']),
                'flow every repeated PUBREC/PUBREL needs its response')
        if original:
            require(sum(i < first_rel for i in state['recs']) >= 2 and max(state['comps'][:2]) < phases[1],
                    'flow duplicate PUBREC/pair completion barrier missing')
        else:
            require(state['comps'][0] < phases[2], 'flow initial health ACK missing before observation starts')
    return last_delivery


def check_evidence(events):
    start_i, start = one(events, 'qos2-flow-start')
    roles_i, roles = one(events, 'qos2-flow-roles')
    require(start['scenario_version'] == VERSION and re.fullmatch(r'validation/[0-9a-f]{32}/qos2-flow', start['topic']),
            'flow version/namespace mismatch')
    phase_rows = [(i, e) for i, e in enumerate(events) if e['kind'] == 'qos2-flow-phase']
    require([e['phase'] for _, e in phase_rows] == list(PHASES), 'flow phases missing/duplicated/reordered')
    phases = [i for i, _ in phase_rows]
    connections, frames = capture(events)
    publisher, subscriber = roles['publisher'], roles['subscriber']
    require(len(connections) == 2 and {publisher, subscriber} == set(connections)
            and len({e['client_id'] for _, e in connections.values()}) == 2, 'flow role/connection/identity mismatch')
    for c in connections:
        handshake(c, connections, frames, clean=True, present=False, graceful=True)
        require(start_i < connections[c][0] < frames[c][1][0] < roles_i
                and all(i < frames[c][-1][0] for i, _, _ in frames[c][:-1])
                and phases[3] < frames[c][-1][0], 'flow connection/roles/cleanup ordering')
    require(not any(e['kind'] == 'connection-aborted' for e in events), 'flow aborted connection')
    sent, input_completed = publisher_proof(frames[publisher], start['topic'])
    require(roles_i < input_completed < phases[0] < phases[1] < sent[PAYLOADS[2]], 'flow input/pair/health phase barrier')
    require(frames[publisher][-2][0] < phases[2], 'flow publisher health completion barrier')
    last_delivery = subscriber_proof(events, frames[subscriber], subscriber, start['topic'], sent, phases)
    obs_i, obs = one(events, 'observation')
    require(last_delivery < phases[2] < obs_i < phases[3] and obs['completed'] is True
            and obs['seconds_required'] >= 0.4 and obs['actual_seconds'] >= obs['seconds_required']
            and obs['seconds'] - events[phases[2]]['seconds'] >= obs['seconds_required'], 'flow observation lacks health/quiet window')
    require(not any(i > phases[3] and f != (0xe0, b'') for rows in frames.values() for i, _, f in rows),
            'flow protocol facts after observation completion')
