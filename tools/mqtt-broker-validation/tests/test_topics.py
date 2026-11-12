"""Topic proof rejects forged labels without excluding legal QoS1 retransmission."""
import socket
import tempfile
import unittest
from collections import deque
from pathlib import Path

from reporting import check_pass
from topic_vectors import Receiver, materialize
from topic_fixture import evidence
from wire import Client, Trace, packet, string


class TopicEvidence(unittest.TestCase):
    def test_general_system_and_legal_duplicate_combinations(self):
        for options in [dict(),dict(system=True),dict(first_dup=True),dict(repeat=True),dict(reuse_mid=True),
                        dict(first_dup=True,repeat=True,reuse_mid=True)]:
            with self.subTest(options=options):
                events,job=evidence(**options);check_pass(events,job)

    def test_tags_and_unrelated_bytes_cannot_replace_actual_exchanges(self):
        events,job=evidence()
        events=[e for e in events if e['kind'] in {'topic-vector','assertion'}]
        events += [dict(kind='bytes',direction=d,hex='c000') for d in ['tx','rx']]
        with self.assertRaises(ValueError): check_pass(events,job)

    def test_missing_duplicate_or_mismatched_roles_vectors_and_proof_facts(self):
        for mutation in ['start','version','index','filter','topic','matches','control','root','plan','role','identity','raw','frame',
                         'publisher-ack','receiver-ack','subscription','grant','observation','short-window','wrong-observer',
                         'completion','duplicate-completion','after-completion','partial-rx','abort']:
            events,job=evidence()
            def first(kind,**fields):
                return next(e for e in events if e['kind']==kind and all(e.get(k)==v for k,v in fields.items()))
            if mutation=='start':events.remove(first('topic-vector-start'))
            elif mutation in {'version','index','filter','topic','matches','control','root'}:
                keys=dict(version='scenario_version',control='control_topic')
                key=keys.get(mutation,mutation)
                first('topic-vector-start')[key]=False if key=='matches' else 2 if key=='index' else 'wrong'
            elif mutation=='plan':first('planned-topic-vectors')['vectors'].pop()
            elif mutation=='role':first('topic-vector-roles')['candidate']='other'
            elif mutation=='identity':first('connection')['client_id']='other'
            elif mutation=='raw':first('bytes',direction='rx')['hex']='ff'
            elif mutation=='frame':events.remove(first('frame'))
            elif mutation=='publisher-ack':
                events.remove(first('frame',connection='0-pub',hex='40020017'))
            elif mutation=='receiver-ack':events.remove(first('bytes',direction='tx',connection='0-candidate',hex='40020029'))
            elif mutation=='subscription':first('bytes',connection='0-candidate',direction='tx',hex=packet(0x82,b'\0\x09'+string(first('topic-vector-start')['filter'])+b'\x01').hex())['hex']='8203000901'
            elif mutation=='grant':events.remove(first('frame',connection='0-candidate',hex='9003000901'))
            elif mutation=='observation':events.remove(first('topic-observation'))
            elif mutation=='short-window':first('topic-observation')['actual_seconds']=0.1
            elif mutation=='wrong-observer':first('topic-observation')['connection']='0-pub'
            elif mutation=='completion':events.remove(first('topic-vector'))
            elif mutation=='duplicate-completion':events.append(first('topic-vector'))
            elif mutation=='after-completion':
                i=events.index(first('topic-vector'))+1;events.insert(i,dict(kind='bytes',direction='tx',connection='0-pub',hex='c000'))
            elif mutation=='partial-rx':
                i=events.index(first('topic-vector'));events.insert(i,dict(kind='bytes',direction='rx',connection='0-candidate',hex='32'))
            else:
                i=events.index(first('topic-vector'));events.insert(i,dict(kind='connection-aborted',connection='0-candidate',client_id='0-candidate'))
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):check_pass(events,job)

    def test_raw_consistency_alone_cannot_hide_bad_packet_semantics(self):
        for connection,old,new in [('0-pub','40020017','40020018'),('0-candidate','9003000901','9003000900'),
                                   ('0-positive','20020000','20020100')]:
            events,job=evidence()
            event=next(e for e in events if e['kind']=='frame' and e['connection']==connection and e['hex']==old)
            i=events.index(event);events[i-2]['hex']=new[:2];events[i-1]['hex']=new[2:];event['hex']=new
            with self.subTest(packet=new),self.assertRaises(ValueError):check_pass(events,job)

    def test_negative_probe_receipt_is_rejected_even_with_matching_positive_labels(self):
        events,job=evidence()
        begin=next(i for i,e in enumerate(events) if e['kind']=='topic-vector-start' and e['index']==2)
        i=next(i for i in range(begin,len(events)) if events[i]['kind']=='topic-observation')
        topic=events[begin]['topic'];raw=packet(0x32,string(topic)+b'\0\x2a'+b'topic-2').hex()
        events[i:i]=[dict(kind='bytes',connection='2-candidate',direction='rx',hex=raw),
                     dict(kind='frame',connection='2-candidate',direction='rx',hex=raw),
                     dict(kind='bytes',connection='2-candidate',direction='tx',hex='4002002a')]
        with self.assertRaises(ValueError):check_pass(events,job)

    def test_candidate_control_matches_exact_filter_for_fixed_negative_rows(self):
        # Literal, non-wildcard negative probes use their original exact name;
        # root wildcards require a normal namespace positive control.
        for row,expected in [(('a/B','a/b',False),'validation/id/a/B'),
                             (('+','$validation',False),'validation-id'),
                             (('#','$validation/test',False),'validation/id/probe')]:
            self.assertEqual(materialize(row,'validation/id')[3],expected)


class TopicReceiver(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='mqtt-topic-unit-');self.addCleanup(self.temporary.cleanup)
        self.stream,self.peer=socket.socketpair();self.addCleanup(self.stream.close);self.addCleanup(self.peer.close)
        self.stream.settimeout(1)
        client=Client.__new__(Client);client.stream=self.stream;client.pending=deque()
        client.trace=Trace(Path(self.temporary.name)/'trace.jsonl');client.identity=client.connection='unit'
        self.client=client;self.receiver=Receiver(client)

    def test_partial_frame_and_eof_cannot_pass_quiet_window(self):
        self.peer.sendall(b'\x32')
        with self.assertRaises(TimeoutError):self.receiver.observe()
        self.peer.close()
        with self.assertRaises(EOFError):self.receiver.observe()

    def test_legal_duplicate_ack_and_new_exchange_id_reuse(self):
        def frame(payload,dup=False):return (0x3a if dup else 0x32,string('topic')+b'\0\x29'+payload)
        self.assertTrue(self.receiver.consume(frame(b'old',True),('topic',b'old')))
        self.assertFalse(self.receiver.consume(frame(b'old',True)))
        self.assertTrue(self.receiver.consume(frame(b'new'),('topic',b'new')))
        with self.assertRaises(AssertionError):self.receiver.consume(frame(b'new'))

    def test_unexpected_publication_cannot_pass_quiet_window(self):
        self.peer.sendall(packet(0x32,string('topic')+b'\0\x29'+b'forbidden'))
        with self.assertRaises(AssertionError):self.receiver.observe()
