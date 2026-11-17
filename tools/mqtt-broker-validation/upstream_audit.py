#!/usr/bin/env python3
"""Check selected upstream assessments; never import or run candidate source.

Normal checks are offline and need no source checkout. --verify-sources also
checks the pinned external checkouts. --fetch explicitly prepares missing ones;
it never resets or overwrites an existing checkout.
"""
import argparse
from collections import Counter
from datetime import date
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile

from common import InputError, ROOT, command, digest, load_catalog, read_json
from schema_check import check

REPOSITORIES = {
    'mosquitto': 'https://github.com/eclipse-mosquitto/mosquitto',
    'paho-testing': 'https://github.com/eclipse-paho/paho.mqtt.testing',
    'emqx': 'https://github.com/emqx/emqx',
}
DEFAULT_CACHE = Path.home() / '.cache/mqtt-broker-validation'


def safe_relative(value):
    path = PurePosixPath(value)
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', value)
            or path.is_absolute() or any(part in {'..', '.git'} for part in path.parts)
            or path.as_posix() != value):
        raise InputError('unsafe upstream assessment path')
    return path


def validate_assessment(value, catalog, lock):
    check('upstream-audit', value)
    try:
        date.fromisoformat(value['observed_date'])
    except ValueError as error:
        raise InputError('invalid upstream observation date') from error
    if value['adapter_sha256'] != digest(ROOT / 'adapters.py'):
        raise InputError('adapter changed; reassess upstream integration')
    projects = value['projects']
    if Counter(p['id'] for p in projects) != Counter(REPOSITORIES.keys()):
        raise InputError('assessment requires one entry per selected upstream project')
    directories = [p['cache_directory'] for p in projects]
    if len(set(directories)) != len(directories):
        raise InputError('duplicate upstream cache directory')
    roots = [safe_relative(name) for name in directories]
    if any(left != right and left.is_relative_to(right) for left in roots for right in roots):
        raise InputError('overlapping upstream cache directories')
    selected = {case['id']: case for case in catalog['cases'] if case['runner'] == 'upstream'}
    integrated, asset_ids = set(), set()
    for project in projects:
        safe_relative(project['cache_directory'])
        if project['repository'] != REPOSITORIES[project['id']]:
            raise InputError('upstream repository differs from selected project')
        if not project['files']:
            raise InputError('empty assessed source inventory')
        for name in project['files']:
            safe_relative(name)
        if project['id'] == 'mosquitto':
            if (project['revision'] != lock['mosquitto']['commit']
                    or project['reference_kind'] != 'tag' or project['reference'] != lock['mosquitto']['tag']
                    or project['files'] != lock['mosquitto']['files']):
                raise InputError('Mosquitto assessment differs from runtime dependency lock')
        elif project['reference_kind'] != 'commit-snapshot' or project['reference'] != project['revision']:
            raise InputError('candidate upstream must use an explicit commit snapshot')
        license_file = 'LICENSE.txt' if project['id'] == 'mosquitto' else 'LICENSE'
        if license_file not in {item['file'] for item in project['license_evidence']}:
            raise InputError('missing root license evidence')
        evidence = list(project['license_evidence'])
        for asset in project['assets']:
            if asset['id'] in asset_ids:
                raise InputError('duplicate assessed asset ID')
            asset_ids.add(asset['id'])
            if asset['file'] not in project['files'] or asset['file'] not in {item['file'] for item in asset['evidence']}:
                raise InputError('asset lacks locked primary source evidence')
            evidence += asset['evidence']
            if asset['reuse_grade'] not in {'A', 'B', 'C', 'D'}:
                raise InputError('invalid reuse_grade on assessed asset')
            if any(not asset.get(field) for field in ('adaptations', 'evidence_limits', 'next_steps')):
                raise InputError('assessed asset lacks required adaptation/limit/decision rationale')
            for dependency in asset['dependencies']:
                if '/' in dependency and dependency.endswith(('.py', '.erl', '.sh')):
                    if asset['integration_status'] == 'integrated':
                        matched = dependency in project['files']
                    else:
                        matched = dependency in project['files'] or any(
                            path.endswith('/' + dependency) for path in project['files']
                        )
                    if not matched:
                        raise InputError('asset dependency path lacks locked source evidence')
            identity = asset['catalog_case_id']
            if asset['integration_status'] == 'integrated':
                case = selected.get(identity)
                if (project['id'] != 'mosquitto' or case is None or identity in integrated
                        or asset['id'] != identity or asset['kind'] != 'test-invocation'
                        or asset['reuse_grade'] != 'B' or asset['entrypoints'] != ['do_test']
                        or asset['protocols'] != ['mqtt311']
                        or asset['file'] != 'test/broker/' + case['upstream']['file']
                        or asset['parameters'] != case['upstream']['parameters']):
                    raise InputError('integrated assessment differs from executable B invocation')
                integrated.add(identity)
            elif identity is not None or asset['parameters'] is not None:
                raise InputError('candidate assessment cannot claim an executable catalog invocation')
        for item in evidence:
            if item['file'] not in project['files']:
                raise InputError('assessment evidence references unlocked source')
    if integrated != set(selected):
        raise InputError('selected executable upstream cases lack individual assessment')
    return value


