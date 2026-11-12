"""Synthetic bidirectional wire exchanges with independently chosen Broker IDs."""
from common import ROOT, read_json
from qos2 import mid_bytes
from qos2_duplex import BRANCHES, INPUTS, OUTPUTS, PHASES, SOURCE_IDS, VERSION
from schema_check import check
from wire import packet, string


def evidence(*, broker_ids=(0x1234, 0x3456), reuse_mid=False, retransmit=False, extra_release=False,
             early=False, early_source_ack=False, same_id=True, wrong_order=False):
    events=[]
    case=next(c for c in read_json(ROOT/'catalog.json')['cases'] if c.get('entrypoint')=='qos2_duplex')
    def add(kind,**fields):
        event=dict(kind=kind,schema_version=1,sequence=len(events),seconds=float(len(events)),**fields)
        check('event',event);events.append(event)
    def frame(c,d,kind,body=b''):
        raw=packet(kind,body)
        if d=='tx':add('bytes',connection=c,direction=d,hex=raw.hex())
        else:
            for part in [raw[:1],raw[1:]]:add('bytes',connection=c,direction=d,hex=part.hex())
            add('frame',connection=c,direction=d,hex=raw.hex())
    def control(c,d,kind,mid):frame(c,d,kind,mid_bytes(mid))
    def publish(c,d,topic,payload,mid,dup=False):frame(c,d,0x3c if dup else 0x34,string(topic)+mid_bytes(mid)+payload)
    def release(c,topic,payload,mid,deliver=True):
        control(c,'rx',0x62,mid)
        if deliver:add('application-delivery',connection=c,packet_id=mid,topic=topic,payload_hex=payload.hex(),count=1)
        control(c,'tx',0x70,mid)
    def received(c,topic,payload,mid):
        publish(c,'rx',topic,payload,mid)
        if retransmit:publish(c,'rx',topic,payload,mid,True)
        for _ in range(2 if retransmit else 1):control(c,'tx',0x50,mid)
        release(c,topic,payload,mid)
        if retransmit:release(c,topic,payload,mid,False)
    for ordinal,branch in enumerate(BRANCHES):
        owner,helper=str(ordinal)+'-owner',str(ordinal)+'-helper'
        topic='validation/'+str(ordinal)*32+'/qos2-duplex/'+branch
        inside,outside=topic+'/in',topic+'/out'
        mid=broker_ids[ordinal];input_mid=mid if same_id else mid+1
        helper_mid=0x2345
        add('qos2-duplex-branch-start',branch=branch,topic=topic,scenario_version=VERSION)
        for c in [owner,helper]:
            add('connection',connection=c,client_id=c,peer=['127.0.0.1',1883])
            frame(c,'tx',0x10,b'\x00\x04MQTT\x04\x02\x00\x1e'+string(c));frame(c,'rx',0x20,b'\0\0')
        add('qos2-duplex-roles',branch=branch,owner=owner,helper=helper)
        for c,t in [(owner,outside),(helper,inside)]:
            frame(c,'tx',0x82,b'\0\x09'+string(t)+b'\x02');frame(c,'rx',0x90,b'\0\x09\x02')
        publish(helper,'tx',outside,OUTPUTS[0],SOURCE_IDS[0]);control(helper,'rx',0x50,SOURCE_IDS[0]);control(helper,'tx',0x62,SOURCE_IDS[0])
        if early_source_ack:control(helper,'rx',0x70,SOURCE_IDS[0])
        publish(owner,'rx',outside,OUTPUTS[0],mid)
        if retransmit:publish(owner,'rx',outside,OUTPUTS[0],mid,True)
        publish(owner,'tx',inside,INPUTS[0],input_mid);control(owner,'rx',0x50,input_mid)
        if early:received(helper,inside,INPUTS[0],helper_mid)
        add('qos2-duplex-phase',branch=branch,phase=PHASES[0])
        def incoming():control(owner,'tx',0x62,input_mid);control(owner,'rx',0x70,input_mid)
        def outgoing():
            for _ in range(2 if retransmit else 1):control(owner,'tx',0x50,mid)
            release(owner,outside,OUTPUTS[0],mid)
            if retransmit:release(owner,outside,OUTPUTS[0],mid,False)
        order=[incoming,outgoing] if branch=='incoming-first' else [outgoing,incoming]
        if wrong_order:order.reverse()
        order[0]();add('qos2-duplex-phase',branch=branch,phase=PHASES[1])
        order[1]();add('qos2-duplex-phase',branch=branch,phase=PHASES[2])
        if not early_source_ack:control(helper,'rx',0x70,SOURCE_IDS[0])
        if not early:received(helper,inside,INPUTS[0],helper_mid)
        publish(owner,'tx',inside,INPUTS[1],input_mid);control(owner,'rx',0x50,input_mid)
        control(owner,'tx',0x62,input_mid);control(owner,'rx',0x70,input_mid)
        healthy_helper_mid=helper_mid if reuse_mid else helper_mid+1
        received(helper,inside,INPUTS[1],healthy_helper_mid)
        publish(helper,'tx',outside,OUTPUTS[1],SOURCE_IDS[1]);control(helper,'rx',0x50,SOURCE_IDS[1]);control(helper,'tx',0x62,SOURCE_IDS[1])
        healthy_owner_mid=mid if reuse_mid else mid+1
        received(owner,outside,OUTPUTS[1],healthy_owner_mid);control(helper,'rx',0x70,SOURCE_IDS[1])
        add('qos2-duplex-phase',branch=branch,phase=PHASES[3])
        for c,t,p,m in [(owner,outside,OUTPUTS[1],healthy_owner_mid),(helper,inside,INPUTS[1],healthy_helper_mid)]:
            add('qos2-duplex-observation-start',branch=branch,connection=c,seconds_required=0.4)
            if extra_release:release(c,t,p,m,False)
            add('qos2-duplex-observation',branch=branch,connection=c,seconds_required=0.4,actual_seconds=0.4,completed=True)
        add('qos2-duplex-phase',branch=branch,phase=PHASES[4])
        for c in [owner,helper]:frame(c,'tx',0xe0)
        add('qos2-duplex-branch-completed',branch=branch)
    for assertion in case['required_assertions']:add('assertion',assertion=assertion,passed=True)
    return events,dict(case=case,target='tool',resources=None)
