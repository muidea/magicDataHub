"""Mixed QoS oracle must accept legal copies and reject unsupported PASS."""
from collections import deque
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from mixed_qos import PAYLOAD, Receiver
from mixed_qos_fixture import evidence
from qos2 import mid_bytes
from reporting import check_pass
from test_qos2_fanout import frame_index, replace_frame
from wire import Blocked, Client, Trace, packet, string


class MixedQoS(unittest.TestCase):
    def test_legal_grants_optional_copies_retries_and_early_forwarding(self):
        for options in [dict(),dict(lower=True),dict(retry=True),dict(extra_release=True),dict(early=True),
                        dict(best_effort_duplicates=True),dict(lower=True,retry=True,extra_release=True,early=True)]:
            with self.subTest(options=options):check_pass(*evidence(**options))

    def test_all_raw_evidence_and_matrix_boundaries_required(self):
        for kind in ['connection','bytes','frame','mixed-vector-start','mixed-roles','mixed-input-completed',
                     'mixed-delivery','mixed-observation-start','mixed-observation','mixed-vector-completed','assertion']:
            events,job=evidence();events.remove(next(e for e in events if e['kind']==kind))
            with self.subTest(kind=kind),self.assertRaises(ValueError):check_pass(events,job)

    def test_wrong_qos_and_payload_rejected_with_consistent_raw_capture(self):
        for mutation in ['qos','payload','retain','dup']:
            events,job=evidence();i=frame_index(events,'8-subscriber','rx','34');raw=events[i]['hex']
            if mutation=='qos':raw='32'+raw[2:]
            elif mutation=='payload':raw=raw[:-2]+'00'
            elif mutation=='retain':raw='35'+raw[2:]
            else:raw='3c'+raw[2:]
            replace_frame(events,i,raw)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):check_pass(events,job)

    def test_actual_grant_not_requested_qos_is_the_oracle(self):
        events,job=evidence();i=frame_index(events,'8-subscriber','rx','90')
        replace_frame(events,i,events[i]['hex'][:-2]+'01')
        with self.assertRaises(ValueError):check_pass(events,job)

    def test_higher_or_rejected_grant_cannot_pass(self):
        for grant in ['01','80']:
            events,job=evidence();i=frame_index(events,'0-subscriber','rx','90')
            replace_frame(events,i,events[i]['hex'][:-2]+grant)
            with self.subTest(grant=grant),self.assertRaises(ValueError):check_pass(events,job)

    def test_delivery_cannot_be_borrowed_or_forged(self):
        for field,value in [('connection','0-publisher'),('packet_id',111),('qos',2),('topic','wrong'),('payload_hex','00')]:
            events,job=evidence();next(e for e in events if e['kind']=='mixed-delivery')[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):check_pass(events,job)

    def test_own_qos1_and_qos2_ack_required(self):
        for c,prefix in [('4-subscriber','40'),('8-subscriber','50'),('8-subscriber','70'),('8-publisher','62')]:
            events,job=evidence();events.pop(frame_index(events,c,'tx',prefix))
            with self.subTest(c=c,prefix=prefix),self.assertRaises(ValueError):check_pass(events,job)

    def test_observation_and_roles_cannot_be_forged(self):
        for mutation in ['short','fake-time','borrow','duplicate-index','reused-role','missing-disconnect']:
            events,job=evidence()
            if mutation=='short':next(e for e in events if e['kind']=='mixed-observation')['actual_seconds']=0.1
            elif mutation=='fake-time':
                start=next(e for e in events if e['kind']=='mixed-observation-start')
                next(e for e in events if e['kind']=='mixed-observation')['seconds']=start['seconds']+0.1
            elif mutation=='borrow':next(e for e in events if e['kind']=='mixed-observation')['connection']='1-subscriber'
            elif mutation=='duplicate-index':next(e for e in events if e['kind']=='mixed-vector-start' and e['index']==1)['index']=0
            elif mutation=='reused-role':next(e for e in events if e['kind']=='mixed-roles')['subscriber']='1-subscriber'
            else:events.pop(frame_index(events,'0-subscriber','tx','e0'))
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):check_pass(events,job)

    def client(self):
        temp=tempfile.TemporaryDirectory(prefix='mqtt-mixed-check-');self.addCleanup(temp.cleanup)
        stream,peer=socket.socketpair();self.addCleanup(stream.close);self.addCleanup(peer.close);stream.settimeout(1)
        client=Client.__new__(Client)
        client.stream,client.trace,client.connection,client.pending=stream,Trace(Path(temp.name)/'trace.jsonl'),'unit',deque()
        client.identity='unit'
        return client,peer

    def test_receiver_handles_all_qos_and_distinct_optional_copies(self):
        client,peer=self.client();receiver=Receiver(client,'topic',2,[0,1,2])
        for qos in range(3):
            peer.sendall(packet(0x30|qos<<1,string('topic')+(mid_bytes(1) if qos else b'')+PAYLOAD))
            receiver.step(time.monotonic()+1)
            if qos==2:
                peer.sendall(packet(0x62,mid_bytes(1)));receiver.step(time.monotonic()+1)
        self.assertEqual(receiver.received,{0:1,1:1,2:1})

    def test_qos2_repeated_release_does_not_redeliver(self):
        client,peer=self.client();receiver=Receiver(client,'topic',2,[2])
        peer.sendall(packet(0x34,string('topic')+mid_bytes(1)+PAYLOAD)+packet(0x62,mid_bytes(1))*2)
        for _ in range(3):receiver.step(time.monotonic()+1)
        self.assertEqual(receiver.received,{2:1})

    def test_qos1_to_qos0_permitted_duplicate_copies(self):
        client,peer=self.client();receiver=Receiver(client,'topic',1,[0])
        peer.sendall(packet(0x30,string('topic')+PAYLOAD)*2)
        for _ in range(2):receiver.step(time.monotonic()+1)
        self.assertEqual(receiver.received,{0:2})

    def test_absent_best_effort_positive_control_is_blocked(self):
        client,_=self.client();receiver=Receiver(client,'topic',0,[2])
        with patch('mixed_qos.select.select',return_value=([],[],[])),self.assertRaises(Blocked):receiver.run()

    def test_missing_reliable_maximum_qos_has_explicit_failure(self):
        client,_=self.client();receiver=Receiver(client,'topic',2,[0,1,2])
        receiver.received[0]=1
        with patch('mixed_qos.select.select',return_value=([],[],[])),self.assertRaises(AssertionError):receiver.run()
        self.assertIn('mixed-required-qos-positive-control',client.trace.path.read_text())

    def test_observation_cannot_succeed_on_partial_frame_eof_or_wrong_copy(self):
        for mutation in ['partial','eof','duplicate']:
            client,peer=self.client();receiver=Receiver(client,'topic',0,[0]);receiver.received[0]=1;receiver.used[0]=1
            if mutation=='partial':peer.sendall(b'\x30')
            elif mutation=='eof':peer.close()
            else:peer.sendall(packet(0x30,string('topic')+PAYLOAD))
            error={'partial':TimeoutError,'eof':EOFError,'duplicate':AssertionError}[mutation]
            with self.subTest(mutation=mutation),self.assertRaises(error):receiver.run()
            self.assertNotIn('"kind": "mixed-observation"',client.trace.path.read_text())


if __name__=='__main__':unittest.main()
