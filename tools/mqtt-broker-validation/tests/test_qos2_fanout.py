"""Every subscriber must supply its own actual exchange and delivery proof."""
from collections import Counter, deque
from pathlib import Path
import socket
import tempfile
import time
import unittest

from qos2 import mid_bytes
from qos2_fanout import MID, PAYLOADS, Receiver, send_input
from qos2_fanout_fixture import evidence
from reporting import check_pass
from wire import Client, packet, string


def frame_index(events,connection,direction,prefix,ordinal=0):
    kind='frame' if direction=='rx' else 'bytes'
    return [i for i,e in enumerate(events) if e['kind']==kind and e.get('connection')==connection
            and e.get('direction')==direction and e['hex'].startswith(prefix)][ordinal]


def replace_frame(events,i,raw):
    event=events[i];event['hex']=raw
    if event['kind']=='frame':events[i-2]['hex']=raw[:2];events[i-1]['hex']=raw[2:]


class Fanout(unittest.TestCase):
    def test_legal_shared_or_independent_ids_reuse_retransmission_and_early_forwarding(self):
        for options in [dict(),dict(shared_ids=False),dict(reuse_mid=True),dict(extra_release=True),
                        dict(retransmit=True),dict(early=True),dict(shared_ids=False,reuse_mid=True,retransmit=True,extra_release=True,early=True)]:
            with self.subTest(options=options):check_pass(*evidence(**options))

    def test_each_connection_and_all_raw_evidence_are_required(self):
        for kind in ['connection','bytes','frame','application-delivery','qos2-fanout-start',
                     'qos2-fanout-roles','qos2-fanout-phase','qos2-fanout-observation','qos2-fanout-observation-start','assertion']:
            events,job=evidence()
            if kind=='assertion':events=[e for e in events if e['kind']!=kind]
            else:events.remove(next(e for e in events if e['kind']==kind))
            with self.subTest(kind=kind),self.assertRaises(ValueError):check_pass(events,job)

    def test_delivery_cannot_be_borrowed_from_another_recipient(self):
        for field,value in [('connection','a'),('packet_id',1),('topic','wrong'),('payload_hex','00'),('count',2)]:
            events,job=evidence();next(e for e in events if e['kind']=='application-delivery' and e['connection']=='b')[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):check_pass(events,job)

    def test_equal_packet_ids_do_not_allow_borrowing_another_connections_ack(self):
        for prefix in ['50','70']:
            events,job=evidence()
            i=frame_index(events,'b','tx',prefix)
            events[i]['connection']='a'
            with self.subTest(prefix=prefix),self.assertRaises(ValueError):check_pass(events,job)

    def test_consistent_wrong_wire_content_ack_and_grant_are_rejected(self):
        for direction,prefix in [('tx','50'),('tx','70'),('rx','62'),('rx','90'),('rx','34')]:
            events,job=evidence(shared_ids=False)
            i=frame_index(events,'b',direction,prefix);raw=events[i]['hex']
            if prefix=='90':raw=raw[:-2]+'01'
            elif prefix=='34':raw=raw[:-2]+'00'
            else:raw=raw[:-2]+'ff'
            replace_frame(events,i,raw)
            with self.subTest(prefix=prefix),self.assertRaises(ValueError):check_pass(events,job)

    def test_duplicate_input_must_precede_reading_pubrec_and_cannot_be_omitted(self):
        for mutation in ['omit','after-rec','different-payload','different-id']:
            events,job=evidence();i=frame_index(events,'publisher','tx','3c');e=events[i]
            if mutation=='omit':events.pop(i)
            elif mutation=='after-rec':
                events.pop(i);i=frame_index(events,'publisher','rx','50');events.insert(i+1,e)
            elif mutation=='different-payload':e['hex']=e['hex'][:-2]+'00'
            else:e['hex']=e['hex'].replace('3456','3457')
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):check_pass(events,job)

    def test_health_must_reuse_completed_input_id_and_real_payload(self):
        events,job=evidence();i=frame_index(events,'publisher','tx','34',1)
        events[i]['hex']=events[i]['hex'].replace('3456','3457')
        with self.assertRaises(ValueError):check_pass(events,job)

    def test_repeated_pubrel_needs_another_pubcomp_without_another_delivery(self):
        for mutation in ['missing-comp','extra-delivery']:
            events,job=evidence(extra_release=True)
            i=frame_index(events,'a','tx','70',-1)
            if mutation=='missing-comp':events.pop(i)
            else:
                delivery=next(e for e in events if e['kind']=='application-delivery' and e['connection']=='a')
                events.insert(i,dict(delivery))
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):check_pass(events,job)

    def test_phases_per_recipient_windows_and_cleanup_cannot_be_forged(self):
        for mutation in ['short','borrow-window','fake-window','overlap','early-phase','no-disconnect','abort']:
            events,job=evidence()
            if mutation=='short':next(e for e in events if e['kind']=='qos2-fanout-observation')['actual_seconds']=0.1
            elif mutation=='borrow-window':next(e for e in events if e['kind']=='qos2-fanout-observation' and e['connection']=='b')['connection']='a'
            elif mutation=='fake-window':
                end=next(e for e in events if e['kind']=='qos2-fanout-observation')
                start=next(e for e in events if e['kind']=='qos2-fanout-observation-start')
                end['seconds']=start['seconds']+0.1
            elif mutation=='overlap':
                start=next(e for e in events if e['kind']=='qos2-fanout-observation-start' and e['connection']=='b')
                events.remove(start);i=next(i for i,e in enumerate(events) if e['kind']=='qos2-fanout-observation');events.insert(i,start)
            elif mutation=='early-phase':
                e=next(e for e in events if e['kind']=='qos2-fanout-phase' and e['phase']=='health-fanout-completed');events.remove(e);events.insert(0,e)
            elif mutation=='no-disconnect':events.pop(frame_index(events,'b','tx','e0'))
            else:events.append(dict(kind='connection-aborted',connection='b',client_id='b'))
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):check_pass(events,job)

    def client(self):
        from wire import Trace
        temp=tempfile.TemporaryDirectory(prefix='mqtt-fanout-check-');self.addCleanup(temp.cleanup)
        stream,peer=socket.socketpair();self.addCleanup(stream.close);self.addCleanup(peer.close);stream.settimeout(1)
        client=Client.__new__(Client)
        client.stream,client.trace,client.connection,client.pending=stream,Trace(Path(temp.name)/'trace.jsonl'),'unit',deque()
        client.identity='unit'
        return client,peer

    def test_receiver_deduplicates_repeated_release_and_reuses_completed_id(self):
        client,peer=self.client();receiver=Receiver(client,'topic')
        for payload in PAYLOADS:
            peer.sendall(packet(0x34,string('topic')+mid_bytes(1)+payload)+packet(0x62,mid_bytes(1)))
            receiver.receive(payload)
            peer.sendall(packet(0x62,mid_bytes(1)));receiver.step(time.monotonic()+1)
        self.assertEqual(receiver.deliveries,Counter(PAYLOADS))

    def test_observation_rejects_partial_frame_eof_and_duplicate_onward(self):
        for mutation in ['partial','eof','duplicate']:
            client,peer=self.client();receiver=Receiver(client,'topic');receiver.deliveries=Counter(PAYLOADS)
            if mutation=='partial':peer.sendall(b'\x34')
            elif mutation=='eof':peer.close()
            else:peer.sendall(packet(0x34,string('topic')+mid_bytes(1)+PAYLOADS[0]))
            error={'partial':TimeoutError,'eof':EOFError,'duplicate':AssertionError}[mutation]
            with self.subTest(mutation=mutation),self.assertRaises(error):receiver.observe()
            self.assertFalse(client.trace.path.exists() and '"kind": "qos2-fanout-observation"' in client.trace.path.read_text())

    def test_input_ack_must_confirm_both_prequeued_publications(self):
        client,peer=self.client()
        peer.sendall(packet(0x50,mid_bytes(MID))+packet(0x70,mid_bytes(MID)))
        with self.assertRaises(AssertionError):send_input(client,'topic',PAYLOADS[0],repeat=True)
        with self.assertRaises(AssertionError):
            receiver=Receiver(client,'topic')
            peer.sendall(packet(0x62,mid_bytes(99)));receiver.step(time.monotonic()+1)


if __name__=='__main__':unittest.main()
