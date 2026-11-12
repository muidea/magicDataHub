"""Synthetic negotiated QoS wire facts; not an acceptance artifact."""
from common import ROOT, read_json
from mixed_qos import PAYLOAD, VERSION, VECTORS, filters
from qos2 import mid_bytes
from schema_check import check
from wire import packet, string


def evidence(*, lower=False, overlap_copies=False, retry=False, extra_release=False, early=False, excess=False,
             best_effort_duplicates=False):
    events=[]
    case=next(c for c in read_json(ROOT/'catalog.json')['cases'] if c.get('entrypoint')=='mixed_qos')
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
    for index,(p,requests) in enumerate(VECTORS):
        topic='validation/'+f'{index:032x}'+'/mixed-qos/item'
        pub,sub=f'{index}-publisher',f'{index}-subscriber'
        add('mixed-vector-start',index=index,scenario_version=VERSION,topic=topic)
        for c in [pub,sub]:
            add('connection',connection=c,client_id=c,peer=['127.0.0.1',1883])
            frame(c,'tx',0x10,b'\0\x04MQTT\x04\x02\0\x1e'+string(c));frame(c,'rx',0x20,b'\0\0')
        add('mixed-roles',index=index,publisher=pub,subscriber=sub)
        grants=[min(s,1) if lower else s for s in requests]
        for mid,(f,s,g) in enumerate(zip(filters(topic,len(requests)),requests,grants),9):
            frame(sub,'tx',0x82,mid_bytes(mid)+string(f)+bytes([s]));frame(sub,'rx',0x90,mid_bytes(mid)+bytes([g]))
        frame(pub,'tx',0x30|p<<1,string(topic)+(b'\0\x17' if p else b'')+PAYLOAD)
        def outputs():
            qs=[min(p,max(grants))]
            for offset,qos in enumerate(qs):
                mid=101+offset if qos else None
                body=string(topic)+(mid_bytes(mid) if qos else b'')+PAYLOAD
                frame(sub,'rx',0x30|qos<<1,body)
                if qos==2:
                    control(sub,'tx',0x50,mid)
                    if retry:frame(sub,'rx',0x3c,body);control(sub,'tx',0x50,mid)
                    control(sub,'rx',0x62,mid)
                add('mixed-delivery',connection=sub,qos=qos,packet_id=mid,topic=topic,payload_hex=PAYLOAD.hex())
                if qos:control(sub,'tx',0x70 if qos==2 else 0x40,mid)
                if retry and qos==1:
                    frame(sub,'rx',0x3a,body)
                    add('mixed-delivery',connection=sub,qos=qos,packet_id=mid,topic=topic,payload_hex=PAYLOAD.hex())
                    control(sub,'tx',0x40,mid)
                if extra_release and qos==2:control(sub,'rx',0x62,mid);control(sub,'tx',0x70,mid)
                if best_effort_duplicates and p==1 and qos==0:
                    frame(sub,'rx',0x30,body)
                    add('mixed-delivery',connection=sub,qos=0,packet_id=None,topic=topic,payload_hex=PAYLOAD.hex())
        if early:outputs()
        if p==1:control(pub,'rx',0x40,23)
        elif p==2:
            control(pub,'rx',0x50,23);control(pub,'tx',0x62,23);control(pub,'rx',0x70,23)
        add('mixed-input-completed',index=index)
        if not early:outputs()
        add('mixed-observation-start',connection=sub,seconds_required=0.4)
        add('mixed-observation',connection=sub,seconds_required=0.4,actual_seconds=0.4,completed=True)
        for c in [sub,pub]:frame(c,'tx',0xe0)
        add('mixed-vector-completed',index=index)
    for assertion in case['required_assertions']:add('assertion',assertion=assertion,passed=True)
    return events,dict(case=case,target='tool',resources=None)
