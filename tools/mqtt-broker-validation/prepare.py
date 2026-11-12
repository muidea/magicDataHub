#!/usr/bin/env python3
"""Prepare hash-locked upstream and Paho in an isolated, reusable cache."""
import argparse
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import urllib.request
import zipfile

from common import InputError, ROOT, check_upstream, command, digest, read_json, write_json


def wheel_members(body):
    archive = zipfile.ZipFile(io.BytesIO(body))
    for info in archive.infolist():
        path = Path(info.filename)
        if path.is_absolute() or '..' in path.parts or info.file_size > 8 * 1024 * 1024:
            archive.close()
            raise InputError('unsafe wheel member')
    return archive


def prepare(cache, *, offline=False, pull_reference=False):
    lock = read_json(ROOT / 'upstream.lock.json')
    cache.mkdir(parents=True, exist_ok=True)
    upstream = cache / ('mosquitto-' + lock['mosquitto']['tag'].removeprefix('v'))
    if not upstream.exists():
        if offline:
            raise InputError('offline cache has no upstream checkout')
        command('git', 'clone', '--depth', '1', '--branch', lock['mosquitto']['tag'],
                lock['mosquitto']['repository'], str(upstream), timeout=120)
    check_upstream(upstream, lock)
    artifact = cache / lock['paho']['filename']
    if not artifact.exists():
        if offline:
            raise InputError('offline cache has no locked Paho wheel')
        with urllib.request.urlopen(lock['paho']['url'], timeout=30) as response:
            body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024 or hashlib.sha256(body).hexdigest() != lock['paho']['sha256']:
            raise InputError('Paho download digest mismatch')
        artifact.write_bytes(body)
    body = artifact.read_bytes()
    if hashlib.sha256(body).hexdigest() != lock['paho']['sha256']:
        raise InputError('cached Paho wheel digest mismatch; cache not overwritten')
    environment = cache / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(environment)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    python = environment / 'bin/python'
    site = Path(command(str(python), '-c', 'import sysconfig; print(sysconfig.get_paths()["purelib"])').strip())
    with wheel_members(body) as archive:
        files = {info.filename: hashlib.sha256(archive.read(info)).hexdigest()
                 for info in archive.infolist() if not info.is_dir()}
        for relative, expected in files.items():
            installed = site / relative
            if installed.exists() and digest(installed) != expected:
                raise InputError('installed Paho differs from lock; existing files preserved')
        for relative in files:
            installed = site / relative
            if not installed.exists():
                installed.parent.mkdir(parents=True, exist_ok=True)
                installed.write_bytes(archive.read(relative))
    command(str(python), '-c', 'import paho.mqtt.client; import importlib.metadata; '
            'assert importlib.metadata.version("paho-mqtt") == "2.1.0"')
    manifest = dict(schema_version=1, lock_sha256=digest(ROOT / 'upstream.lock.json'),
                    upstream=str(upstream.resolve()), python=str(python.resolve()),
                    python_launcher=str(python.absolute()), site_packages=str(site), paho_files=files)
    write_json(cache / 'dependencies.json', manifest)
    if pull_reference:
        command('docker', 'pull', lock['reference_image'], timeout=180)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', type=Path, default=Path.home() / '.cache/mqtt-broker-validation')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--pull-reference', action='store_true')
    args = parser.parse_args()
    try:
        manifest = prepare(args.cache_dir, offline=args.offline, pull_reference=args.pull_reference)
    except InputError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        print('dependency preparation failed: ' + type(error).__name__, file=sys.stderr)
        return 3
    print('Dependencies verified. Python: ' + manifest['python_launcher'])
    print('Upstream: ' + manifest['upstream'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
