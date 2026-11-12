"""Recovery assertions accept legal retransmissions and reject missing evidence."""
from pathlib import Path
from collections import Counter, deque
import socket
import tempfile
import threading
import unittest

from calibration_model import RECOVERY_BRANCHES
from common import ROOT, read_json
from qos2 import Receiver, recovery_peer, resume_probe
from reporting import check_pass
from wire import Client, Trace
from qos2_fixture import evidence


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mqtt-qos2-check-')
        self.addCleanup(self.temporary.cleanup)
        self.trace = Trace(Path(self.temporary.name) / 'trace.jsonl')

    def test_legal_resume_uses_broker_assigned_ids_and_preserves_delivery_state(self):
        for phase in ['AwaitPubRec', 'AwaitPubComp']:
            errors = []
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                listener.listen(2)
                listener.settimeout(5)
                def peer():
                    try:
                        recovery_peer(listener, None, phase, self.trace)
                    except Exception as error:
                        errors.append(error)
                worker = threading.Thread(target=peer)
                worker.start()
                try:
                    resume_probe(listener.getsockname(), phase, self.trace)
                finally:
                    worker.join(timeout=6)
                self.assertFalse(worker.is_alive())
                self.assertFalse(errors, errors)

    def evidence(self):
        return evidence()

    def test_wire_replay_accepts_early_forwarding_id_reuse_and_repeated_pubrel(self):
        for options in [dict(early_delivery=True),dict(reuse_mid=True),dict(repeated_pubrel=True),
                        dict(early_delivery=True,reuse_mid=True,repeated_pubrel=True)]:
            events,job=evidence(**options)
            check_pass(events,job)

    def test_missing_or_duplicate_branch_cannot_pass(self):
        events, job = self.evidence()
        check_pass(events, job)
        for kind in ['qos2-branch-completed', 'qos2-disconnect', 'qos2-clean-session-reset', 'fault-cut', 'observation']:
            subset = events.copy()
            subset.remove(next(x for x in subset if x['kind'] == kind))
            with self.assertRaises(ValueError):
                check_pass(subset, job)
        duplicate = events + [next(x for x in events if x['kind'] == 'qos2-branch-completed')]
        with self.assertRaises(ValueError):
            check_pass(duplicate, job)

    def test_missing_branch_delivery_or_assertion_cannot_pass(self):
        events, job = self.evidence()
        for kind in ['application-delivery', 'assertion']:
            subset = events.copy()
            subset.remove(next(x for x in subset if x['kind'] == kind and x.get('phase') == 'AwaitPubRec'
                               and (kind != 'assertion' or x.get('assertion') == 'qos2-health-control-and-single-delivery')))
            with self.assertRaises(ValueError):
                check_pass(subset, job)

    def test_forwarded_ack_cannot_claim_unreceived_ack(self):
        events, job = self.evidence()
        next(x for x in events if x['kind'] == 'fault-cut')['forwarded'] = True
        with self.assertRaisesRegex(ValueError, 'ACK cut'):
            check_pass(events, job)

    def test_labels_cannot_replace_connection_bytes_proxy_or_delivery_proof(self):
        mutations = ['start','version','identity','namespace','connection','raw','frame','proxy-receive',
                     'proxy-forward','cut-raw','proxy-cleanup','receiver-ack','delivery-connection',
                     'delivery-content','reset','observation','abort','wire-phase']
        for mutation in mutations:
            events,job=self.evidence()
            def first(kind, **values):
                return next(e for e in events if e['kind']==kind and all(e.get(k)==v for k,v in values.items()))
            if mutation=='start': events.remove(first('qos2-branch-start'))
            elif mutation=='version': first('qos2-branch-start')['scenario_version']='1.0.0'
            elif mutation=='identity': first('qos2-branch-start')['client_id']='other'
            elif mutation=='namespace': first('qos2-branch-start')['topic']='other'
            elif mutation=='connection': events.remove(first('connection'))
            elif mutation=='raw': first('bytes',direction='rx')['hex']='ff'
            elif mutation=='frame': events.remove(first('frame'))
            elif mutation in ['proxy-receive','proxy-forward']:
                first('proxy-bytes',operation=mutation.split('-')[1])['hex']='ff'
            elif mutation=='cut-raw': first('fault-cut')['hex']='50021235'
            elif mutation=='proxy-cleanup': first('proxy-cleanup')['complete']=False
            elif mutation=='receiver-ack':
                e=next(e for e in events if e['kind']=='bytes' and e['direction']=='tx' and e['hex'].startswith('70'))
                events.remove(e)
            elif mutation=='delivery-connection': first('application-delivery')['connection']='other'
            elif mutation=='delivery-content': first('application-delivery')['payload_hex']='00'
            elif mutation=='reset': first('qos2-clean-session-reset')['client_id']='other'
            elif mutation=='observation': first('observation')['actual_seconds']=0.1
            elif mutation=='abort': first('connection-aborted')['connection']='other'
            else: first('bytes')['phase']='AwaitPubRec'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                check_pass(events,job)

    def test_consistent_corrupt_wire_still_fails_semantics_not_just_capture_agreement(self):
        for old,new in [('20020100','20020000'),('62020201','62020202'),
                        ('70020201','70020202')]:
            events,job=self.evidence()
            e=next(e for e in events if e['kind'] in ['frame','bytes'] and e.get('connection')=='0-resumed' and e['hex']==old)
            if e['kind']=='frame':
                i=events.index(e);events[i-2]['hex']=new[:2];events[i-1]['hex']=new[2:]
            e['hex']=new
            with self.subTest(packet=new),self.assertRaises(ValueError): check_pass(events,job)

    def test_absent_wire_cannot_pass_even_with_all_old_success_labels(self):
        events,job=self.evidence()
        events=[e for e in events if e['kind'] not in ['bytes','frame','connection','proxy-bytes']]
        events += [dict(kind='bytes',connection='missing',direction=d,hex='50021234') for d in ['tx','rx']]
        with self.assertRaises(ValueError): check_pass(events,job)

    def test_v3_requires_same_connection_repeat_before_release(self):
        events,job=evidence()
        duplicate=next(e for e in events if e['kind']=='bytes' and e.get('connection')=='2-resumed'
                       and e.get('direction')=='tx' and e['hex'].startswith('3c'))
        events.remove(duplicate)
        recs=[i for i,e in enumerate(events) if e['kind']=='frame' and e.get('connection')=='2-resumed' and e['hex']=='50021234']
        self.assertEqual(len(recs),2)
        i=recs[1];del events[i-2:i+1]
        with self.assertRaisesRegex(ValueError,'resumed sender'):check_pass(events,job)

    def test_v3_requires_new_health_payload_to_reuse_completed_incoming_id(self):
        events,job=evidence()
        for event in events:
            if event.get('connection')!='2-resumed':continue
            if event['kind']=='bytes' and event['direction']=='tx' and '6865616c746879' in event['hex']:
                event['hex']=event['hex'].replace('1234','1235')
        with self.assertRaises(ValueError):check_pass(events,job)

    def test_new_broker_publication_cannot_claim_dup1_on_clean_first_send(self):
        events,job=evidence()
        event=next(e for e in events if e['kind']=='frame' and e.get('connection')=='0-initial' and e['hex'].startswith('34'))
        i=events.index(event);event['hex']='3c'+event['hex'][2:];events[i-2]['hex']='3c'
        with self.assertRaisesRegex(ValueError,'DUP=0'):check_pass(events,job)

    def quiet_receiver(self):
        stream,peer=socket.socketpair()
        self.addCleanup(stream.close);self.addCleanup(peer.close)
        stream.settimeout(1)
        client=Client.__new__(Client)
        client.stream,client.trace,client.connection,client.pending=stream,self.trace,'unit',deque()
        client.identity='unit'
        receiver=Receiver('topic',b'original',b'health',self.trace)
        receiver.deliveries=Counter({b'original':1,b'health':1})
        return receiver,client,peer

    def test_partial_frame_cannot_finish_qos2_quiet_observation(self):
        receiver,client,peer=self.quiet_receiver()
        peer.sendall(b'\x34')
        with self.assertRaises(TimeoutError): receiver.collect(client)
        self.assertFalse(self.trace.path.exists() and '"kind": "observation"' in self.trace.path.read_text())

    def test_eof_cannot_finish_qos2_quiet_observation(self):
        receiver,client,peer=self.quiet_receiver()
        peer.close()
        with self.assertRaises(EOFError): receiver.collect(client)
        self.assertFalse(self.trace.path.exists() and '"kind": "observation"' in self.trace.path.read_text())


if __name__ == '__main__':
    unittest.main()
