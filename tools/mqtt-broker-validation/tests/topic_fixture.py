"""Synthetic clean-session QoS1 wire exchanges, independent of archived runs."""
from common import ROOT, read_json
from schema_check import check
from topic_vectors import GENERAL, SYSTEM, VERSION, materialize
from wire import packet, string


def evidence(*, system=False, first_dup=False, repeat=False, reuse_mid=False):
    events, clock = [], 0
    def add(kind, **fields):
        nonlocal clock
        clock += 0.01
        e=dict(kind=kind,schema_version=1,sequence=len(events),seconds=clock,**fields)
        check('event',e); events.append(e)
    def frame(c,d,k,b=b''):
        raw=packet(k,b).hex()
        if d=='rx':
            add('bytes',connection=c,direction=d,hex=raw[:2])
            add('bytes',connection=c,direction=d,hex=raw[2:])
            add('frame',connection=c,direction=d,hex=raw)
        else: add('bytes',connection=c,direction=d,hex=raw)
    def publication(c,topic,payload,mid=41,dup=False):
        frame(c,'rx',0x3a if dup else 0x32,string(topic)+mid.to_bytes(2,'big')+payload)
        frame(c,'tx',0x40,mid.to_bytes(2,'big'))
    rows=SYSTEM if system else GENERAL
    add('planned-topic-vectors',vectors=[list(row) for row in rows])
    for index,row in enumerate(rows):
        root='validation/'+format(index,'032x')
        pattern,topic,matches,control=materialize(row,root)
        add('topic-vector-start',scenario_version=VERSION,index=index,root=root,filter=pattern,topic=topic,matches=matches,control_topic=control)
        pub,candidate,positive=[f'{index}-{role}' for role in ['pub','candidate','positive']]
        for c in [pub,candidate,positive]:
            add('connection',connection=c,client_id=c,peer=['127.0.0.1',1883])
            frame(c,'tx',0x10,b'\0\x04MQTT\x04\x02\0\x1e'+string(c))
            frame(c,'rx',0x20,b'\0\0')
        add('topic-vector-roles',publisher=pub,candidate=candidate,positive=positive)
        frame(candidate,'tx',0x82,b'\0\x09'+string(pattern)+b'\x01')
        frame(candidate,'rx',0x90,b'\0\x09\x01')
        control_payload=('control-'+str(index)).encode();target_payload=('topic-'+str(index)).encode()
        frame(pub,'tx',0x32,string(control)+b'\0\x17'+control_payload)
        frame(pub,'rx',0x40,b'\0\x17')
        publication(candidate,control,control_payload,dup=first_dup)
        frame(positive,'tx',0x82,b'\0\x09'+string(topic)+b'\x01')
        frame(positive,'rx',0x90,b'\0\x09\x01')
        frame(pub,'tx',0x32,string(topic)+b'\0\x17'+target_payload)
        frame(pub,'rx',0x40,b'\0\x17')
        publication(positive,topic,target_payload,dup=first_dup)
        if matches: publication(candidate,topic,target_payload,mid=41 if reuse_mid else 42,dup=first_dup)
        if repeat:
            publication(candidate,topic if matches else control,target_payload if matches else control_payload,
                        mid=(41 if reuse_mid else 42) if matches else 41,dup=True)
            publication(positive,topic,target_payload,dup=True)
        for c in [candidate,positive]:
            clock+=0.4
            add('topic-observation',connection=c,seconds_required=0.4,actual_seconds=0.4,completed=True)
        for c in [positive,candidate,pub]: frame(c,'tx',0xe0)
        add('topic-vector',index=index,filter=pattern,topic=topic,matches=matches)
    case=next(c for c in read_json(ROOT/'catalog.json')['cases'] if c.get('entrypoint')==('system_topics' if system else 'topics'))
    for a in case['required_assertions']: add('assertion',assertion=a,passed=True)
    return events,dict(case=case,resources=None,target='tool')
