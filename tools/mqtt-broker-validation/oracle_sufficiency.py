#!/usr/bin/env python3
"""Validate oracle, applicability, SHOULD deviation and sufficiency ledger (MQTT-T01/T02/T03)."""
import hashlib
import importlib
import re
import sys

from common import InputError, ROOT, load_catalog, read_json
from normative_map import SELECTED_IDS, load_mapping
from schema_check import check, load_schema

MIXED_IDS = ('MQTT-3.3.5-1', 'MQTT-3.8.4-5', 'MQTT-3.8.4-6')


def _event_kinds():
    schema = load_schema('event')
    return {branch['properties']['kind']['const'] for branch in schema['oneOf']}


def _test_methods():
    methods = set()
    for path in (ROOT / 'tests').glob('test_*.py'):
        text = path.read_text()
        for match in re.finditer(r'^\s*def (test_[A-Za-z0-9_]+)\(', text, re.M):
            methods.add(f'{path.stem}.{match.group(1)}')
    return methods


def _verify_callable(dotted):
    mod_name, fn_name = dotted.split('.', 1)
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as error:
        raise InputError(f'missing parent verifier module: {mod_name}') from error
    fn = getattr(mod, fn_name, None)
    if not callable(fn):
        raise InputError(f'missing callable parent verifier: {dotted}')


def _check_entry_refs(entry, cases_by_id, event_kinds, test_methods):
    for cid in entry.get('case_ids', entry.get('required_cases', [])):
        if cid not in cases_by_id:
            raise InputError(f'unknown case_id: {cid}')
    for group in entry.get('alternative_groups', []):
        for cid in group:
            if cid not in cases_by_id:
                raise InputError(f'unknown alternative case_id: {cid}')
    for cid in entry.get('conditional_cases', []):
        if cid not in cases_by_id:
            raise InputError(f'unknown conditional case_id: {cid}')
    ref_cases = entry.get('case_ids', entry.get('required_cases', []))
    known_assertions = {a for cid in ref_cases for a in cases_by_id[cid]['required_assertions']}
    for assertion in entry['required_assertions']:
        if assertion not in known_assertions:
            raise InputError(f'unknown required_assertion {assertion} for cases {ref_cases}')
    unknown_events = set(entry['required_event_kinds']) - event_kinds
    if unknown_events:
        raise InputError(f'unknown event kinds: {sorted(unknown_events)}')
    _verify_callable(entry['parent_verifier'])
    for name in entry['positive_oracles'] + entry['negative_counterexamples']:
        if name not in test_methods:
            raise InputError(f'missing referenced test method: {name}')


