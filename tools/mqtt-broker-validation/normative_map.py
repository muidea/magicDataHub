#!/usr/bin/env python3
"""Draft selected connection/Retained/QoS2/Topic mappings; never approval or runtime results."""
import argparse
import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import tempfile
import sys
import urllib.request

from common import InputError, ROOT, load_catalog, read_json
from retained import PHASES, VERSION
from connection import PHASES as CONNECT_PHASES, VERSION as CONNECT_VERSION
from qos2 import BRANCH_NAMES, VERSION as QOS2_VERSION
from topic_vectors import PHASES as TOPIC_PHASES, VERSION as TOPIC_VERSION
from schema_check import check
from compound_source import Blocks, validate_compounds, verify_compounds

DEFAULT_CACHE = Path.home() / '.cache/mqtt-broker-validation'
RETAIN_IDS = {'MQTT-3.3.1-' + str(n) for n in [5, 6, 8, 9, 10, 11, 12]}
CONNECT_IDS = {'MQTT-3.1.4-4', 'MQTT-3.2.0-1', 'MQTT-3.2.2-1', 'MQTT-3.12.4-1'}
QOS2_IDS = {'MQTT-2.3.1-6', 'MQTT-3.2.2-2', 'MQTT-4.4.0-1'}
TOPIC_IDS = {'MQTT-3.8.4-1', 'MQTT-3.8.4-2', 'MQTT-4.7.2-1', 'MQTT-4.7.3-4'}
SELECTED_IDS = RETAIN_IDS | CONNECT_IDS | QOS2_IDS | TOPIC_IDS
PHASE_BINDINGS = {
    **{'MQTT-3.3.1-' + str(n): list(PHASES[:2]) for n in [5, 6, 8]},
    **{'MQTT-3.3.1-' + str(n): list(PHASES[2:5]) for n in [9, 10]},
    'MQTT-3.3.1-11': list(PHASES[3:5]),
    'MQTT-3.3.1-12': [PHASES[1], PHASES[4]],
}
PHASE_BINDINGS.update({identity: [CONNECT_PHASES[1] if identity == 'MQTT-3.12.4-1'
                                 else CONNECT_PHASES[0]] for identity in CONNECT_IDS})
PHASE_BINDINGS.update({identity: list(BRANCH_NAMES) for identity in QOS2_IDS})
PHASE_BINDINGS.update({identity: list(TOPIC_PHASES) for identity in ['MQTT-3.8.4-1', 'MQTT-3.8.4-2']})
PHASE_BINDINGS['MQTT-4.7.2-1'] = [TOPIC_PHASES[i] for i in [6, 7, 8]]
PHASE_BINDINGS['MQTT-4.7.3-4'] = [TOPIC_PHASES[i] for i in [0, 9, 10, 13, 14]]
CONTRACTS = {
    'MQTT311-RETAIN-DELETE-001': (VERSION, PHASES[:-1], RETAIN_IDS),
    'MQTT311-CONNECT-001': (CONNECT_VERSION, CONNECT_PHASES[:-1], CONNECT_IDS),
    'MQTT311-QOS2-RECOVER-001': (QOS2_VERSION, BRANCH_NAMES, QOS2_IDS),
    'MQTT311-TOPIC-MATCH-001': (TOPIC_VERSION, TOPIC_PHASES, TOPIC_IDS),
}


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active, self.finished, self.items = None, False, []

    def handle_starttag(self, tag, attrs):
        if dict(attrs).get('class') == 'AppendixHeading1':
            self.finished, self.active = True, None
        if not self.finished and tag == 'p':
            self.active = []

    def handle_endtag(self, tag):
        if tag == 'p' and self.active is not None:
            self.items.append(re.sub(r'\s+', ' ', ''.join(self.active)).strip())
            self.active = None

    def handle_data(self, value):
        if self.active is not None:
            self.active.append(value)


