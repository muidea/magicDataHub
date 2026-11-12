"""Shared input, identity and bounded command helpers."""
import hashlib
import json
import math
import re
from pathlib import Path
import subprocess

from calibration_model import MUTATIONS

ROOT = Path(__file__).resolve().parent


class InputError(ValueError):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def decode_json(body):
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise InputError('duplicate JSON object key: ' + key)
            value[key] = item
        return value
    def nonfinite(_):
        raise InputError('nonfinite JSON number')
    def finite_float(raw):
        value = float(raw)
        if not math.isfinite(value):
            nonfinite(raw)
        return value
    return json.loads(body, object_pairs_hook=unique_pairs, parse_constant=nonfinite, parse_float=finite_float)


def read_json(path):
    try:
        return decode_json(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise InputError(f'invalid or missing JSON input: {path}') from error


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.new')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def command(*args, timeout=30):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True, timeout=timeout)


def source_identity(root):
    patterns = ['*.py', '*.json', '*.md', '.github/workflows/*.yml', 'requirements*.txt']
    paths = {p for pattern in patterns for p in root.rglob(pattern) if p.is_file()
             and not any(part in {'.git', '.venv', '__pycache__', 'results'} for part in p.relative_to(root).parts)}
    identity = {str(p.relative_to(root)): digest(p) for p in sorted(paths)}
    if root.resolve() == ROOT:
        for relative in ('.github/workflows/verify-mqtt.yml', 'scripts/check-mqtt-validation.sh'):
            identity['../../' + relative] = digest(ROOT.parents[1] / relative)
    return identity


def check_upstream(root, lock):
    if not root.is_dir():
        raise InputError('upstream checkout does not exist')
    try:
        revision = command('git', '-C', str(root), 'rev-parse', 'HEAD').strip()
    except subprocess.CalledProcessError as error:
        raise InputError('upstream is not a Git checkout') from error
    if revision != lock['mosquitto']['commit']:
        raise InputError('upstream revision mismatch')
    for relative, expected in lock['mosquitto']['files'].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or digest(path) != expected:
            raise InputError('upstream digest mismatch: ' + relative)


def load_catalog(path=ROOT / 'catalog.json', lock=None):
    value = read_json(path)
    return validate_catalog(value, lock)


def validate_catalog(value, lock):
    # Local import avoids the schema loader's common input-helper dependency.
    from schema_check import check
    check('catalog', value)
    if not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('inventory_scope') != 'representative' or value.get('complete_official_inventory') is not False:
        raise InputError('unsupported catalog schema or scope')
    cases, obligations = value.get('cases', []), value.get('obligations', [])
    if not isinstance(cases, list) or not isinstance(obligations, list) or not all(isinstance(x, dict) for x in cases + obligations):
        raise InputError('catalog entries must be objects')
    ids = [case.get('id') for case in cases]
    requirement_ids = [item.get('id') for item in obligations]
    if any(not isinstance(x, str) or not re.fullmatch(r'[A-Z0-9]+(?:-[A-Z0-9]+)*', x) for x in ids + requirement_ids):
        raise InputError('catalog IDs must be stable safe uppercase identifiers')
    if not cases or not all(isinstance(x, str) and x for x in ids) or len(set(ids)) != len(ids):
        raise InputError('missing or duplicate case IDs')
    if not all(isinstance(x, str) and x for x in requirement_ids) or len(set(requirement_ids)) != len(requirement_ids):
        raise InputError('missing or duplicate obligation IDs')
    for case in cases:
        if not isinstance(case.get('runner'), str) or not isinstance(case.get('entrypoint', ''), str):
            raise InputError('invalid case runner or entrypoint')
        for field in ['required_assertions', 'limitations', 'evidence_types', 'obligations', 'required_events']:
            values = case.get(field, [])
            if not isinstance(values, list) or not all(isinstance(x, str) and x for x in values) or len(set(values)) != len(values):
                raise InputError('invalid case string-list field: ' + field)
        if not isinstance(case.get('preconditions'), dict) or not isinstance(case.get('name'), str):
            raise InputError('invalid case metadata')
        if 'topic_vector_count' in case and (type(case['topic_vector_count']) is not int or case['topic_vector_count'] <= 0):
            raise InputError('invalid topic vector count')
        if case.get('runner') not in {'generic', 'upstream', 'calibration'}:
            raise InputError('unknown runner')
        if case.get('profile') != 'mqtt311-tcp-anonymous-memory':
            raise InputError('unsupported case profile')
        if not case.get('preconditions') or not case.get('limitations') or not case.get('required_assertions'):
            raise InputError('missing case preconditions, limitations or assertions')
        if case['runner'] == 'generic' and case.get('entrypoint') not in {
                'connect', 'retained', 'topics', 'system_topics', 'paho', 'qos2_recovery', 'qos2_flow', 'qos2_fanout', 'qos2_duplex', 'mixed_qos'}:
            raise InputError('unsupported generic entrypoint')
        if case['runner'] == 'calibration' and (case['id'] != 'TOOL-CAL-001' or case.get('expected_faults') != len(MUTATIONS)):
            raise InputError('unsupported calibration')
        if case['runner'] == 'upstream':
            if lock is None or not any(case.get('upstream') == {'file': x['file'], 'parameters': x['parameters']}
                                       for x in lock['mosquitto']['cases']):
                raise InputError('unlocked upstream invocation')
        if not set(case.get('obligations', [])) <= set(requirement_ids):
            raise InputError('unknown obligation reference')
    for item in obligations:
        for field in ['source_sections', 'required_cases', 'limitations']:
            values = item.get(field)
            if not isinstance(values, list) or not values or not all(isinstance(x, str) and x for x in values) or len(set(values)) != len(values):
                raise InputError('invalid obligation string-list field: ' + field)
        if item.get('completion_rule') != 'all_of_selected_scenarios' or item.get('normative_level') != 'pending_review' or item.get('official_ids') != []:
            raise InputError('unreviewed normative claim')
        if not item.get('source_sections') or item.get('review_status') != 'pending':
            raise InputError('this representative catalog requires explicit pending normative review')
        if not item.get('required_cases') or not set(item['required_cases']) <= set(ids):
            raise InputError('invalid obligation case references')
        if not item.get('limitations'):
            raise InputError('missing obligation evidence boundary')
        for identity in item['required_cases']:
            if item['id'] not in cases[ids.index(identity)]['obligations']:
                raise InputError('case and obligation mappings disagree')
    for case in cases:
        for reference in case['obligations']:
            if case['id'] not in obligations[requirement_ids.index(reference)]['required_cases']:
                raise InputError('reverse case and obligation mappings disagree')
    if sum(c['runner'] == 'calibration' for c in cases) != 1:
        raise InputError('one tool calibration is required')
    return value
