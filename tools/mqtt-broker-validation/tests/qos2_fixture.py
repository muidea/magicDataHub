"""Synthetic, reusable wire facts; never a stored Broker acceptance report."""
from calibration_model import RECOVERY_BRANCHES
from common import ROOT, read_json
from qos2 import VERSION, mid_bytes
from schema_check import check
from wire import packet, string


def evidence(*, early_delivery=False, reuse_mid=False, repeated_pubrel=False):
    events = []
    case = next(c for c in read_json(ROOT/'catalog.json')['cases'] if c.get('entrypoint') == 'qos2_recovery')
    for ordinal, (direction, phase) in enumerate(sorted(RECOVERY_BRANCHES)):
        context = dict(direction=direction, phase=phase)
        identity, topic = 'persistent-' + str(ordinal), 'topic-' + str(ordinal)
        original, health = b'original', b'healthy-after-recovery'
        initial, resumed, reset, helper = [str(ordinal) + suffix for suffix in ['-initial','-resumed','-reset','-helper']]
        def add(kind, **fields):
            event = dict(kind=kind, schema_version=1, sequence=len(events), seconds=float(len(events)), **(context | fields))
            check('event', event)
            events.append(event)
        def frame(connection, direction, kind, body=b''):
            raw = packet(kind, body)
            if direction == 'tx':
                add('bytes', connection=connection, direction=direction, hex=raw.hex())
            else:
                for part in [raw[:1], raw[1:]]:
                    add('bytes', connection=connection, direction=direction, hex=part.hex())
                add('frame', connection=connection, direction=direction, hex=raw.hex())
        def connect(connection, clean=True, present=False):
            client_id = identity if connection != helper else 'helper-' + str(ordinal)
            add('connection', connection=connection, client_id=client_id, peer=['127.0.0.1',1883])
            frame(connection,'tx',0x10,b'\x00\x04MQTT\x04'+bytes([2 if clean else 0])+b'\x00\x1e'+string(client_id))
            frame(connection,'rx',0x20,bytes([int(present),0]))
        def subscribe(connection):
            frame(connection,'tx',0x82,b'\0\x09'+string(topic)+b'\x02')
            frame(connection,'rx',0x90,b'\0\x09\x02')
        def publish(connection, payload, mid, dup=False):
            frame(connection,'tx',0x3c if dup else 0x34,string(topic)+mid_bytes(mid)+payload)
        def send(connection,payload,mid):
            publish(connection,payload,mid)
            frame(connection,'rx',0x50,mid_bytes(mid))
            frame(connection,'tx',0x62,mid_bytes(mid))
            frame(connection,'rx',0x70,mid_bytes(mid))
        def receive(connection,payload,mid):
            frame(connection,'rx',0x34,string(topic)+mid_bytes(mid)+payload)
            frame(connection,'tx',0x50,mid_bytes(mid))
            release(connection,payload,mid)
        def release(connection,payload,mid,deliver=True,complete=True):
            frame(connection,'rx',0x62,mid_bytes(mid))
            if deliver:
                add('application-delivery',connection=connection,packet_id=mid,topic=topic,payload_hex=payload.hex(),count=1)
            if complete: frame(connection,'tx',0x70,mid_bytes(mid))
        add('qos2-branch-start',scenario_version=VERSION,client_id=identity,topic=topic,original_hex=original.hex(),health_hex=health.hex())
        if direction == 'client-to-broker':
            connect(helper);subscribe(helper);connect(initial,clean=False)
            publish(initial,original,0x1234)
            if phase == 'AwaitPubComp':
                frame(initial,'rx',0x50,mid_bytes(0x1234));frame(initial,'tx',0x62,mid_bytes(0x1234))
            if early_delivery: receive(helper,original,513)
            tx=b''.join(bytes.fromhex(e['hex']) for e in events if e['kind']=='bytes' and e.get('connection')==initial and e['direction']=='tx')
            rx=b''.join(bytes.fromhex(e['hex']) for e in events if e['kind']=='frame' and e.get('connection')==initial)
            ack=0x50 if phase=='AwaitPubRec' else 0x70
            raw=packet(ack,mid_bytes(0x1234))
            add('proxy-target',peer=['127.0.0.1',1883],listener=['127.0.0.1',1884])
            for d,op,body in [('client-to-broker','receive',tx),('client-to-broker','forward',tx),
                              ('broker-to-client','receive',rx+raw),('broker-to-client','forward',rx)]:
                add('proxy-bytes',direction=d,operation=op,hex=body.hex())
            add('fault-cut',ack_kind=ack,ack_body_hex=mid_bytes(0x1234).hex(),hex=raw.hex(),forwarded=False,boundary='received not forwarded')
            add('qos2-disconnect',sender_state=phase,packet_id=0x1234,ack_received_by_client=False)
            add('connection-aborted',connection=initial,client_id=identity)
            add('proxy-cleanup',complete=True,errors=[])
            connect(resumed,clean=False,present=True)
            if phase=='AwaitPubRec':
                publish(resumed,original,0x1234,True);frame(resumed,'rx',0x50,mid_bytes(0x1234))
            frame(resumed,'tx',0x62,mid_bytes(0x1234));frame(resumed,'rx',0x70,mid_bytes(0x1234))
            publish(resumed,health,0x1234);publish(resumed,health,0x1234,True)
            frame(resumed,'rx',0x50,mid_bytes(0x1234));frame(resumed,'rx',0x50,mid_bytes(0x1234))
            frame(resumed,'tx',0x62,mid_bytes(0x1234));frame(resumed,'rx',0x70,mid_bytes(0x1234))
            if not early_delivery: receive(helper,original,513)
            receive(helper,health,513 if reuse_mid else 514)
            if repeated_pubrel:
                release(helper,health,513 if reuse_mid else 514,deliver=False)
        else:
            connect(initial,clean=False);subscribe(initial);connect(helper)
            send(helper,original,0x4321)
            frame(initial,'rx',0x34,string(topic)+mid_bytes(513)+original)
            if phase=='AwaitPubComp':
                frame(initial,'tx',0x50,mid_bytes(513));release(initial,original,513,complete=False)
            add('qos2-disconnect',sender_state=phase,packet_id=513,withheld='PUBREC' if phase=='AwaitPubRec' else 'PUBCOMP')
            add('connection-aborted',connection=initial,client_id=identity)
            connect(resumed,clean=False,present=True)
            if phase=='AwaitPubRec':
                frame(resumed,'rx',0x3c,string(topic)+mid_bytes(513)+original);frame(resumed,'tx',0x50,mid_bytes(513))
            release(resumed,original,513,deliver=phase=='AwaitPubRec')
            send(helper,health,0x4322)
            receive(resumed,health,513 if reuse_mid else 514)
            if repeated_pubrel: release(resumed,health,513 if reuse_mid else 514,deliver=False)
        add('observation',completed=True,seconds_required=0.4,actual_seconds=0.4)
        frame(resumed,'tx',0xe0);frame(helper,'tx',0xe0)
        connect(reset,clean=True);frame(reset,'tx',0xe0)
        add('qos2-clean-session-reset',client_id=identity)
        for assertion in case['required_assertions']:
            if assertion!='qos2-session-present': add('assertion',assertion=assertion,passed=True)
        for present in [False,True]: add('assertion',assertion='qos2-session-present',passed=True,expected=present)
        add('qos2-branch-completed',original_deliveries=1,health_deliveries=1)
    return events,dict(case=case,target='tool',resources=None)
