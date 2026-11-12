"""Rebuild both subscriber deliveries from their separate captured exchanges."""
from collections import Counter
import re

from qos2 import mid_bytes
from qos2_evidence import capture, handshake, one, require
from qos2_fanout import MID, PAYLOADS, PHASES, VERSION
from wire import publication, string


def publisher_proof(rows, topic, phases):
    rows = rows[2:-1]
    body = lambda payload: string(topic) + mid_bytes(MID) + payload
    expected = [('tx', (0x34, body(PAYLOADS[0]))), ('tx', (0x3c, body(PAYLOADS[0]))),
                ('rx', (0x50, mid_bytes(MID))), ('rx', (0x50, mid_bytes(MID))),
                ('tx', (0x62, mid_bytes(MID))), ('rx', (0x70, mid_bytes(MID))),
                ('tx', (0x34, body(PAYLOADS[1]))), ('rx', (0x50, mid_bytes(MID))),
                ('tx', (0x62, mid_bytes(MID))), ('rx', (0x70, mid_bytes(MID)))]
    require([(d, f) for _, d, f in rows] == expected, 'fanout duplicate input/ACK/reused ID exchange differs')
    require(rows[5][0] < phases[0] < phases[1] < rows[6][0] < rows[9][0] < phases[2],
            'fanout publisher original/health phase barriers')
    return {PAYLOADS[0]: rows[0][0], PAYLOADS[1]: rows[6][0]}


def subscriber_proof(events, rows, connection, topic, sent, phases):
    requests = [(i, f) for i, d, f in rows if d == 'tx' and f[0] == 0x82]
    grants = [(i, f) for i, d, f in rows if d == 'rx' and f[0] == 0x90]
    require(len(requests) == len(grants) == 1 and requests[0][1] == (0x82, b'\0\x09' + string(topic) + b'\x02')
            and grants[0][1] == (0x90, b'\0\x09\x02') and requests[0][0] < grants[0][0] < sent[PAYLOADS[0]],
            'fanout subscriber exact subscription/grant/send barrier')
    states, exchanges, deliveries = {}, [], Counter()
    rec_debt, comp_debt, wanted = Counter(), [], None
    by_index = {i: (d, f) for i, d, f in rows[2:-1]}
    for i, event in enumerate(events):
        if i in by_index:
            direction, (kind, body) = by_index[i]
            allowed = {0x82, 0x50, 0x70} if direction == 'tx' else {0x90, 0x34, 0x3c, 0x62}
            require(kind in allowed, 'fanout unexpected subscriber frame')
            if direction == 'rx' and kind >> 4 == 3:
                value = publication((kind, body)); mid, payload = value['mid'], value['payload']
                require(value['topic'] == topic and value['qos'] == 2 and not value['retain']
                        and mid not in (None, 0) and payload in PAYLOADS and i > sent[payload],
                        'fanout received PUBLISH content/send barrier')
                state = states.get(mid)
                if state and not state['complete']:
                    require(not state['rels'] and value['dup'] and state['payload'] == payload,
                            'fanout active ID reused/content changed/PUBLISH after PUBREL')
                    state['publications'] += 1
                else:
                    require(not value['dup'] and payload not in {s['payload'] for s in exchanges},
                            'fanout new publication DUP/duplicate onward payload')
                    state = dict(mid=mid, payload=payload, publications=1, recs=[], rels=[], comps=[], complete=False, delivered=False)
                    states[mid] = state; exchanges.append(state)
                rec_debt[mid] += 1
            elif direction == 'tx' and kind == 0x50:
                mid = int.from_bytes(body, 'big')
                require(len(body) == 2 and rec_debt[mid] > 0 and not states[mid]['rels'],
                        'fanout PUBREC without matching PUBLISH or after PUBREL')
                rec_debt[mid] -= 1; states[mid]['recs'].append(i)
            elif direction == 'rx' and kind == 0x62:
                mid = int.from_bytes(body, 'big')
                require(len(body) == 2 and mid in states and states[mid]['recs'] and not rec_debt[mid],
                        'fanout PUBREL without own PUBREC/ID')
                state = states[mid]; state['rels'].append(i); comp_debt.append((mid, state))
                if not state['delivered']:
                    require(wanted is None, 'fanout missing prior delivery')
                    wanted = (mid, state)
            elif direction == 'tx' and kind == 0x70:
                mid = int.from_bytes(body, 'big')
                require(len(body) == 2 and comp_debt and comp_debt[0][0] == mid and wanted is None,
                        'fanout PUBCOMP missing/mismatched or before delivery')
                _, state = comp_debt.pop(0); state['comps'].append(i); state['complete'] = True
        if event['kind'] == 'application-delivery' and event['connection'] == connection:
            require(wanted is not None, 'fanout delivery has no matching PUBREL')
            mid, state = wanted
            require(event['packet_id'] == mid and event['topic'] == topic
                    and bytes.fromhex(event['payload_hex']) == state['payload'] and event['count'] == 1,
                    'fanout application identity/content mismatch')
            deliveries[state['payload']] += 1
            require(deliveries[state['payload']] == 1, 'fanout duplicate application delivery')
            state['delivered'] = True; wanted = None
    require(not +rec_debt and not comp_debt and wanted is None and deliveries == Counter(PAYLOADS)
            and len(exchanges) == 2, 'fanout incomplete subscriber exchanges/deliveries')
    for state in exchanges:
        require(state['complete'] and state['delivered'] and len(state['recs']) == state['publications']
                and len(state['rels']) >= len(state['recs']) and len(state['comps']) == len(state['rels']),
                'fanout ACK counts/response missing')
        phase = phases[1] if state['payload'] == PAYLOADS[0] else phases[3]
        require(state['comps'][0] < phase, 'fanout first completion missing before phase barrier')
    completions = {state['payload']: state['comps'][0] for state in exchanges}
    observations = [(i, e) for i, e in enumerate(events) if e['kind'] == 'qos2-fanout-observation' and e['connection'] == connection]
    require(len(observations) == 1, 'fanout missing/duplicate subscriber observation')
    i, obs = observations[0]
    starts = [(n, e) for n, e in enumerate(events) if e['kind'] == 'qos2-fanout-observation-start' and e['connection'] == connection]
    require(len(starts) == 1, 'fanout missing/duplicate subscriber observation start')
    start_i, start = starts[0]
    require(phases[3] < start_i < i < phases[4] and obs['completed'] is True and obs['seconds_required'] >= 0.4
            and obs['actual_seconds'] >= obs['seconds_required']
            and start['seconds_required'] == obs['seconds_required']
            and obs['seconds'] - start['seconds'] >= obs['actual_seconds']
            and all(n < i for state in exchanges for n in state['comps']), 'fanout incomplete observation/ACK window')
    return completions


