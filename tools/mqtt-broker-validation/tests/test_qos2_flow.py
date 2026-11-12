"""Independent parent proof rejects plausible but incomplete wire evidence."""
from collections import Counter, deque
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from qos2 import mid_bytes
from qos2_flow import MIDS, PAYLOADS, Receiver, controls
from qos2_flow_fixture import evidence
from reporting import check_pass
from wire import Blocked, Client, Trace, packet, string


class Flow(unittest.TestCase):
    def test_legal_ack_permutations_independent_ids_reuse_and_repeated_release(self):
        for options in [dict(),dict(reverse=True),dict(reuse_mid=True),dict(extra_release=True),
                        dict(retransmit=True),dict(reverse=True,reuse_mid=True,extra_release=True,retransmit=True)]:
            with self.subTest(options=options):check_pass(*evidence(**options))

    def test_labels_cannot_replace_wire_identity_and_delivery(self):
        for kind in ['connection','bytes','frame','application-delivery','qos2-flow-start',
                     'qos2-flow-roles','qos2-flow-phase','observation','assertion']:
            events,job=evidence()
            if kind=='assertion': events=[e for e in events if e['kind']!=kind]
            else: events.remove(next(e for e in events if e['kind']==kind))
            with self.subTest(kind=kind),self.assertRaises(ValueError):check_pass(events,job)
        for field,value in [('connection','publisher'),('packet_id',1),('payload_hex','00'),('topic','other'),('count',2)]:
            events,job=evidence();next(e for e in events if e['kind']=='application-delivery')[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):check_pass(events,job)

    def test_consistent_wire_corruption_cannot_pass(self):
        # Alter both capture and decoded frame; capture equality alone is insufficient.
        for old,new in [('50024001','500240ff'),('62024001','620240ff'),('70024001','700240ff'),
                        ('20020000','20020100'),('9003000902','9003000901')]:
            events,job=evidence()
            i=next(i for i,e in enumerate(events) if e['kind']=='frame' and e.get('connection')=='subscriber' and e['hex']==old) if old[:2] not in ['50','70'] else next(i for i,e in enumerate(events) if e['kind']=='bytes' and e.get('connection')=='subscriber' and e.get('direction')=='tx' and e['hex']==old)
            e=events[i];e['hex']=new
            if e['kind']=='frame':events[i-2]['hex']=new[:2];events[i-1]['hex']=new[2:]
            with self.subTest(old=old),self.assertRaises(ValueError):check_pass(events,job)

    def test_missing_second_outstanding_packet_or_colliding_ids_cannot_pass(self):
        for old,new in [('4002','4001'),('34','3c')]:
            events,job=evidence()
            e=next(e for e in events if e['kind']=='frame' and e['hex'].startswith('34'))
            if old=='4002':e=next(e for e in events if e['kind']=='frame' and e['hex'].startswith('34') and '4002' in e['hex'])
            i=events.index(e);e['hex']=e['hex'].replace(old,new,1)
            events[i-2]['hex']=e['hex'][:2];events[i-1]['hex']=e['hex'][2:]
            with self.subTest(old=old),self.assertRaises(ValueError):check_pass(events,job)

    def test_each_duplicate_control_requires_a_real_response(self):
        for kind,direction,connection in [('50','tx','subscriber'),('62','rx','subscriber'),
                                          ('70','tx','subscriber'),('62','tx','publisher'),('70','rx','publisher')]:
            events,job=evidence()
            matches=[i for i,e in enumerate(events) if e['kind']==('frame' if direction=='rx' else 'bytes')
                     and e.get('connection')==connection and e.get('direction')==direction and e['hex'].startswith(kind)]
            i=matches[-2] if connection=='publisher' else matches[1]
            if direction=='rx':del events[i-2:i+1]
            else:del events[i]
            with self.subTest(kind=kind,connection=connection),self.assertRaises(ValueError):check_pass(events,job)

    def test_phase_window_and_cleanup_barriers(self):
        for mutation in ['early-pair','early-health','short-window','early-observation','late-frame','abort']:
            events,job=evidence()
            phases=[e for e in events if e['kind']=='qos2-flow-phase']
            if mutation in ['early-pair','early-health']:
                e=phases[1 if mutation=='early-pair' else 2];events.remove(e);events.insert(0,e)
            elif mutation=='short-window':next(e for e in events if e['kind']=='observation')['actual_seconds']=0.1
            elif mutation=='early-observation':
                e=next(e for e in events if e['kind']=='observation');events.remove(e);events.insert(events.index(phases[2]),e)
            elif mutation=='late-frame':
                e=phases[3];events.remove(e);events.insert(events.index(phases[2])+1,e)
            else:events.append(dict(kind='connection-aborted',connection='subscriber',client_id='subscriber'))
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):check_pass(events,job)

    def test_publish_after_pubrel_and_pubcomp_before_application_cannot_pass(self):
        for mutation in ['publish-after-release','comp-before-delivery','rec-before-second-publish']:
            events,job=evidence()
            delivery=next(e for e in events if e['kind']=='application-delivery')
            if mutation=='publish-after-release':
                i=next(i for i,e in enumerate(events) if e['kind']=='frame' and e['hex'].startswith('34'))
                copied=[dict(e) for e in events[i-2:i+1]]
                copied[0]['hex']='3c';copied[2]['hex']='3c'+copied[2]['hex'][2:]
                events[events.index(delivery)+1:events.index(delivery)+1]=copied
            elif mutation=='comp-before-delivery':
                i=events.index(delivery);events[i],events[i+1]=events[i+1],events[i]
            else:
                rec=next(e for e in events if e['kind']=='bytes' and e.get('connection')=='subscriber' and e.get('direction')=='tx' and e['hex'].startswith('50'))
                events.remove(rec)
                i=next(i for i,e in enumerate(events) if e['kind']=='frame' and e['hex'].startswith('34'))
                events.insert(i+1,rec)
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):check_pass(events,job)

    def client(self):
        temporary=tempfile.TemporaryDirectory(prefix='mqtt-flow-check-');self.addCleanup(temporary.cleanup)
        trace=Trace(Path(temporary.name)/'trace.jsonl')
        stream,peer=socket.socketpair();self.addCleanup(stream.close);self.addCleanup(peer.close)
        stream.settimeout(1)
        client=Client.__new__(Client)
        client.stream,client.trace,client.connection,client.pending=stream,trace,'unit',deque()
        client.identity='unit'
        return client,peer

    def test_control_packets_accept_permutation_and_reject_unknown_id(self):
        for mids in [MIDS[::-1],(MIDS[0],1)]:
            client,peer=self.client()
            peer.sendall(b''.join(packet(0x50,mid_bytes(mid)) for mid in mids))
            if mids==MIDS[::-1]:controls(client,0x50,MIDS)
            else:
                with self.assertRaises(AssertionError):controls(client,0x50,MIDS)

    def test_receiver_repeated_release_delivers_once_and_allows_completed_id_reuse(self):
        client,peer=self.client();receiver=Receiver(client,'topic')
        receiver.publish((0x34,string('topic')+mid_bytes(1)+PAYLOADS[0]),acknowledge=False)
        receiver.release((0x62,mid_bytes(1)));receiver.release((0x62,mid_bytes(1)))
        receiver.publish((0x34,string('topic')+mid_bytes(1)+PAYLOADS[2]),acknowledge=False)
        receiver.release((0x62,mid_bytes(1)))
        self.assertEqual(receiver.deliveries,Counter({PAYLOADS[0]:1,PAYLOADS[2]:1}))
        self.assertEqual(receiver.releases[1],3)

    def test_quiet_window_rejects_partial_frame_and_eof(self):
        for eof in [False,True]:
            client,peer=self.client();receiver=Receiver(client,'topic');receiver.deliveries=Counter(PAYLOADS)
            if eof:peer.close()
            else:peer.sendall(b'\x34')
            with self.subTest(eof=eof),self.assertRaises(EOFError if eof else TimeoutError):receiver.observe()
            self.assertFalse(client.trace.path.exists() and '"kind": "observation"' in client.trace.path.read_text())

    def test_serial_sender_is_blocked_but_partial_second_frame_is_not(self):
        for partial in [False,True]:
            client,peer=self.client();receiver=Receiver(client,'topic')
            frames=[(0x34,string('topic')+mid_bytes(1)+PAYLOADS[0])]
            def next_frame(client,deadline):
                if frames:return frames.pop()
                if partial:client.trace.event('bytes',connection='unit',direction='rx',hex='34')
                raise TimeoutError('test deadline')
            with patch('qos2_flow.next_frame',side_effect=next_frame):
                with self.subTest(partial=partial),self.assertRaises(TimeoutError if partial else Blocked):receiver.pair()


if __name__=='__main__':unittest.main()