def validate_ledger(value, catalog=None, mapping=None):
    check('oracle-sufficiency', value)
    if catalog is None:
        catalog = load_catalog(ROOT / 'catalog.json', read_json(ROOT / 'upstream.lock.json'))
    if mapping is None:
        mapping = load_mapping(catalog=catalog)

    cases_by_id = {c['id']: c for c in catalog['cases']}
    event_kinds = _event_kinds()
    test_methods = _test_methods()
    mappings_by_id = {m['official_id']: m for m in mapping['mappings']}

    # 1. Selected 18 official IDs
    seen_sel = set()
    for entry in value['selected_id_oracles']:
        oid = entry['id']
        if oid in seen_sel or oid not in SELECTED_IDS:
            raise InputError(f'invalid or duplicate selected_id: {oid}')
        seen_sel.add(oid)
        if entry['applicability'] == 'excluded' and not entry['exclusion_rationale']:
            raise InputError(f'excluded entry {oid} requires exclusion_rationale')
        if entry['applicability'] == 'conditional' and not entry['condition']:
            raise InputError(f'conditional entry {oid} requires condition')
        if entry['normative_level'] in ('SHOULD', 'SHOULD NOT') and entry['should_deviation'] == '':
            raise InputError(f'empty should_deviation string on {oid}')
        if mappings_by_id[oid]['case_id'] not in entry['case_ids']:
            raise InputError(f'selected_id {oid} missing its mapped case_id')
        _check_entry_refs(entry, cases_by_id, event_kinds, test_methods)
    if seen_sel != SELECTED_IDS:
        raise InputError('selected_id_oracles must match all 18 normative-map IDs')

    # 2. Compound QoS2 12 MUST atoms
    expected_atoms = {(g['official_id'], a['key'])
                      for g in mapping['compound_requirements'] for a in g['atoms']}
    seen_atoms = set()
    for entry in value['compound_atom_oracles']:
        key = (entry['parent_id'], entry['key'])
        if key in seen_atoms or key not in expected_atoms:
            raise InputError(f'invalid or duplicate compound atom: {key}')
        seen_atoms.add(key)
        _check_entry_refs(entry, cases_by_id, event_kinds, test_methods)
    if seen_atoms != expected_atoms:
        raise InputError('compound_atom_oracles must cover all 12 QoS2 MUST atoms')

    # 3. MIXED QoS 3 official IDs (MQTT-T01/T03 OASIS source digest bound)
    seen_mixed = set()
    for entry in value['mixed_qos_draft_oracles']:
        oid = entry['id']
        if oid in seen_mixed or oid not in MIXED_IDS:
            raise InputError(f'invalid or duplicate mixed_qos id: {oid}')
        seen_mixed.add(oid)
        if f'[{oid}]' not in entry['source_text']:
            raise InputError(f'official marker [{oid}] absent from source_text')
        if hashlib.sha256(entry['source_text'].encode()).hexdigest() != entry['source_sha256']:
            raise InputError(f'source_sha256 mismatch on {oid}')
        _check_entry_refs(entry, cases_by_id, event_kinds, test_methods)
    if seen_mixed != set(MIXED_IDS):
        raise InputError('mixed_qos_draft_oracles must cover MQTT-3.3.5-1, 3.8.4-5, 3.8.4-6')

    # 4. Internal 9 catalog obligations
    expected_obls = {o['id']: o for o in catalog['obligations']}
    seen_obls = set()
    for entry in value['internal_obligation_oracles']:
        oid = entry['id']
        if oid in seen_obls or oid not in expected_obls:
            raise InputError(f'invalid or duplicate internal obligation: {oid}')
        seen_obls.add(oid)
        if set(entry['required_cases']) | set(entry['conditional_cases']) != set(expected_obls[oid]['required_cases']):
            raise InputError(f'obligation {oid} cases must partition catalog required_cases cleanly')
        if entry['applicability'] == 'excluded' and not entry['exclusion_rationale']:
            raise InputError(f'excluded obligation {oid} requires exclusion_rationale')
        if entry['applicability'] == 'conditional' and not entry['condition']:
            raise InputError(f'conditional obligation {oid} requires condition')
        _check_entry_refs(entry, cases_by_id, event_kinds, test_methods)
    if seen_obls != set(expected_obls):
        raise InputError('internal_obligation_oracles must cover all 9 catalog obligations')
    return value


def _case_passed(outcome):
    return outcome.get('status') == 'PASS' and outcome.get('evidence_validated') is True


