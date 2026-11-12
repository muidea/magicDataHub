"""Exact expected assertion rejected by each controlled single-fault peer."""
MUTATIONS = {
    'wrong-CONNACK': 'valid-success-CONNACK',
    'wrong-PINGRESP': 'expected-control-packet',
    'wrong-PUBREC-id': 'expected-control-packet',
    'wrong-topic': 'delivery-topic-and-payload',
    'stale-retained': 'forbidden-packet-in-observation-window',
    'recovery-session-lost': 'qos2-session-present',
    'recovery-wrong-mid': 'qos2-retransmit-mid',
    'recovery-missing-dup': 'qos2-retransmit-dup',
    'recovery-wrong-payload': 'qos2-retransmit-content',
    'recovery-wrong-pubrel-mid': 'qos2-PUBREL',
    'recovery-duplicate-delivery': 'qos2-single-application-delivery',
}
RECOVERY_BRANCHES = {(direction, phase) for direction in ('client-to-broker', 'broker-to-client')
                     for phase in ('AwaitPubRec', 'AwaitPubComp')}
