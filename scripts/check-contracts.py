#!/usr/bin/env python3
"""Require exact successful execution lines, never treating ignored tests as evidence."""
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def verify(scope, output, mapping):
    expected_ids = {f'MDH-COMP-{number:03d}' for number in range(1, 13)} | {'MDH-IMAGES-001', 'MDH-WORKFLOW-001'}
    if set(mapping) != expected_ids or any(not record for record in mapping.values()):
        raise ValueError('contract mapping is incomplete')
    for record in mapping.values():
        if set(record) - {'local', 'remote', 'images', 'workflow'} or any(
                not isinstance(names, list) or not names or
                any(not isinstance(name, str) or not re.fullmatch(r'[\w:-]+', name) for name in names)
                for names in record.values()):
            raise ValueError('invalid or empty evidence requirement')
    successes = set(re.findall(r'^test ([\w:-]+) \.\.\. ok$', output, re.MULTILINE))
    failures = re.findall(r'^test [\w:-]+ \.\.\. (?:FAILED|ignored.*)$|^test result: FAILED.*$', output, re.MULTILINE)
    # Local suites deliberately contain the one owned-fixture ignored case. It is
    # never a success; any actual failed test blocks every scope.
    if any('FAILED' in line for line in failures):
        raise ValueError('test failure in evidence')
    required = {key: record[scope] for key, record in mapping.items() if scope in record}
    if not required:
        raise ValueError('scope has no contracts')
    missing = {key: sorted(set(names) - successes) for key, names in required.items() if set(names) - successes}
    if missing:
        raise ValueError('missing successful execution: ' + json.dumps(missing, sort_keys=True))
    return sorted(required)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=['local', 'remote', 'images', 'workflow'], required=True)
    parser.add_argument('--results', type=Path, required=True, help='output of a successfully exited runner')
    args = parser.parse_args()
    mapping = json.loads((ROOT / 'tests/contracts/mapping.json').read_text())
    contracts = verify(args.scope, args.results.read_text(), mapping)
    print(json.dumps({'status': 'Passed', 'scope': args.scope, 'contracts': contracts,
                      'other_scopes': 'NotRun by this invocation'}, indent=2))


if __name__ == '__main__':
    main()