def load_assessment(path=ROOT / 'upstream-audit.json', catalog=None, lock=None):
    lock = read_json(ROOT / 'upstream.lock.json') if lock is None else lock
    catalog = load_catalog(ROOT / 'catalog.json', lock) if catalog is None else catalog
    return validate_assessment(read_json(path), catalog, lock)


def verify_project(project, cache):
    """Verify bytes and line references without loading any upstream module."""
    directory = cache.resolve() / str(safe_relative(project['cache_directory']))
    if directory.is_symlink() or directory.resolve() != directory or not directory.is_dir():
        raise InputError('missing or noncanonical upstream checkout: ' + project['id'])
    revision = command('git', '-C', str(directory), 'rev-parse', 'HEAD', timeout=15).strip()
    origin = command('git', '-C', str(directory), 'remote', 'get-url', 'origin', timeout=15).strip()
    if revision != project['revision'] or origin.removesuffix('.git') != project['repository']:
        raise InputError('upstream revision or origin mismatch: ' + project['id'])
    texts = {}
    for relative, expected in project['files'].items():
        path = directory / str(safe_relative(relative))
        if path.resolve() != path or not path.is_file() or digest(path) != expected:
            raise InputError('assessed upstream file digest mismatch: ' + relative)
        texts[relative] = path.read_text().splitlines()
    evidence = project['license_evidence'] + [item for asset in project['assets'] for item in asset['evidence']]
    for item in evidence:
        lines = texts[item['file']]
        if item['line'] > len(lines) or lines[item['line'] - 1].strip() != item['text']:
            raise InputError('assessed source line reference mismatch: ' + item['file'])
    return directory


def fetch_missing(project, cache):
    """Fetch only on explicit request; retain an existing checkout even if invalid."""
    cache = cache.resolve()
    destination = cache / str(safe_relative(project['cache_directory']))
    if destination.exists() or destination.is_symlink():
        return
    # Do not create even a staging directory through an existing symlink parent.
    parent = destination.parent
    while parent != cache:
        if parent.is_symlink() or parent.exists() and not parent.is_dir():
            raise InputError('noncanonical upstream cache parent')
        parent = parent.parent
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mqtt-audit-fetch-', dir=destination.parent) as temporary:
        staged_cache = Path(temporary)
        staged = staged_cache / str(safe_relative(project['cache_directory']))
        staged.mkdir(parents=True)
        git = ['git', '-c', 'core.hooksPath=/dev/null', '-C', str(staged)]
        command(*git, 'init', '--quiet')
        command(*git, 'remote', 'add', 'origin', project['repository'])
        command(*git, 'fetch', '--depth', '1', '--filter=blob:none', 'origin', project['revision'], timeout=120)
        command(*git, 'sparse-checkout', 'set', '--no-cone', '--',
                *['/' + name for name in project['files']], timeout=30)
        command(*git, '-c', 'advice.detachedHead=false', 'checkout', '--detach', project['revision'], timeout=120)
        verify_project(project, staged_cache)
        staged.rename(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', type=Path, default=DEFAULT_CACHE)
    parser.add_argument('--verify-sources', action='store_true', help='verify pinned cached source and evidence lines offline')
    parser.add_argument('--fetch', action='store_true', help='explicitly fetch missing selected checkouts, then verify sources')
    args = parser.parse_args()
    try:
        value = load_assessment()
        if args.fetch or args.verify_sources:
            for project in value['projects']:
                if args.fetch:
                    fetch_missing(project, args.cache_dir)
                verify_project(project, args.cache_dir)
        count = sum(len(p['assets']) for p in value['projects'])
        print(f'Selected upstream assessment valid: {len(value["projects"])} projects, {count} assets; '
              + ('source bytes/lines verified' if args.fetch or args.verify_sources else 'metadata only; source check not requested')
              + '; runtime results not produced; normative/independent review pending.')
        return 0
    except (InputError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('upstream assessment rejected: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
