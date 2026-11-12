"""Import two complete QoS2 lists without treating their markers as obligations.

This is a selected source inventory and evidence plan, never a coverage verdict.
"""
import hashlib
from html.parser import HTMLParser
import json
import re

from common import InputError
from qos2 import BRANCH_NAMES, VERSION
from qos2_flow import CASE_ID as FLOW_ID, PHASES as FLOW_PHASES, VERSION as FLOW_VERSION

from qos2_fanout import CASE_ID as FANOUT_ID, PHASES as FANOUT_PHASES, VERSION as FANOUT_VERSION

from qos2_duplex import CASE_ID as DUPLEX_ID, PHASES as DUPLEX_PHASES, VERSION as DUPLEX_VERSION, BRANCHES as DUPLEX_BRANCHES

DUPLEX_KEYS = {'pubrel-original-id', 'pubcomp-on-pubrel', 'id-reuse-is-new'}

COMPOUND_IDS = ('MQTT-4.3.3-1', 'MQTT-4.3.3-2')
ATOM_KEYS = {
    'sender': ('unused-id', 'new-publish-qos2-dup0', 'publish-unacknowledged',
               'pubrel-on-pubrec', 'pubrel-original-id', 'pubrel-unacknowledged', 'no-publish-after-pubrel'),
    'receiver': ('pubrec-on-publish', 'repeat-pubrec', 'no-duplicate-onward', 'pubcomp-on-pubrel', 'id-reuse-is-new'),
}


