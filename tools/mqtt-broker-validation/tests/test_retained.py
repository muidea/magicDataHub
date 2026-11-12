"""Retained v2 distinguishes real delivery gaps from permitted QoS1 retransmits."""
import collections
from pathlib import Path
import socket
import struct
import tempfile
import unittest

from common import InputError, ROOT, read_json
from reporting import check_pass
from retained import PHASES, VERSION, Receiver, check_evidence
from schema_check import check
from wire import Client, Trace, packet, read_frame, string


class RetainedOracle(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mqtt-retained-check-')
        self.addCleanup(self.temporary.cleanup)
        self.trace = Trace(Path(self.temporary.name) / 'trace.jsonl')
        self.stream, self.peer = socket.socketpair()
        self.addCleanup(self.stream.close)
        self.addCleanup(self.peer.close)
        self.stream.settimeout(1)
        self.peer.settimeout(1)
        self.client = Client.__new__(Client)
        self.client.stream, self.client.trace = self.stream, self.trace
        self.client.connection, self.client.identity = 'receiver', 'unit'
        self.client.pending = collections.deque()
        self.receiver = Receiver(self.client, 'topic')

    def message(self, payload, *, mid=81, dup=False, retain=False):
        return packet(0x32 | int(retain) | (8 if dup else 0), string('topic') + struct.pack('!H', mid) + payload)

    def test_publish_before_suback_and_identical_qos1_duplicate_are_accepted_and_acked(self):
        self.peer.sendall(self.message(b'old', retain=True) + packet(0x90, b'\x00\x09\x01'))
        self.client.expect(0x90, b'\x00\x09\x01')
        self.receiver.delivery(b'old', True)
        self.peer.sendall(self.message(b'old', dup=True, retain=True) + self.message(b'live', mid=95))
        self.receiver.delivery(b'live', False)
        self.assertEqual([read_frame(self.peer) for _ in range(3)], [(0x40, b'\x00Q'), (0x40, b'\x00Q'), (0x40, b'\x00_')])

    def test_first_observed_dup_and_packet_id_reuse_for_different_expected_message_are_legal(self):
        self.peer.sendall(self.message(b'old', dup=True, retain=True))
        self.receiver.delivery(b'old', True)
        self.peer.sendall(self.message(b'live', dup=True))
        self.receiver.delivery(b'live', False)

    def establish_health(self):
        self.peer.sendall(self.message(b'live'))
        self.receiver.delivery(b'live', False)

    def test_quiet_window_accepts_known_duplicate_without_treating_it_as_new_application_delivery(self):
        self.establish_health()
        self.peer.sendall(self.message(b'live', dup=True))
        self.receiver.no_new_delivery()
        events = [read_json_line for read_json_line in self.trace.path.read_text().splitlines()]
        self.assertTrue(any('retained-observation' in line for line in events))

    def test_old_empty_changed_or_unmarked_duplicate_cannot_pass_quiet_window(self):
        for raw in [self.message(b'old', retain=True), self.message(b'', retain=True),
                    self.message(b'changed', dup=True), self.message(b'live'), self.message(b'live', mid=96, dup=True)]:
            with self.subTest(raw=raw.hex()):
                self.establish_health()
                self.peer.sendall(raw)
                with self.assertRaisesRegex(AssertionError, 'retained-delivery'):
                    self.receiver.no_new_delivery()
                self.receiver.seen.clear()

    def test_partial_frame_or_eof_cannot_be_reported_as_valid_quiet_window(self):
        self.establish_health()
        self.assertEqual(read_frame(self.peer), (0x40, b'\x00Q'))
        self.peer.sendall(b'\x32')
        with self.assertRaises(TimeoutError):
            self.receiver.no_new_delivery()
        self.peer.close()
        with self.assertRaises(EOFError):
            self.receiver.no_new_delivery()

    def evidence(self, *, duplicate=False):
        events = []
        def add(kind, **values):events.append(dict(kind=kind, **values))
        def frame(connection, direction, kind, body=b''):
            raw = packet(kind, body)
            if direction == 'rx':
                for part in [raw[:1], raw[1:]]:add('bytes', connection=connection, direction=direction, hex=part.hex())
                add('frame', connection=connection, direction=direction, hex=raw.hex())
            else:add('bytes', connection=connection, direction=direction, hex=raw.hex())
        def connect(connection):
            add('connection', connection=connection, peer=['127.0.0.1', 1883])
            frame(connection, 'tx', 0x10, b'\x00\x04MQTT\x04\x02\x00\x1e' + string(connection))
            frame(connection, 'rx', 0x20, b'\x00\x00')
        def pub(payload, retain):
            frame('publisher', 'tx', 0x32 | retain, string('topic') + b'\x00\x17' + payload)
            frame('publisher', 'rx', 0x40, b'\x00\x17')
        def subscribe(connection):
            frame(connection, 'tx', 0x82, b'\x00\x09' + string('topic') + b'\x01')
        def grant(connection):frame(connection, 'rx', 0x90, b'\x00\x09\x01')
        def receive(connection, payload, retain, mid, dup=False):
            frame(connection, 'rx', 0x32 | retain | (8 if dup else 0), string('topic') + struct.pack('!H', mid) + payload)
            frame(connection, 'tx', 0x40, struct.pack('!H', mid))
        def phase(index, connection):add('retained-phase', phase=PHASES[index], connection=connection, topic='topic', scenario_version=VERSION)
        def observe(connection):add('retained-observation', connection=connection, topic='topic', seconds_required=0.4, actual_seconds=0.4, completed=True)
        connect('publisher'); connect('early')
        pub(b'old', True); subscribe('early')
        receive('early', b'old', True, 81); grant('early'); phase(0, 'early')
        pub(b'transient-before-delete', False); receive('early', b'transient-before-delete', False, 82)
        connect('probe');subscribe('probe');grant('probe');receive('probe', b'old', True, 91);phase(1,'probe')
        pub(b'',True);receive('early',b'',False,83);phase(2,'early')
        connect('late');subscribe('late');grant('late')
        pub(b'live-control',False);receive('late',b'live-control',False,121);receive('early',b'live-control',False,84)
        if duplicate:receive('late',b'live-control',False,121,dup=True)
        observe('late');phase(3,'late')
        connect('post');subscribe('post');grant('post')
        pub(b'post-control',False);receive('post',b'post-control',False,151)
        receive('late',b'post-control',False,122);receive('early',b'post-control',False,85)
        observe('post');phase(4,'post')
        pub(b'',True);phase(5,'publisher')
        for name in ['retained-delivery','retained-observation-completed']:add('assertion',assertion=name,passed=True)
        for index, event in enumerate(events):
            event.update(schema_version=1,sequence=index,seconds=index * 0.2)
            check('event',event)
        return events

    def test_parent_accepts_complete_wire_proof_with_early_delivery_and_legal_duplicate(self):
        for duplicate in [False,True]:
            events=self.evidence(duplicate=duplicate)
            check_evidence(events)
            case=next(c for c in read_json(ROOT/'catalog.json')['cases'] if c.get('entrypoint')=='retained')
            check_pass(events,dict(case=case,target='tool',resources=None))

    def test_success_labels_cannot_replace_missing_phase_raw_bytes_handshake_or_positive_control(self):
        for mutate in ['phase','raw','publisher-ack','receiver-ack','grant','control','observation','reorder','topic']:
            events=self.evidence()
            if mutate=='phase':events.remove(next(e for e in events if e['kind']=='retained-phase'))
            elif mutate=='raw':next(e for e in events if e['kind']=='bytes' and e['direction']=='rx')['hex']='ff'
            elif mutate=='publisher-ack':
                for e in events:
                    if e['kind']=='frame' and e['connection']=='publisher' and e['hex']=='40020017':e['hex']='40020018';break
            elif mutate=='receiver-ack':events.remove(next(e for e in events if e['kind']=='bytes' and e['connection']=='post' and e['direction']=='tx' and e['hex'].startswith('40')))
            elif mutate=='grant':next(e for e in events if e['kind']=='frame' and e['connection']=='post' and e['hex'].startswith('90'))['hex']='9003000980'
            elif mutate=='control':next(e for e in events if e['kind']=='retained-phase' and e['phase']=='post-control-empty')['connection']='other'
            elif mutate=='observation':events.remove(next(e for e in events if e['kind']=='retained-observation'))
            elif mutate=='reorder':
                a=next(e for e in events if e['kind']=='retained-phase' and e['phase']=='old-replayed')
                events.remove(a);events.insert(0,a)
            else:next(e for e in events if e['kind']=='retained-phase')['topic']='other'
            with self.subTest(mutation=mutate),self.assertRaises((InputError,AssertionError)):
                check_evidence(events)


if __name__ == '__main__':
    unittest.main()
