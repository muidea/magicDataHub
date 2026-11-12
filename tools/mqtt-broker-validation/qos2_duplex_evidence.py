"""Independent replay with separate sender and receiver Packet ID namespaces."""
from collections import Counter
import re

from qos2_evidence import capture, handshake, one, require
from qos2_duplex import BRANCHES, INPUTS, OUTPUTS, PHASES, SOURCE_IDS, VERSION
from wire import publication, string


def connection_proof(events, rows, connection, send_topic, receive_topic, sent_payloads, received_payloads):
    require([(d, f) for _, d, f in rows[2:4]] ==
            [('tx', (0x82, b'\0\x09' + string(receive_topic) + b'\x02')), ('rx', (0x90, b'\0\x09\x02'))],
            'duplex exact subscription/grant required')
    sending, receiving, sends, receives = {}, {}, [], []
    rec_debt, comp_debt, wanted, deliveries = Counter(), [], None, Counter()
    by_index = {i: (d, f) for i, d, f in rows[4:-1]}
    for i, event in enumerate(events):
        if i in by_index:
            direction, (kind, body) = by_index[i]
            if kind >> 4 == 3:
                value = publication((kind, body)); mid, payload = value['mid'], value['payload']
                require(value['qos'] == 2 and not value['retain'] and mid not in (None, 0), 'duplex PUBLISH flags/ID')
                if direction == 'tx':
                    require(kind == 0x34 and value['topic'] == send_topic and payload in sent_payloads
                            and (mid not in sending or sending[mid]['comp'] is not None), 'duplex client send/active input ID reused')
                    state = dict(mid=mid, payload=payload, publish=i, rec=None, rel=None, comp=None)
                    sending[mid] = state; sends.append(state)
                else:
                    require(value['topic'] == receive_topic and payload in received_payloads, 'duplex received PUBLISH content')
                    state = receiving.get(mid)
                    if state and not state['comps']:
                        require(value['dup'] and state['payload'] == payload and not state['rels'],
                                'duplex active output ID/content/PUBLISH after PUBREL')
                        state['publications'] += 1
                    else:
                        require(not value['dup'] and payload not in {s['payload'] for s in receives},
                                'duplex new output DUP/duplicate onward message')
                        state = dict(mid=mid, payload=payload, publish=i, publications=1, recs=[], rels=[], comps=[], delivered=False)
                        receiving[mid] = state; receives.append(state)
                    rec_debt[mid] += 1
            elif (direction, kind) in {('rx', 0x50), ('tx', 0x62), ('rx', 0x70)}:
                mid = int.from_bytes(body, 'big')
                require(len(body) == 2 and mid in sending, 'duplex input ACK without its sender exchange')
                state = sending[mid]
                if kind == 0x50:
                    require(state['rec'] is None and state['rel'] is None and state['comp'] is None, 'duplex input PUBREC ordering')
                    state['rec'] = i
                elif kind == 0x62:
                    require(state['rec'] is not None and state['rel'] is None and state['comp'] is None, 'duplex input PUBREL ordering')
                    state['rel'] = i
                else:
                    require(state['rel'] is not None and state['comp'] is None, 'duplex input PUBCOMP ordering')
                    state['comp'] = i
            elif (direction, kind) in {('tx', 0x50), ('rx', 0x62), ('tx', 0x70)}:
                mid = int.from_bytes(body, 'big')
                require(len(body) == 2 and mid in receiving, 'duplex output ACK without its receiver exchange')
                state = receiving[mid]
                if kind == 0x50:
                    require(rec_debt[mid] > 0 and not state['rels'], 'duplex output PUBREC without PUBLISH/after PUBREL')
                    rec_debt[mid] -= 1; state['recs'].append(i)
                elif kind == 0x62:
                    require(state['recs'] and not rec_debt[mid], 'duplex output PUBREL before PUBREC')
                    state['rels'].append(i); comp_debt.append((mid, state))
                    if not state['delivered']:
                        require(wanted is None, 'duplex missing prior application delivery'); wanted = (mid, state)
                else:
                    require(comp_debt and comp_debt[0][0] == mid and wanted is None, 'duplex output PUBCOMP before delivery/wrong exchange')
                    _, state = comp_debt.pop(0); state['comps'].append(i)
            else:
                require(False, 'duplex unexpected frame')
        if event['kind'] == 'application-delivery' and event['connection'] == connection:
            require(wanted is not None, 'duplex application label without own PUBREL')
            mid, state = wanted
            require(event['packet_id'] == mid and event['topic'] == receive_topic
                    and bytes.fromhex(event['payload_hex']) == state['payload'] and event['count'] == 1,
                    'duplex application identity/content mismatch')
            deliveries[state['payload']] += 1
            require(deliveries[state['payload']] == 1, 'duplex duplicate application delivery')
            state['delivered'] = True; wanted = None
    require([s['payload'] for s in sends] == list(sent_payloads) and all(s['comp'] is not None for s in sends),
            'duplex sender exchange missing/order/extra')
    require([s['payload'] for s in receives] == list(received_payloads) and deliveries == Counter(received_payloads)
            and not +rec_debt and not comp_debt and wanted is None, 'duplex receiver exchange/delivery missing/order/extra')
    for state in receives:
        require(state['delivered'] and state['comps'] and len(state['recs']) == state['publications']
                and len(state['rels']) >= len(state['recs']) and len(state['comps']) == len(state['rels']),
                'duplex output repeated packet/ACK response missing')
    return sends, receives


