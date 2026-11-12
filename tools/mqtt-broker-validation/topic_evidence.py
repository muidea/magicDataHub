"""Reconstruct each selected Topic probe from connection-scoped wire facts."""
import re

from common import InputError
from topic_vectors import GENERAL, SYSTEM, VERSION, materialize
from wire import decode_frame, publication, string


def require(value, reason):
    if not value:
        raise InputError('Topic wire proof: ' + reason)


def one(events, kind):
    rows = [(i, e) for i, e in enumerate(events) if e['kind'] == kind]
    require(len(rows) == 1, 'missing or duplicate ' + kind)
    return rows[0]


def capture(events):
    connections, frames, buffers = {}, {}, {}
    for i, event in enumerate(events):
        kind = event['kind']
        if kind == 'connection':
            c = event['connection']
            require(c not in connections, 'duplicate connection')
            connections[c], frames[c], buffers[c] = (i, event), [], b''
        elif kind in {'bytes', 'frame'}:
            c = event['connection']
            require(c in connections, 'unknown connection or bytes before connection')
            raw = bytes.fromhex(event['hex'])
            require(bool(raw), 'empty wire capture')
            if kind == 'bytes' and event['direction'] == 'rx':
                buffers[c] += raw
            else:
                if kind == 'frame':
                    require(event['direction'] == 'rx' and buffers[c] == raw, 'RX raw/frame mismatch')
                    buffers[c] = b''
                frames[c].append((i, event['direction'], decode_frame(raw)))
    require(not any(buffers.values()), 'partial or undecoded RX')
    for c, (i, event) in connections.items():
        rows = frames[c]
        connect = (0x10, b'\x00\x04MQTT\x04\x02\x00\x1e' + string(event['client_id']))
        require(len(rows) >= 3 and rows[0][1:] == ('tx', connect)
                and rows[1][1:] == ('rx', (0x20, b'\0\0')) and i < rows[0][0], 'clean CONNECT/CONNACK ordering')
        require(rows[-1][1:] == ('tx', (0xe0, b'')), 'missing final DISCONNECT')
    return connections, frames


def subscriber(events, rows, pattern, expected, sent):
    """Validate every received publication and its own ACK, preserving legal DUP."""
    tx = [(i, f) for i, d, f in rows[2:-1] if d == 'tx']
    rx = [(i, f) for i, d, f in rows[2:-1] if d == 'rx']
    requests = [(i, f) for i, f in tx if f[0] == 0x82]
    grants = [(i, f) for i, f in rx if f[0] == 0x90]
    require(len(requests) == len(grants) == 1 and requests[0][1] == (0x82, b'\0\x09' + string(pattern) + b'\x01')
            and grants[0][1] == (0x90, b'\0\x09\x01') and requests[0][0] < grants[0][0], 'subscription/filter/grant mismatch')
    require(all(f[0] in {0x82, 0x40} for _, f in tx) and all(f[0] in {0x90, 0x32, 0x3a} for _, f in rx),
            'unexpected subscriber frame')
    expected_acks, known, deliveries = [], {}, []
    for i, direction, frame in rows[2:-1]:
        kind, body = frame
        if direction == 'rx' and kind >> 4 == 3:
            value = publication(frame)
            signature = value['topic'], value['payload']
            require(value['qos'] == 1 and not value['retain'] and bool(value['mid']) and i > requests[0][0], 'invalid/out-of-subscription PUBLISH')
            require(signature in sent and i > sent[signature], 'PUBLISH precedes its actual test publication')
            duplicate = value['dup'] and known.get(value['mid']) == signature
            if not duplicate:
                deliveries.append((i, signature))
                known[value['mid']] = signature
            expected_acks.append((value['mid'], i))
        elif direction == 'tx' and kind == 0x40:
            require(len(body) == 2 and expected_acks and int.from_bytes(body, 'big') == expected_acks[0][0],
                    'PUBACK missing/mismatched/reused')
            expected_acks.pop(0)
    require(not expected_acks and [signature for _, signature in deliveries] == expected, 'delivery sequence/ACK incomplete')
    return requests[0][0], grants[0][0], deliveries