def check_evidence(events):
    start_i, start = one(events, 'qos2-fanout-start'); roles_i, roles = one(events, 'qos2-fanout-roles')
    require(start['scenario_version'] == VERSION and re.fullmatch(r'validation/[0-9a-f]{32}/qos2-fanout', start['topic']),
            'fanout version/namespace mismatch')
    phase_rows = [(i, e) for i, e in enumerate(events) if e['kind'] == 'qos2-fanout-phase']
    require([e['phase'] for _, e in phase_rows] == list(PHASES), 'fanout phases missing/duplicated/reordered')
    phases = [i for i, _ in phase_rows]
    connections, frames = capture(events)
    publisher, subscribers = roles['publisher'], roles['subscribers']
    require(len(connections) == 3 and len(subscribers) == len(set(subscribers)) == 2
            and publisher not in subscribers and {publisher, *subscribers} == set(connections)
            and len({e['client_id'] for _, e in connections.values()}) == 3, 'fanout roles/connections/identities mismatch')
    for c in connections:
        handshake(c, connections, frames, clean=True, present=False, graceful=True)
        require(start_i < connections[c][0] < frames[c][1][0] < roles_i and phases[4] < frames[c][-1][0],
                'fanout connection/roles/cleanup barriers')
        require(not any(i > phases[4] and f != (0xe0, b'') for i, _, f in frames[c]), 'fanout frame after final phase')
    require(not any(e['kind'] == 'connection-aborted' for e in events), 'fanout aborted connection')
    deliveries = [e for e in events if e['kind'] == 'application-delivery']
    require(len(deliveries) == 4 and {e['connection'] for e in deliveries} == set(subscribers), 'fanout deliveries outside subscriber roles')
    starts = [e for e in events if e['kind'] == 'qos2-fanout-observation-start']
    require(len(starts) == 2 and {e['connection'] for e in starts} == set(subscribers), 'fanout observation starts outside subscriber roles')
    observations = [e for e in events if e['kind'] == 'qos2-fanout-observation']
    require(len(observations) == 2 and {e['connection'] for e in observations} == set(subscribers), 'fanout observations outside subscriber roles')
    first_end = next(i for i, e in enumerate(events) if e['kind'] == 'qos2-fanout-observation' and e['connection'] == subscribers[0])
    second_start = next(i for i, e in enumerate(events) if e['kind'] == 'qos2-fanout-observation-start' and e['connection'] == subscribers[1])
    require(first_end < second_start, 'fanout selected separate observation windows')
    sent = publisher_proof(frames[publisher], start['topic'], phases)
    require(roles_i < sent[PAYLOADS[0]], 'fanout publisher sent before roles')
    completed = [subscriber_proof(events, frames[c], c, start['topic'], sent, phases) for c in subscribers]
    require(completed[0][PAYLOADS[0]] < completed[1][PAYLOADS[0]]
            and completed[1][PAYLOADS[1]] < completed[0][PAYLOADS[1]], 'fanout selected recipient completion order')
