#!/usr/bin/env python3
"""Check actual locked dependency identity and normal/build closures, offline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REV = '336ba20196044e1b81fb2ca297a18b5e258a5800'
SOURCE = f'git+https://github.com/muidea/modorch-rs?rev={REV}#{REV}'


def metadata():
    host = subprocess.check_output(['rustc', '-vV'], text=True)
    target = os.environ.get('CARGO_BUILD_TARGET') or next(line[6:] for line in host.splitlines() if line.startswith('host: '))
    return json.loads(subprocess.check_output([
        'cargo', 'metadata', '--format-version=1', '--locked', '--offline',
        '--filter-platform', target, '--manifest-path', str(ROOT / 'Cargo.toml')], text=True))


def verify(graph):
    packages = {p['id']: p for p in graph['packages']}
    nodes = {n['id']: n for n in graph['resolve']['nodes']}
    frameworks = [p for p in packages.values() if p['name'] == 'modorch']
    if len(frameworks) != 1 or frameworks[0]['version'] != '0.0.3' or frameworks[0]['source'] != SOURCE:
        raise ValueError('expected unique Modorch 0.0.3 Git identity')
    owned = [p['name'] for p in packages.values() if p['name'] == 'modorch' or p['name'].startswith('modorch-')]
    if len(owned) != len(set(owned)):
        raise ValueError('duplicate orchestration or business package identity')
    for p in packages.values():
        if p['name'].startswith('modorch-') and not p['name'].startswith(('modorch-mqtt', 'modorch-fedengine')):
            if p['version'] != '0.0.3' or p['source'] != SOURCE:
                raise ValueError('mixed framework package sources')

    def closure(name):
        ids = [p['id'] for p in packages.values() if p['name'] == name]
        if len(ids) != 1:
            raise ValueError('missing or duplicated host package: ' + name)
        pending, seen = ids[:], set()
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            for dep in nodes[key]['deps']:
                if any(k['kind'] in (None, 'build') for k in dep['dep_kinds']):
                    pending.append(dep['pkg'])
        return {packages[key]['name'] for key in seen}

    wanted = {
        'mqtt-server': (True, False), 'fedengine-server': (False, True),
        'datahub-server': (True, True), 'datahub-app': (False, False),
        'datahub-config': (False, False), 'datahub-mqtt': (True, False),
        'datahub-fedengine': (False, True),
    }
    closures = {}
    for name, expected in wanted.items():
        names = closure(name)
        actual = ('modorch-mqtt' in names, 'modorch-fedengine' in names)
        if actual != expected or any('test-host' in n for n in names):
            raise ValueError('business dependency isolation failed: ' + name)
        if not expected[1] and any('duckdb' in n for n in names):
            raise ValueError('unexpected database dependency: ' + name)
        closures[name] = len(names)
    return closures


def identity(graph):
    sources = {}
    nodes = {n['id']: n for n in graph['resolve']['nodes']}
    for p in graph['packages']:
        if p['name'] == 'modorch' or p['name'].startswith('modorch-'):
            base = Path(p['manifest_path']).parent
            files = [base / 'Cargo.toml', *sorted((base / 'src').rglob('*.rs'))]
            if p['name'] == 'modorch-fedengine':
                files.append(base / 'README.md')
            digest = hashlib.sha256()
            for path in sorted(files):
                if path.is_symlink() or not path.is_file():
                    raise ValueError('missing or symlinked source: ' + p['name'])
                digest.update(path.relative_to(base).as_posix().encode() + b'\0')
                digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode() + b'\n')
            sources[p['name']] = {'version': p['version'], 'source': p['source'],
                                  'sha256': digest.hexdigest(), 'features': sorted(nodes[p['id']]['features'])}
    return {'lock_sha256': hashlib.sha256((ROOT / 'Cargo.lock').read_bytes()).hexdigest(),
            'packages': dict(sorted(sources.items()))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-identity', action='store_true', help='refresh only after revalidating source changes')
    args = parser.parse_args()
    graph = metadata()
    closures = verify(graph)
    actual = identity(graph)
    path = ROOT / 'scripts/source-identity.json'
    if args.write_identity:
        path.write_text(json.dumps(actual, indent=2) + '\n')
    elif json.loads(path.read_text()) != actual:
        raise ValueError('source or lock fingerprint changed; revalidate before refreshing identity')
    print(json.dumps({'status': 'Passed', 'framework_version': '0.0.3',
                      'normal_build_closure_packages': closures,
                      'local_business_sources': True, 'remote_reproducibility': 'NotRun'}, indent=2))
    print('test MDH-IDENTITY-001 ... ok')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print('dependency identity failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