def vector_proof(events, row, index):
    start_index, start = one(events, 'topic-vector-start')
    roles_index, roles = one(events, 'topic-vector-roles')
    done_index, done = one(events, 'topic-vector')
    require(start_index == 0 and done_index == len(events) - 1 and start['scenario_version'] == VERSION,
            'vector boundary/version mismatch')
    require(start['index'] == done['index'] == index and re.fullmatch(r'validation/[0-9a-f]{32}', start['root']), 'vector index/namespace mismatch')
    pattern, topic, matches, control = materialize(row, start['root'])
    require((start['filter'], start['topic'], start['matches'], start['control_topic']) == (pattern, topic, matches, control)
            and (done['filter'], done['topic'], done['matches']) == (pattern, topic, matches), 'vector content differs from fixed table')
    connections, frames = capture(events)
    publisher, candidate, positive = [roles[k] for k in ['publisher', 'candidate', 'positive']]
    require(len({publisher, candidate, positive}) == len(connections) == 3
            and set(connections) == {publisher, candidate, positive}
            and len({e['client_id'] for _, e in connections.values()}) == 3, 'role/identity/connection mismatch')
    require(all(frames[c][1][0] < roles_index < frames[c][-1][0] < done_index for c in connections), 'role/cleanup barrier')
    require(not any(e['kind'] == 'connection-aborted' for e in events), 'aborted Topic connection')
    control_sig, target_sig = (control, ('control-' + str(index)).encode()), (topic, ('topic-' + str(index)).encode())
    pub_rows = frames[publisher]
    expected_frames = [('tx', pub_rows[0][2]), ('rx', (0x20, b'\0\0'))]
    for name, payload in [control_sig, target_sig]:
        expected_frames += [('tx', (0x32, string(name) + b'\0\x17' + payload)), ('rx', (0x40, b'\0\x17'))]
    expected_frames += [('tx', (0xe0, b''))]
    require([(d, f) for _, d, f in pub_rows] == expected_frames, 'publisher content/ACK sequence mismatch')
    control_send, target_send = pub_rows[2][0], pub_rows[4][0]
    sent = {control_sig: control_send, target_sig: target_send}
    c_sub, c_grant, c_deliveries = subscriber(events, frames[candidate], pattern,
                                            [control_sig] + ([target_sig] if matches else []), sent)
    p_sub, p_grant, p_deliveries = subscriber(events, frames[positive], topic, [target_sig], sent)
    control_ack = next(i for i, d, f in frames[candidate] if d == 'tx' and f[0] == 0x40)
    require(roles_index < c_sub < c_grant < control_send < c_deliveries[0][0] < control_ack < p_sub < p_grant < target_send,
            'candidate control/positive subscription/target barrier')
    observations = [(i, e) for i, e in enumerate(events) if e['kind'] == 'topic-observation']
    require([e['connection'] for _, e in observations] == [candidate, positive], 'missing/duplicate observation or wrong role')
    # Both target delivery and publisher ACK precede both observation windows.
    bound = max(pub_rows[5][0], c_deliveries[-1][0], p_deliveries[-1][0])
    for i, e in observations:
        c = e['connection']
        ack_indices = [j for j, d, f in frames[c] if d == 'tx' and f[0] == 0x40]
        require(e['completed'] is True and e['seconds_required'] >= 0.4
                and e['actual_seconds'] >= e['seconds_required']
                and e['seconds'] - events[bound]['seconds'] >= e['seconds_required']
                and bound < i < frames[c][-1][0] and max(ack_indices) < i
                and not any(j > i and d == 'rx' for j, d, _ in frames[c]), 'observation lacks completed controls/ACK/window')
    return connections


def check_evidence(events, *, system=False):
    rows = SYSTEM if system else GENERAL
    plan_index, plan = one(events, 'planned-topic-vectors')
    require(plan['vectors'] == [list(row) for row in rows], 'planned vectors differ from fixed table')
    starts = [i for i, e in enumerate(events) if e['kind'] == 'topic-vector-start']
    require(len(starts) == len(rows) and plan_index < starts[0], 'missing/extra vector starts')
    connections, identities, roots = set(), set(), set()
    boundaries = starts[1:] + [len(events)]
    fact_kinds = {'connection', 'bytes', 'frame', 'connection-aborted', 'topic-vector-roles', 'topic-observation', 'topic-vector'}
    require(not any(e['kind'] in fact_kinds for e in events[:starts[0]]), 'wire facts before vectors')
    for index, (begin, end, row) in enumerate(zip(starts, boundaries, rows)):
        # Host shutdown/profile/case events may follow the final vector, but no
        # protocol facts can hide after its completion or between vectors.
        branch = events[begin:end]
        done, _ = one(branch, 'topic-vector')
        require(not any(e['kind'] in fact_kinds for e in branch[done + 1:]), 'protocol facts after vector completion')
        roles = vector_proof(branch[:done + 1], row, index)
        require(not connections.intersection(roles), 'connection reused across vectors')
        new_identities = {e['client_id'] for _, e in roles.values()}
        require(not identities.intersection(new_identities), 'ClientID reused across vectors')
        root = branch[0]['root']
        require(root not in roots, 'vector namespace reused')
        connections.update(roles); identities.update(new_identities); roots.add(root)
