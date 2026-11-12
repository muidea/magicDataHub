"""Synthetic fanout wire facts, never a retained Broker acceptance report."""
from common import ROOT, read_json
from qos2 import mid_bytes
from qos2_fanout import MID, PAYLOADS, PHASES, VERSION
from schema_check import check
from wire import packet, string


def evidence(*, shared_ids=True, reuse_mid=False, extra_release=False, retransmit=False, early=False):
    events = []
    topic = 'validation/' + 'b' * 32 + '/qos2-fanout'
    case = next(c for c in read_json(ROOT/'catalog.json')['cases'] if c.get('entrypoint')=='qos2_fanout')
    def add(kind, **fields):
        event = dict(kind=kind, schema_version=1, sequence=len(events), seconds=float(len(events)), **fields)
        check('event',event);events.append(event)
    def frame(c,d,kind,body=b''):
        raw=packet(kind,body)
        if d=='tx':add('bytes',connection=c,direction=d,hex=raw.hex())
        else:
            for part in [raw[:1],raw[1:]]:add('bytes',connection=c,direction=d,hex=part.hex())
            add('frame',connection=c,direction=d,hex=raw.hex())
    def control(c,d,kind,mid):frame(c,d,kind,mid_bytes(mid))
    def publish(c,d,payload,mid,dup=False):frame(c,d,0x3c if dup else 0x34,string(topic)+mid_bytes(mid)+payload)
    def release(c,payload,mid,deliver=True):
        control(c,'rx',0x62,mid)
        if deliver:add('application-delivery',connection=c,packet_id=mid,topic=topic,payload_hex=payload.hex(),count=1)
        control(c,'tx',0x70,mid)
    def receive(c,payload,health=False):
        mid=(0x4101 if shared_ids or c=='a' else 0x4201)+(int(health and not reuse_mid))
        publish(c,'rx',payload,mid)
        control(c,'tx',0x50,mid)
        if retransmit:
            publish(c,'rx',payload,mid,True);control(c,'tx',0x50,mid)
        release(c,payload,mid)
        if retransmit:release(c,payload,mid,False)
        return mid
    add('qos2-fanout-start',scenario_version=VERSION,topic=topic)
    for c in ['publisher','a','b']:
        add('connection',connection=c,client_id=c,peer=['127.0.0.1',1883])
        frame(c,'tx',0x10,b'\x00\x04MQTT\x04\x02\x00\x1e'+string(c));frame(c,'rx',0x20,b'\0\0')
    add('qos2-fanout-roles',publisher='publisher',subscribers=['a','b'])
    for c in ['a','b']:
        frame(c,'tx',0x82,b'\0\x09'+string(topic)+b'\x02');frame(c,'rx',0x90,b'\0\x09\x02')
    publish('publisher','tx',PAYLOADS[0],MID);publish('publisher','tx',PAYLOADS[0],MID,True)
    if early:
        for c in ['a','b']:receive(c,PAYLOADS[0])
    for _ in range(2):control('publisher','rx',0x50,MID)
    control('publisher','tx',0x62,MID);control('publisher','rx',0x70,MID)
    add('qos2-fanout-phase',phase=PHASES[0])
    if not early:
        for c in ['a','b']:receive(c,PAYLOADS[0])
    add('qos2-fanout-phase',phase=PHASES[1])
    publish('publisher','tx',PAYLOADS[1],MID);control('publisher','rx',0x50,MID)
    control('publisher','tx',0x62,MID);control('publisher','rx',0x70,MID)
    add('qos2-fanout-phase',phase=PHASES[2])
    mids={c:receive(c,PAYLOADS[1],True) for c in ['b','a']}
    add('qos2-fanout-phase',phase=PHASES[3])
    for c in ['a','b']:
        add('qos2-fanout-observation-start',connection=c,seconds_required=0.4)
        if extra_release:release(c,PAYLOADS[1],mids[c],False)
        add('qos2-fanout-observation',connection=c,seconds_required=0.4,actual_seconds=0.4,completed=True)
    add('qos2-fanout-phase',phase=PHASES[4])
    for c in ['a','b','publisher']:frame(c,'tx',0xe0)
    for assertion in case['required_assertions']:add('assertion',assertion=assertion,passed=True)
    return events,dict(case=case,target='tool',resources=None)