def evaluate_sufficiency(ledger, case_outcomes, independent_approvals=0):
    """Aggregate sufficiency without offsetting FAIL/BLOCKED/NOT_TESTED across obligations (MQTT-T02)."""
    validate_ledger(ledger)
    if type(independent_approvals) is not int or independent_approvals < 0:
        raise InputError('invalid independent_approvals count')
    by_case = {r['case_id']: r for r in case_outcomes}

    all_entries = (
        list(ledger['selected_id_oracles'])
        + list(ledger['compound_atom_oracles'])
        + list(ledger['mixed_qos_draft_oracles'])
        + list(ledger['internal_obligation_oracles'])
    )

    counts = {'PASS': 0, 'FAIL': 0, 'ERROR': 0, 'BLOCKED': 0, 'NOT_TESTED': 0, 'EXCLUDED': 0}
    should_deviations = []
    per_item = []

    for entry in all_entries:
        name = entry.get('id') or f"{entry['parent_id']}:{entry['key']}"
        applicability = entry['applicability']
        if entry.get('should_deviation'):
            should_deviations.append({'id': name, 'rationale': entry['should_deviation']})
        if applicability == 'excluded':
            counts['EXCLUDED'] += 1
            per_item.append({'id': name, 'status': 'EXCLUDED', 'rationale': entry['exclusion_rationale']})
            continue

        req_cases = list(entry.get('case_ids') or entry.get('required_cases'))
        alt_groups = entry.get('alternative_groups', [])
        if alt_groups:
            group_statuses = []
            for group in alt_groups:
                statuses = [by_case.get(c, {'status': 'NOT_TESTED', 'evidence_validated': False}) for c in group]
                if all(_case_passed(s) for s in statuses):
                    group_statuses.append('PASS')
                elif any(s.get('status') == 'FAIL' for s in statuses):
                    group_statuses.append('FAIL')
                elif any(s.get('status') == 'ERROR' or (s.get('status') == 'PASS' and not s.get('evidence_validated')) for s in statuses):
                    group_statuses.append('ERROR')
                elif any(s.get('status') == 'BLOCKED' for s in statuses):
                    group_statuses.append('BLOCKED')
                else:
                    group_statuses.append('NOT_TESTED')
            if 'PASS' in group_statuses:
                status = 'PASS'
            elif 'FAIL' in group_statuses:
                status = 'FAIL'
            elif 'ERROR' in group_statuses:
                status = 'ERROR'
            elif 'BLOCKED' in group_statuses:
                status = 'BLOCKED'
            else:
                status = 'NOT_TESTED'
        else:
            outcomes = [by_case.get(c, {'status': 'NOT_TESTED', 'evidence_validated': False}) for c in req_cases]
            if any(o.get('status') == 'FAIL' for o in outcomes):
                status = 'FAIL'
            elif any(o.get('status') == 'ERROR' or (o.get('status') == 'PASS' and not o.get('evidence_validated')) for o in outcomes):
                status = 'ERROR'
            elif any(o.get('status') == 'BLOCKED' for o in outcomes):
                status = 'BLOCKED'
            elif all(_case_passed(o) for o in outcomes):
                status = 'PASS'
            else:
                status = 'NOT_TESTED'

        counts[status] += 1
        per_item.append({'id': name, 'status': status, 'applicability': applicability})

    # Non-offsetting strict verdict rules
    if counts['FAIL'] > 0:
        verdict = 'FAIL'
    elif counts['ERROR'] > 0:
        verdict = 'ERROR'
    elif counts['BLOCKED'] > 0:
        verdict = 'BLOCKED'
    elif counts['NOT_TESTED'] > 0:
        verdict = 'NOT_TESTED'
    elif independent_approvals == 0 or ledger['review_status'] == 'pending':
        verdict = 'INCONCLUSIVE'
    else:
        verdict = 'INCONCLUSIVE'  # TargetedEvidenceOnly never upgrades to CONFORMANT

    return {
        'overall_status': 'Partial',
        'conformance_verdict': verdict,
        'independent_approvals': independent_approvals,
        'counts': counts,
        'should_deviations': should_deviations,
        'items': per_item,
    }


def load_ledger(path=ROOT / 'oracle-sufficiency.json'):
    return validate_ledger(read_json(path))


def main():
    try:
        ledger = load_ledger()
        print(
            f"Oracle & sufficiency ledger valid: {len(ledger['selected_id_oracles'])} selected IDs, "
            f"{len(ledger['compound_atom_oracles'])} compound atoms, "
            f"{len(ledger['mixed_qos_draft_oracles'])} MIXED draft IDs, "
            f"{len(ledger['internal_obligation_oracles'])} internal obligations; "
            f"review_status={ledger['review_status']}; independent_approvals={ledger['independent_approvals']}."
        )
        return 0
    except (InputError, OSError, ValueError) as error:
        print('oracle-sufficiency rejected: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