def validate_mapping(value, catalog):
    check('normative-map', value)
    paragraphs = {p['id']: p for p in value['paragraphs']}
    if len(paragraphs) != len(value['paragraphs']):
        raise InputError('duplicate normative paragraph ID')
    for paragraph in paragraphs.values():
        if hashlib.sha256(paragraph['source_text'].encode()).hexdigest() != paragraph['sha256']:
            raise InputError('normative paragraph digest mismatch')
    cases = {c['id']: c for c in catalog['cases']}
    rows = value['mappings']
    if {r['official_id'] for r in rows} != SELECTED_IDS or len(rows) != len(SELECTED_IDS):
        raise InputError('selected official-ID mapping missing or duplicated')
    referenced, phases = set(), {identity: set() for identity in CONTRACTS}
    for row in rows:
        paragraph = paragraphs.get(row['paragraph_id'])
        if paragraph is None or '[' + row['official_id'] + ']' not in paragraph['source_text']:
            raise InputError('official ID absent from its full source paragraph')
        case = cases[row['case_id']]
        version, _, identities = CONTRACTS[row['case_id']]
        if (row['official_id'] not in identities
                or row['scenario_version'] != case.get('scenario_version') or row['scenario_version'] != version
                or row['required_assertions'] != case['required_assertions']
                or row['required_phases'] != PHASE_BINDINGS[row['official_id']]):
            raise InputError('mapping differs from current scenario/evidence contract')
        referenced.add(row['paragraph_id'])
        phases[row['case_id']].update(row['required_phases'])
    if referenced != set(paragraphs) or any(not set(contract[1]) <= phases[identity]
                                          for identity, contract in CONTRACTS.items()):
        raise InputError('unbound paragraph or selected proof phase')
    if {g['id']: g['case_id'] for g in value['gaps']} != {
            'MQTT-GAP-001': 'MQTT311-RETAIN-DELETE-001', 'MQTT-GAP-002': 'MQTT311-CONNECT-001',
            'MQTT-GAP-003': 'MQTT311-QOS2-RECOVER-001',
            'MQTT-GAP-004': 'MQTT311-TOPIC-MATCH-001'} or len(value['gaps']) != 4:
        raise InputError('selected validation-gap identities mismatch')
    validate_compounds(value, catalog)
    return value


def load_mapping(path=ROOT / 'normative-map.json', catalog=None):
    if catalog is None:
        catalog = load_catalog(ROOT / 'catalog.json', read_json(ROOT / 'upstream.lock.json'))
    return validate_mapping(read_json(path), catalog)


def verify_source(mapping, path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise InputError('missing or symlinked OASIS source')
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != mapping['source']['sha256']:
        raise InputError('OASIS source digest mismatch; existing source preserved')
    parser = Paragraphs()
    parser.feed(body.decode(mapping['source']['encoding']))
    if not parser.finished:
        raise InputError('OASIS main-body/appendix boundary missing')
    for paragraph in mapping['paragraphs']:
        if parser.items.count(paragraph['source_text']) != 1:
            raise InputError('draft paragraph differs from pinned OASIS main body')
    blocks = Blocks()
    blocks.feed(body.decode(mapping['source']['encoding']))
    if not blocks.finished:
        raise InputError('compound OASIS main-body/appendix boundary missing')
    verify_compounds(mapping, blocks.blocks, parser.items)
    return path


def fetch_source(mapping, cache):
    cache = cache.absolute()
    path = cache / mapping['source']['cache_file']
    if any(parent.is_symlink() for parent in [path.parent, *path.parents]):
        raise InputError('symlinked OASIS cache directory')
    if path.exists() or path.is_symlink():
        return verify_source(mapping, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(mapping['source']['url'], timeout=30) as response:
        body = response.read(4 * 1024 * 1024 + 1)
    if len(body) > 4 * 1024 * 1024 or hashlib.sha256(body).hexdigest() != mapping['source']['sha256']:
        raise InputError('download differs from pinned OASIS source')
    with tempfile.NamedTemporaryFile(prefix='mqtt-oasis-', dir=path.parent) as temporary:
        temporary.write(body)
        temporary.flush()
        verify_source(mapping, temporary.name)
        # Exclusive creation: never overwrite an existing cache, even on a race.
        os.link(temporary.name, path)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='offline verification against an explicit pinned OASIS HTML')
    parser.add_argument('--cache-dir', type=Path, default=DEFAULT_CACHE)
    parser.add_argument('--fetch-source', action='store_true', help='explicitly fetch missing pinned OASIS HTML into cache')
    args = parser.parse_args()
    try:
        value = load_mapping()
        if args.fetch_source:
            fetch_source(value, args.cache_dir)
        if args.source:
            verify_source(value, args.source)
        print('Draft selected mappings valid: 18 official IDs (7 Retained, 4 connection, 3 QoS2, 4 Topic); '
              + ('pinned source verified' if args.source or args.fetch_source else 'metadata only')
              + '; 2 compound inventories / 12 MUST atoms imported (not mapped coverage)'
              + '; semantic/independent review pending; no conformance or runtime result produced.')
        return 0
    except (InputError, OSError, ValueError, KeyError) as error:
        print('normative mapping rejected: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
