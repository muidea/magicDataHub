#!/usr/bin/env python3
"""Build local service artifact images with explicit current-source provenance; never push."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile

from acceptance.fixtures import LOCK, extension_manifest, validate_extensions

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = json.loads((ROOT / 'docker/runtime.lock.json').read_text())
SERVICES = {'mqtt-server': 'mqtt-development.toml', 'fedengine-server': 'fedengine-development.toml', 'datahub-server': 'combined-development.toml'}


def output(arguments, **kwargs):
    return subprocess.check_output(arguments, text=True, **kwargs).strip()


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def host_identity():
    files = [ROOT / name for name in ['Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml']]
    files += [p for folder in ['apps', 'crates'] for p in (ROOT / folder).rglob('*') if p.is_file() and p.suffix in ('.rs', '.toml')]
    records = {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(files)}
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def git_state(directory):
    return {'head': output(['git', '-C', str(directory), 'rev-parse', 'HEAD']),
            'dirty': bool(output(['git', '-C', str(directory), 'status', '--porcelain', '--untracked-files=all']))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag-prefix', required=True, help='local image prefix, e.g. mdh-local')
    parser.add_argument('--profile', choices=['dev', 'release'], default='dev')
    parser.add_argument('--service', choices=SERVICES)
    parser.add_argument('--extensions-dir', type=Path, required=True)
    parser.add_argument('--duckdb-library-dir', type=Path, required=True)
    parser.add_argument('--require-clean', action='store_true', help='reject uncommitted host/business inputs before building')
    args = parser.parse_args()
    assert re.fullmatch(r'[a-z0-9]+(?:[._/-][a-z0-9]+)*', args.tag_prefix), 'invalid local image prefix'
    assert platform.system() == 'Linux' and platform.machine() == 'x86_64', 'only verified Linux amd64 artifacts are supported'
    assert 'ARG BASE_IMAGE=' + RUNTIME['base_image'] + '\n' in (ROOT / 'docker/Dockerfile').read_text(), 'Dockerfile base differs from runtime lock'
    subprocess.run(['python3', str(ROOT / 'scripts/verify-dependency-identity.py')], cwd=ROOT, check=True)
    extensions = validate_extensions(args.extensions_dir)
    native = args.duckdb_library_dir.resolve() / 'libduckdb.so'
    assert sha(native) == RUNTIME['libduckdb_sha256'], 'native DuckDB checksum mismatch'
    metadata = json.loads(output(['cargo', 'metadata', '--locked', '--offline', '--no-deps', '--format-version=1'], cwd=ROOT))
    target = Path(metadata['target_directory'])
    if os.environ.get('CARGO_BUILD_TARGET'):
        assert os.environ['CARGO_BUILD_TARGET'] == 'x86_64-unknown-linux-gnu', 'unsupported artifact target'
        target /= os.environ['CARGO_BUILD_TARGET']
    states = {name: git_state(ROOT if name == 'magicDataHub' else ROOT.parent / name) for name in ['magicDataHub', 'modorch-mqtt', 'modorch-fedengine']}
    clean = not any(s['dirty'] for s in states.values())
    if args.require_clean:
        assert clean, 'clean Git inputs are required; current local source must be committed and revalidated first'
    source_before = host_identity()
    identity = json.loads((ROOT / 'scripts/source-identity.json').read_text())
    for service in ([args.service] if args.service else SERVICES):
        command = ['cargo', 'build', '-p', service, '--locked', '--offline']
        if args.profile == 'release':
            command.append('--release')
        subprocess.run(command, cwd=ROOT, check=True)
        assert host_identity() == source_before, 'host source changed during artifact build'
        subprocess.run(['python3', str(ROOT / 'scripts/verify-dependency-identity.py')], cwd=ROOT, check=True)
        binary = target / ('debug' if args.profile == 'dev' else 'release') / service
        with tempfile.TemporaryDirectory(prefix='mdh-image-context-') as temporary:
            context = Path(temporary)
            app = context / 'app'
            for directory in ['bin', 'lib', 'config', 'var']:
                (app / directory).mkdir(parents=True)
            subprocess.run(['strip', '--strip-debug', '-o', str(app / 'bin/service'), str(binary)], check=True)
            os.chmod(app / 'bin/service', 0o755)
            profile = SERVICES[service]
            shutil.copyfile(ROOT / 'config' / profile, app / 'config/host.toml')
            if service != 'fedengine-server':
                shutil.copytree(ROOT / 'config/mqtt', app / 'config/mqtt')
            if service != 'mqtt-server':
                shutil.copytree(ROOT / 'config/fedengine', app / 'config/fedengine')
                shutil.copyfile(native, app / 'lib/libduckdb.so')
                for entry in LOCK['extensions'].values():
                    destination = app / 'extensions' / entry['path']
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(extensions / entry['path'], destination)
                (app / 'extensions/manifest.json').write_text(json.dumps(extension_manifest(), indent=2) + '\n')
                for config in (app / 'config/fedengine').glob('*.toml'):
                    config.write_text(config.read_text().replace('install_extensions = false', 'install_extensions = false\nextension_directory = "/opt/magicdatahub/extensions"'))
            provenance = {'service': service, 'framework_version': '0.0.3', 'profile': args.profile,
                          'base_image': RUNTIME['base_image'], 'platform': RUNTIME['platform'],
                          'git_inputs': states, 'clean_git_inputs': clean, 'remote_ci_verified': False,
                          'host_source_sha256': source_before, 'library_identity': identity,
                          'binary_sha256': sha(app / 'bin/service'),
                          'native_files': {p.relative_to(app).as_posix(): sha(p) for folder in ['lib', 'extensions'] if (app / folder).exists() for p in (app / folder).rglob('*') if p.is_file()}}
            (app / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
            tag = args.tag_prefix + '/' + service + ':' + args.profile
            subprocess.run(['docker', 'build', '--platform', RUNTIME['platform'], '--file', str(ROOT / 'docker/Dockerfile'),
                            '--build-arg', 'BASE_IMAGE=' + RUNTIME['base_image'], '--tag', tag,
                            '--label', 'org.opencontainers.image.source=https://github.com/muidea/magicDataHub',
                            '--label', 'org.opencontainers.image.version=0.1.0-development', str(context)], check=True)
            print(json.dumps({'image': tag, 'status': 'Built', 'profile': args.profile,
                              'clean_git_inputs': clean, 'remote_reproducibility': 'NotRun'}), flush=True)


if __name__ == '__main__':
    main()
