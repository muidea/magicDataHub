"""Opposite directions with the same ID must have independent wire evidence."""
from collections import Counter,deque
from pathlib import Path
import socket
import tempfile
import time
import unittest

from qos2 import mid_bytes
from qos2_duplex import INPUTS,OUTPUTS,Receiver
from qos2_duplex_fixture import evidence
from reporting import check_pass
from wire import Client,Trace,packet,string


def frame_index(events,c,d,prefix,ordinal=0):
    kind='frame' if d=='rx' else 'bytes'
    return [i for i,e in enumerate(events) if e['kind']==kind and e.get('connection')==c
            and e.get('direction')==d and e['hex'].startswith(prefix)][ordinal]


def replace_frame(events,i,raw):
    e=events[i];e['hex']=raw
    if e['kind']=='frame':events[i-2]['hex']=raw[:2];events[i-1]['hex']=raw[2:]


class Duplex(unittest.TestCase):
    def test_legal_broker_ids_reuse_early_forwarding_retransmission_and_repeated_release(self):
        for options in [dict(),dict(broker_ids=(9,0x4567)),dict(reuse_mid=True),dict(retransmit=True),
                        dict(extra_release=True),dict(early=True),dict(early_source_ack=True),
                        dict(broker_ids=(0xffff,1),reuse_mid=True,retransmit=True,extra_release=True,early=True,early_source_ack=True)]:
            with self.subTest(options=options):check_pass(*evidence(**options))

    def test_different_direction_ids_or_wrong_completion_order_cannot_claim_this_case(self):
        for options in [dict(same_id=False),dict(wrong_order=True)]:
            with self.subTest(options=options),self.assertRaises(ValueError):check_pass(*evidence(**options))

    def test_each_branch_role_connection_raw_frame_delivery_and_window_is_required(self):
        for kind in ['connection','bytes','frame','application-delivery','qos2-duplex-branch-start',
                     'qos2-duplex-roles','qos2-duplex-phase','qos2-duplex-observation-start',
                     'qos2-duplex-observation','qos2-duplex-branch-completed','assertion']:
            events,job=evidence()
            if kind=='assertion':events=[e for e in events if e['kind']!=kind]
            else:events.remove(next(e for e in events if e['kind']==kind))
            with self.subTest(kind=kind),self.assertRaises(ValueError):check_pass(events,job)

    def test_equal_id_does_not_make_pubrec_pubrel_or_pubcomp_interchangeable(self):
        for d,old,new in [('rx','50','62'),('rx','70','62'),('tx','62','50'),('tx','70','62')]:
            events,job=evidence();i=frame_index(events,'0-owner',d,old)
            replace_frame(events,i,new+events[i]['hex'][2:])
            with self.subTest(direction=d,old=old),self.assertRaises(ValueError):check_pass(events,job)

    def test_consistent_wrong_ids_payload_topic_and_grant_cannot_pass(self):
        for d,prefix in [('rx','50'),('rx','70'),('rx','62'),('tx','50'),('tx','70'),('rx','34'),('rx','90')]:
            events,job=evidence();i=frame_index(events,'0-owner',d,prefix)
            replace_frame(events,i,events[i]['hex'][:-2]+'ff')
            with self.subTest(direction=d,prefix=prefix),self.assertRaises(ValueError):check_pass(events,job)

    def test_application_labels_cannot_move_between_directions_or_connections(self):
        for field,value in [('connection','0-helper'),('packet_id',1),('topic','other'),('payload_hex','00'),('count',2)]:
            events,job=evidence();next(e for e in events if e['kind']=='application-delivery' and e['connection']=='0-owner')[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):check_pass(events,job)

    def test_first_direction_must_finish_while_the_other_is_outstanding(self):
        for phase in ['overlap-established','first-direction-completed','both-directions-completed']:
            events,job=evidence();e=next(e for e in events if e['kind']=='qos2-duplex-phase' and e['phase']==phase)
            events.remove(e);events.insert(0,e)
            with self.subTest(phase=phase),self.assertRaises(ValueError):check_pass(events,job)

    def test_healthy_input_must_reuse_its_completed_id(self):
        events,job=evidence();i=frame_index(events,'0-owner','tx','34',1)
        events[i]['hex']=events[i]['hex'].replace('1234','1235')
        with self.assertRaises(ValueError):check_pass(events,job)

    def test_each_repeated_release_requires_its_own_ack_without_redelivery(self):
        for mutation in ['ack','delivery']:
            events,job=evidence(extra_release=True);i=frame_index(events,'0-owner','tx','70',-1)
            if mutation=='ack':events.pop(i)
            else:events.insert(i,dict(next(e for e in events if e['kind']=='application-delivery' and e['connection']=='0-owner')))
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):check_pass(events,job)

    def test_branch_identity_facts_and_observations_cannot_leak(self):
        for mutation in ['role','client-id','branch','outside','short','fake-elapsed','borrow-window','cleanup']:
            events,job=evidence()
            if mutation=='role':next(e for e in events if e['kind']=='qos2-duplex-roles')['helper']='0-owner'
            elif mutation=='client-id':next(e for e in events if e['kind']=='connection' and e['connection']=='1-owner')['client_id']='0-owner'
            elif mutation=='branch':next(e for e in events if e['kind']=='qos2-duplex-phase')['branch']='outgoing-first'
            elif mutation=='outside':
                i=frame_index(events,'0-owner','tx','e0');e=events.pop(i);events.append(e)
            elif mutation=='short':next(e for e in events if e['kind']=='qos2-duplex-observation')['actual_seconds']=0.1
            elif mutation=='fake-elapsed':
                start=next(e for e in events if e['kind']=='qos2-duplex-observation-start')
                next(e for e in events if e['kind']=='qos2-duplex-observation')['seconds']=start['seconds']+0.1
            elif mutation=='borrow-window':next(e for e in events if e['kind']=='qos2-duplex-observation' and e['connection']=='0-helper')['connection']='0-owner'
            else:events.pop(frame_index(events,'0-owner','tx','e0'))
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):check_pass(events,job)

    def client(self):
        temp=tempfile.TemporaryDirectory(prefix='mqtt-duplex-check-');self.addCleanup(temp.cleanup)
        stream,peer=socket.socketpair();self.addCleanup(stream.close);self.addCleanup(peer.close);stream.settimeout(1)
        client=Client.__new__(Client)
        client.stream,client.trace,client.connection,client.pending=stream,Trace(Path(temp.name)/'trace.jsonl'),'unit',deque()
        client.identity='unit';return client,peer

    def test_receiver_preserves_outbound_state_when_same_id_incoming_ack_arrives(self):
        client,peer=self.client();receiver=Receiver(client,'out',OUTPUTS,enabled=False)
        receiver.publication((0x34,string('out')+mid_bytes(7)+OUTPUTS[0]))
        peer.sendall(packet(0x50,mid_bytes(7)));receiver.control(0x50,7)
        self.assertIn(7,receiver.pending);self.assertFalse(receiver.enabled)
        peer.sendall(packet(0x62,mid_bytes(7)));receiver.receive(OUTPUTS[0])
        self.assertEqual(receiver.deliveries[OUTPUTS[0]],1)
        peer.sendall(packet(0x62,mid_bytes(7))+packet(0x70,mid_bytes(7)));receiver.control(0x70,7)
        self.assertEqual(receiver.deliveries[OUTPUTS[0]],1)

    def test_wrong_kind_cannot_satisfy_incoming_ack_with_same_id(self):
        client,peer=self.client();receiver=Receiver(client,'out',OUTPUTS,enabled=False)
        receiver.publication((0x34,string('out')+mid_bytes(7)+OUTPUTS[0]))
        peer.sendall(packet(0x62,mid_bytes(7)))
        with self.assertRaises(AssertionError):receiver.control(0x50,7)

    def test_observation_rejects_partial_frame_eof_and_duplicate_application(self):
        for mutation in ['partial','eof','duplicate']:
            client,peer=self.client();receiver=Receiver(client,'out',OUTPUTS);receiver.deliveries=Counter(OUTPUTS)
            if mutation=='partial':peer.sendall(b'\x34')
            elif mutation=='eof':peer.close()
            else:peer.sendall(packet(0x34,string('out')+mid_bytes(7)+OUTPUTS[0]))
            with self.subTest(mutation=mutation),self.assertRaises({'partial':TimeoutError,'eof':EOFError,'duplicate':AssertionError}[mutation]):receiver.observe('incoming-first')
            self.assertNotIn('"kind": "qos2-duplex-observation"',client.trace.path.read_text())


if __name__=='__main__':unittest.main()
