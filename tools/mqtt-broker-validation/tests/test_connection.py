"""Connection PASS needs the captured exchange, not just success labels."""
import copy
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from common import InputError, ROOT, read_json, decode_json
from connection import ASSERTIONS, PHASES, VERSION, check_evidence, run
from reporting import check_pass
from schema_check import check
from wire import Trace, packet, read_frame, string


class ConnectionProof(unittest.TestCase):
    def evidence(self):
        events = []
        def add(kind, **fields):
            event = dict(kind=kind, schema_version=1, sequence=len(events), seconds=len(events)/100, **fields)
            check('event', event)
            events.append(event)
        def frame(direction, kind, body=b''):
            raw = packet(kind, body)
            if direction == 'tx':
                add('bytes', connection='c', direction=direction, hex=raw.hex())
            else:
                for part in [raw[:1], raw[1:]]:
                    add('bytes', connection='c', direction=direction, hex=part.hex())
                add('frame', connection='c', direction=direction, hex=raw.hex())
        def phase(n):
            add('connect-phase', connection='c', phase=PHASES[n], scenario_version=VERSION)
        add('connection', connection='c', client_id='mv-unit', peer=['127.0.0.1',1883])
        frame('tx',0x10,b'\x00\x04MQTT\x04\x02\x00\x1e'+string('mv-unit'))
        frame('rx',0x20,b'\x00\x00')
        add('assertion',assertion=ASSERTIONS[0],passed=True)
        phase(0)
        frame('tx',0xc0)
        frame('rx',0xd0)
        add('assertion',assertion=ASSERTIONS[1],passed=True)
        phase(1)
        frame('tx',0xe0)
        add('assertion',assertion=ASSERTIONS[2],passed=True)
        phase(2)
        return events

    def test_parent_accepts_complete_split_capture(self):
        events = self.evidence()
        check_evidence(events)
        case = next(c for c in read_json(ROOT/'catalog.json')['cases'] if c.get('entrypoint') == 'connect')
        check_pass(events,dict(case=case,target='tool',resources=None))

    def test_success_labels_cannot_replace_missing_or_wrong_wire_and_phase_proof(self):
        for mutation in ['phase','connection','id','version','raw','frame','connack','pingreq',
                         'pingresp','disconnect','closed','reorder','extra-connection','partial','extra-message']:
            events = self.evidence()
            def first(kind, **fields):
                return next(e for e in events if e['kind']==kind and all(e.get(k)==v for k,v in fields.items()))
            if mutation == 'phase': events.remove(first('connect-phase'))
            elif mutation == 'connection': first('bytes')['connection']='other'
            elif mutation == 'id': first('connection')['client_id']='different'
            elif mutation == 'version': first('connect-phase')['scenario_version']='1.0.0'
            elif mutation == 'raw': first('bytes',direction='rx')['hex']='ff'
            elif mutation == 'frame': events.remove(first('frame'))
            elif mutation in ['connack','pingresp']:
                first('frame',hex='20020000' if mutation=='connack' else 'd000')['hex']='20020100'
            elif mutation in ['pingreq','disconnect']:
                events.remove(first('bytes',direction='tx',hex='c000' if mutation=='pingreq' else 'e000'))
            elif mutation == 'closed': events.remove(first('assertion',assertion=ASSERTIONS[2]))
            elif mutation == 'reorder':
                e=first('bytes',direction='tx',hex='c000');events.remove(e);events.insert(1,e)
            elif mutation == 'extra-connection': events.append(copy.deepcopy(first('connection')))
            elif mutation == 'partial': events.append(dict(kind='bytes',connection='c',direction='rx',hex='d0'))
            else: events.insert(-1,dict(kind='bytes',connection='c',direction='tx',hex='c000'))
            with self.subTest(mutation=mutation), self.assertRaises(InputError):
                check_evidence(events)

    def test_old_label_only_proof_cannot_pass(self):
        case=next(c for c in read_json(ROOT/'catalog.json')['cases'] if c.get('entrypoint')=='connect')
        events=[dict(kind='assertion',assertion=a,passed=True) for a in ASSERTIONS]
        events += [dict(kind='bytes',direction='tx',hex='1000'),dict(kind='bytes',direction='rx',hex='20020000')]
        with self.assertRaises(InputError):
            check_pass(events,dict(case=case,target='tool',resources=None))

    def test_matching_corrupt_raw_and_frame_still_cannot_pass_protocol_proof(self):
        for old,new in [('20020000','20020100'),('20020000','20020005'),('d000','d100'),('d000','d00178')]:
            events=self.evidence()
            frame=next(e for e in events if e['kind']=='frame' and e['hex']==old)
            end=events.index(frame)
            # Keep the raw/frame agreement intact: failure must come from the
            # semantic wire sequence, rather than a capture-digest mismatch.
            events[end-2]['hex']=new[:2]
            events[end-1]['hex']=new[2:]
            frame['hex']=new
            with self.subTest(packet=new), self.assertRaises(InputError):
                check_evidence(events)

    def peer(self, connack, pingresp=None, error=None):
        with tempfile.TemporaryDirectory(prefix='mqtt-connect-check-') as directory, socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(2)
            observed, failures = [], []
            def serve():
                try:
                    stream,_=listener.accept()
                    with stream:
                        stream.settimeout(2)
                        observed.append(read_frame(stream))
                        stream.sendall(connack)
                        if pingresp is not None:
                            observed.append(read_frame(stream))
                            stream.sendall(pingresp)
                            if error is None:
                                observed.append(read_frame(stream))
                except Exception as caught:
                    failures.append(caught)
            worker=threading.Thread(target=serve)
            worker.start()
            trace=Trace(Path(directory)/'trace.jsonl')
            try:
                if error is None:
                    run(listener.getsockname(),trace)
                else:
                    with self.assertRaises(error): run(listener.getsockname(),trace)
            finally:
                worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(failures,failures)
            events=[decode_json(line) for line in trace.path.read_text().splitlines()]
            if error is None:
                self.assertEqual([kind for kind,_ in observed],[0x10,0xc0,0xe0])
                check_evidence(events)
            else:
                self.assertFalse(any(e['kind']=='connect-phase' and e['phase']==PHASES[-1] for e in events))

    def test_real_loopback_valid_exchange_includes_disconnect_and_local_close(self):
        self.peer(packet(0x20,b'\x00\x00'),packet(0xd0))

    def test_real_loopback_rejects_wrong_session_return_code_or_non_connack_first_packet(self):
        for raw in [packet(0x20,b'\x01\x00'),packet(0x20,b'\x00\x05'),packet(0xd0)]:
            with self.subTest(raw=raw.hex()): self.peer(raw,error=AssertionError)

    def test_real_loopback_rejects_wrong_pingresp_or_partial_eof(self):
        for raw,error in [(packet(0xd1),AssertionError),(packet(0xd0,b'x'),AssertionError),(b'\xd0',EOFError)]:
            with self.subTest(raw=raw.hex()): self.peer(packet(0x20,b'\x00\x00'),raw,error=error)


if __name__ == '__main__':
    unittest.main()
