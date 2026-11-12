"""Synthetic wire model for the bounded concurrent QoS2 probe."""
from common import ROOT, read_json
from qos2 import mid_bytes
from qos2_flow import MIDS, PAYLOADS, PHASES, VERSION
from schema_check import check
from wire import packet, string


def evidence(*, reverse=False, reuse_mid=False, extra_release=False, retransmit=False):
    events = []
    topic = 'validation/' + 'a' * 32 + '/qos2-flow'
    case = next(c for c in read_json(ROOT/'catalog.json')['cases'] if c.get('entrypoint') == 'qos2_flow')
    def add(kind, **fields):
        event = dict(kind=kind, schema_version=1, sequence=len(events), seconds=float(len(events)), **fields)
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
    def control(connection, direction, kind, mid):
        frame(connection, direction, kind, mid_bytes(mid))
    def publish(connection, direction, payload, mid, dup=False):
        frame(connection, direction, 0x3c if dup else 0x34, string(topic)+mid_bytes(mid)+payload)
    def release(payload, mid, deliver=True):
        control('subscriber', 'rx', 0x62, mid)
        if deliver:
            add('application-delivery', connection='subscriber', packet_id=mid, topic=topic,
                payload_hex=payload.hex(), count=1)
        control('subscriber', 'tx', 0x70, mid)
    add('qos2-flow-start', scenario_version=VERSION, topic=topic)
    for c in ['publisher', 'subscriber']:
        add('connection', connection=c, client_id=c, peer=['127.0.0.1',1883])
        frame(c,'tx',0x10,b'\x00\x04MQTT\x04\x02\x00\x1e'+string(c))
        frame(c,'rx',0x20,b'\0\0')
    add('qos2-flow-roles', publisher='publisher', subscriber='subscriber')
    frame('subscriber','tx',0x82,b'\0\x09'+string(topic)+b'\x02')
    frame('subscriber','rx',0x90,b'\0\x09\x02')
    for payload, mid in zip(PAYLOADS, MIDS): publish('publisher','tx',payload,mid)
    order = MIDS[::-1] if reverse else MIDS
    for mid in order: control('publisher','rx',0x50,mid)
    for mid in order * 2: control('publisher','tx',0x62,mid)
    for mid in order * 2: control('publisher','rx',0x70,mid)
    add('qos2-flow-phase',phase=PHASES[0])
    broker_ids = (0x4001, 0x4002)
    pair = list(zip(PAYLOADS[:2], broker_ids))
    if reverse: pair.reverse()
    for payload, mid in pair:
        publish('subscriber','rx',payload,mid)
        if retransmit: publish('subscriber','rx',payload,mid,True)
    for _, mid in pair:
        for _ in range(3 if retransmit else 2): control('subscriber','tx',0x50,mid)
    for payload, mid in pair: release(payload,mid)
    for _ in range(2 if retransmit else 1):
        for payload, mid in reversed(pair): release(payload,mid,False)
    add('qos2-flow-phase',phase=PHASES[1])
    publish('publisher','tx',PAYLOADS[2],MIDS[0])
    control('publisher','rx',0x50,MIDS[0]); control('publisher','tx',0x62,MIDS[0]); control('publisher','rx',0x70,MIDS[0])
    health_mid = broker_ids[0] if reuse_mid else 0x4003
    publish('subscriber','rx',PAYLOADS[2],health_mid)
    control('subscriber','tx',0x50,health_mid); release(PAYLOADS[2],health_mid)
    add('qos2-flow-phase',phase=PHASES[2])
    if extra_release: release(PAYLOADS[2],health_mid,False)
    add('observation',completed=True,seconds_required=0.4,actual_seconds=0.4)
    add('qos2-flow-phase',phase=PHASES[3])
    for c in ['publisher','subscriber']: frame(c,'tx',0xe0)
    for assertion in case['required_assertions']: add('assertion',assertion=assertion,passed=True)
    return events, dict(case=case,target='tool',resources=None)