def branch_proof(events, branch):
    start_i, start = one(events, 'qos2-duplex-branch-start'); roles_i, roles = one(events, 'qos2-duplex-roles')
    end_i, end = one(events, 'qos2-duplex-branch-completed')
    require(start_i == 0 and end_i == len(events) - 1 and start['scenario_version'] == VERSION
            and re.fullmatch(r'validation/[0-9a-f]{32}/qos2-duplex/' + branch, start['topic']), 'duplex branch version/namespace/boundary')
    require(all(e.get('branch') == branch for e in events if e['kind'].startswith('qos2-duplex-')), 'duplex event crossed branch')
    phase_rows = [(i, e) for i, e in enumerate(events) if e['kind'] == 'qos2-duplex-phase']
    require([e['phase'] for _, e in phase_rows] == list(PHASES), 'duplex phases missing/duplicate/order')
    phases = [i for i, _ in phase_rows]
    connections, frames = capture(events); owner, helper = roles['owner'], roles['helper']
    require(len(connections) == 2 and {owner, helper} == set(connections)
            and len({e['client_id'] for _, e in connections.values()}) == 2, 'duplex roles/connections/identity')
    for c in connections:
        handshake(c, connections, frames, clean=True, present=False, graceful=True)
        require(start_i < connections[c][0] < frames[c][1][0] < roles_i < frames[c][2][0]
                and phases[4] < frames[c][-1][0] < end_i, 'duplex handshake/roles/cleanup barriers')
    require(not any(e['kind'] == 'connection-aborted' for e in events), 'duplex aborted connection')
    deliveries = [e for e in events if e['kind'] == 'application-delivery']
    require(len(deliveries) == 4 and {e['connection'] for e in deliveries} == {owner, helper}, 'duplex extra/foreign application labels')
    topic = start['topic']
    own_send, own_recv = connection_proof(events, frames[owner], owner, topic + '/in', topic + '/out', INPUTS, OUTPUTS)
    src_send, src_recv = connection_proof(events, frames[helper], helper, topic + '/out', topic + '/in', OUTPUTS, INPUTS)
    incoming, outgoing = own_send[0], own_recv[0]
    require(incoming['mid'] == outgoing['mid'] and own_send[1]['mid'] == incoming['mid'], 'duplex same ID/health reuse not established')
    require(outgoing['publish'] < incoming['publish'] < incoming['rec'] < phases[0]
            < min(incoming['rel'], outgoing['recs'][0]), 'duplex simultaneous namespaces/overlap not established')
    if branch == 'incoming-first':
        require(incoming['comp'] < phases[1] < outgoing['recs'][0] and outgoing['comps'][0] < phases[2], 'duplex incoming-first completion barrier')
    else:
        require(outgoing['comps'][0] < phases[1] < incoming['rel'] and incoming['comp'] < phases[2], 'duplex outgoing-first completion barrier')
    require([s['mid'] for s in src_send] == list(SOURCE_IDS) and src_send[0]['comp'] < src_send[1]['publish'], 'duplex helper source IDs/completion')
    require(phases[2] < own_send[1]['publish'] and phases[2] < src_send[1]['publish'], 'duplex health before original completion')
    for sent, received in [(own_send, src_recv), (src_send, own_recv)]:
        for s, r in zip(sent, received):
            require(s['publish'] < r['publish'] and s['comp'] < phases[3] and r['comps'][0] < phases[3], 'duplex delivery before source/health incomplete')
    starts = [(i, e) for i, e in enumerate(events) if e['kind'] == 'qos2-duplex-observation-start']
    ends = [(i, e) for i, e in enumerate(events) if e['kind'] == 'qos2-duplex-observation']
    require([e['connection'] for _, e in starts] == [owner, helper]
            and [e['connection'] for _, e in ends] == [owner, helper], 'duplex missing/duplicate/foreign observation')
    require(phases[3] < starts[0][0] < ends[0][0] < starts[1][0] < ends[1][0] < phases[4], 'duplex separate observation barriers')
    for (begin_i, begin), (finish_i, finish) in zip(starts, ends):
        require(begin['seconds_required'] == finish['seconds_required'] >= 0.4 and finish['completed'] is True
                and finish['actual_seconds'] >= finish['seconds_required']
                and finish['seconds'] - begin['seconds'] >= finish['actual_seconds'], 'duplex insufficient observed window')
        require(not any(i > finish_i and f != (0xe0, b'') for i, _, f in frames[finish['connection']]), 'duplex protocol frame after observation')
    return connections


def check_evidence(events):
    starts = [(i, e) for i, e in enumerate(events) if e['kind'] == 'qos2-duplex-branch-start']
    ends = [(i, e) for i, e in enumerate(events) if e['kind'] == 'qos2-duplex-branch-completed']
    require([e['branch'] for _, e in starts] == list(BRANCHES) and [e['branch'] for _, e in ends] == list(BRANCHES),
            'duplex both completion-order branches required')
    all_connections, all_identities, covered = set(), set(), set()
    for (begin, _), (end, _), branch in zip(starts, ends, BRANCHES):
        require(begin < end and not covered.intersection(range(begin, end + 1)), 'duplex branches overlap/order')
        connections = branch_proof(events[begin:end + 1], branch)
        identities = {e['client_id'] for _, e in connections.values()}
        require(not all_connections.intersection(connections) and not all_identities.intersection(identities), 'duplex branch identity reused')
        all_connections.update(connections); all_identities.update(identities); covered.update(range(begin, end + 1))
    require(not any(i not in covered and (e['kind'] in {'connection', 'bytes', 'frame', 'application-delivery', 'connection-aborted'}
                    or e['kind'].startswith('qos2-duplex-')) for i, e in enumerate(events)), 'duplex facts outside branch boundaries')