def block_digest(blocks):
    return hashlib.sha256(json.dumps(blocks, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Blocks(HTMLParser):
    """Nonempty p/li/headings plus explicit list boundaries before the appendix.

    Lists are not flattened into p text. Inner formatting is retained as text;
    nested p within a li contributes to that item, rather than becoming an item.
    """
    def __init__(self):
        super().__init__()
        self.blocks, self.active, self.finished = [], None, False

    def handle_starttag(self, tag, attrs):
        if 'AppendixHeading1' in dict(attrs).get('class', '').split():
            self.finished, self.active = True, None
        if self.finished:
            return
        if tag in {'ul', 'ol'}:
            self.blocks.append(dict(kind=tag + '-start', text=''))
        if tag in {'p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'} and self.active is None:
            self.active = (tag, [])
        elif tag == 'br' and self.active is not None:
            self.active[1].append(' ')

    def handle_endtag(self, tag):
        if self.finished:
            return
        if self.active is not None and self.active[0] == tag:
            text = re.sub(r'\s+', ' ', ''.join(self.active[1])).strip()
            if text:
                self.blocks.append(dict(kind=tag, text=text))
            self.active = None
        if tag in {'ul', 'ol'}:
            self.blocks.append(dict(kind=tag + '-end', text=''))

    def handle_data(self, text):
        if self.active is not None:
            self.active[1].append(text)


def require(value, reason):
    if not value:
        raise InputError('compound QoS2 source: ' + reason)


def validate_compounds(mapping, catalog):
    groups = mapping['compound_requirements']
    require([g['official_id'] for g in groups] == list(COMPOUND_IDS), 'missing, duplicate or reordered parent list')
    case = next(c for c in catalog['cases'] if c.get('entrypoint') == 'qos2_recovery')
    for group, role, count in zip(groups, ['sender', 'receiver'], [6, 4]):
        blocks = group['source_blocks']
        require(group['protocol_role'] == role and group['source_sha256'] == block_digest(blocks), 'role or block digest mismatch')
        require([b['kind'] for b in blocks] == ['p', 'ul-start'] + ['li'] * count + ['ul-end', 'p']
                and blocks[0]['text'] == 'In the QoS 2 delivery protocol, the ' + role.title()
                and blocks[1]['text'] == blocks[-2]['text'] == ''
                and blocks[-1]['text'] == '[' + group['official_id'] + '].', 'complete intro/list/marker structure required')
        contexts = group['context_paragraphs']
        require(len(contexts) == 2 and len({p['source_text'] for p in contexts}) == 2, 'reuse/parallelism or onward-delivery context missing')
        prefixes = (('The Packet Identifier becomes available for reuse once the Sender has received the PUBCOMP Packet.',
                     'Note that a Sender is permitted to send further PUBLISH Packets with different Packet Identifiers')
                    if role == 'sender' else
                    ('1 The receiver is not required to complete delivery of the Application Message before sending the PUBREC or PUBCOMP.',
                     'Figure 4.3 shows that there are two methods by which QoS 2 can be handled by the receiver.'))
        require(all(p['source_text'].startswith(prefix) for p, prefix in zip(contexts, prefixes)),
                'context belongs to a different flow or omits the implementation choice')
        for p in contexts:
            require(hashlib.sha256(p['source_text'].encode()).hexdigest() == p['sha256'], 'context digest mismatch')
        atoms = group['atoms']
        require([a['key'] for a in atoms] == list(ATOM_KEYS[role]), 'atomic inventory missing or reordered')
        inventory = []
        for item_index, block in enumerate(blocks[2:-2]):
            starts = [m.start() for m in re.finditer(r'\bMUST\b', block['text'])]
            for ordinal, offset in enumerate(starts):
                inventory.append((item_index, ordinal, offset, block['text']))
        require(len(inventory) == len(atoms), 'every MUST/MUST NOT needs exactly one source span')
        for atom, (item, ordinal, offset, text) in zip(atoms, inventory):
            expected_branches = [b for b in BRANCH_NAMES if b.startswith('broker-to-client/' if role == 'sender' else 'client-to-broker/')]
            require((atom['item_index'], atom['must_ordinal'], atom['must_offset'], atom['source_text']) == (item, ordinal, offset, text), 'atomic source span changed')
            require(atom['normative_level'] == ('MUST NOT' if text[offset:].startswith('MUST NOT') else 'MUST'), 'atomic normative level mismatch')
            require(atom['case_id'] == case['id'] and atom['scenario_version'] == case['scenario_version'] == VERSION
                    and atom['required_branches'] == expected_branches, 'atomic case/version/role/branch mismatch')
            require(atom['oracle'] == ('qos2_evidence.receiver_proof' if atom['key'] == 'no-duplicate-onward' else 'qos2_evidence.branch_proof'), 'atomic oracle mismatch')
            supplement = [] if atom['key'] == 'repeat-pubrec' else [dict(
                case_id=FLOW_ID, scenario_version=FLOW_VERSION, server_role=role, required_phases=list(FLOW_PHASES),
                oracle='qos2_flow_evidence.check_evidence', evidence_status='partial-selected-evidence', review_status='pending')]
            if role == 'receiver':
                supplement.append(dict(case_id=FANOUT_ID, scenario_version=FANOUT_VERSION, server_role=role,
                                       required_phases=list(FANOUT_PHASES), oracle='qos2_fanout_evidence.check_evidence',
                                       evidence_status='partial-selected-evidence', review_status='pending'))
            if atom['key'] in DUPLEX_KEYS:
                supplement.append(dict(case_id=DUPLEX_ID, scenario_version=DUPLEX_VERSION, server_role=role,
                                       required_phases=list(DUPLEX_PHASES), required_branches=list(DUPLEX_BRANCHES),
                                       oracle='qos2_duplex_evidence.check_evidence', evidence_status='partial-selected-evidence', review_status='pending'))
            duplex = next((c for c in catalog['cases'] if c['id'] == DUPLEX_ID), None)
            require(duplex is not None and duplex['scenario_version'] == DUPLEX_VERSION
                    and duplex['entrypoint'] == 'qos2_duplex', 'atomic supplementary duplex case/version mismatch')
            fanout = next((c for c in catalog['cases'] if c['id'] == FANOUT_ID), None)
            require(fanout is not None and fanout['scenario_version'] == FANOUT_VERSION
                    and fanout['entrypoint'] == 'qos2_fanout', 'atomic supplementary fanout case/version mismatch')
            flow = next((c for c in catalog['cases'] if c['id'] == FLOW_ID), None)
            require(atom['supplemental_evidence'] == supplement and flow is not None
                    and flow['scenario_version'] == FLOW_VERSION and flow['entrypoint'] == 'qos2_flow',
                    'atomic supplementary flow case/version/role/phase mismatch')
    return groups


def verify_compounds(mapping, blocks, paragraphs):
    for group in mapping['compound_requirements']:
        selected = group['source_blocks']
        positions = [i for i in range(len(blocks) - len(selected) + 1) if blocks[i:i + len(selected)] == selected]
        require(len(positions) == 1, 'complete list is absent, changed, reordered or ambiguous')
        for p in group['context_paragraphs']:
            count = paragraphs.count(p['source_text'])
            require(count > 0 if group['protocol_role'] == 'sender' else count == 1,
                    'context differs from pinned main body')
        if group['protocol_role'] == 'sender':
            # These two nonempty paragraphs must immediately follow the marker.
            offset = positions[0] + len(selected)
            require(blocks[offset:offset + 2] == [dict(kind='p', text=p['source_text']) for p in group['context_paragraphs']],
                    'sender reuse/parallelism context moved')
