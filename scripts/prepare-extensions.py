#!/usr/bin/env python3
"""Verify or stage locally compiled SHA256-pinned Linux amd64 custom DuckDB extensions (never download)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile

from acceptance.fixtures import LOCK, extension_manifest, validate_extensions


def stage_file(source: Path, dest: Path, expected_sha256: str) -> None:
    if source.is_symlink() or not source.is_file():
        raise SystemExit(f'Custom extension source must be a regular non-symlink file: {source}')
    data = source.read_bytes()
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        raise SystemExit(
            f'Checksum mismatch for locally compiled extension {source}: '
            f'expected {expected_sha256}, got {actual_sha256}'
        )
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as staged:
        staged_path = Path(staged.name)
        try:
            staged.write(data)
            staged.flush()
            staged_path.chmod(0o644)
            staged_path.replace(dest)
        finally:
            staged_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest', type=Path, required=True)
    parser.add_argument(
        '--source-dir',
        type=Path,
        default=Path(os.environ['MDH_EXTENSIONS_SOURCE']) if os.environ.get('MDH_EXTENSIONS_SOURCE') else None,
        help='Directory containing locally compiled custom DuckDB 2.0.0 extensions matching sources.json',
    )
    args = parser.parse_args()
    assert platform.system() == 'Linux' and platform.machine() == 'x86_64', 'only Linux amd64 is pinned'
    root = args.dest.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source_root = args.source_dir.resolve() if args.source_dir is not None else None
    if source_root is not None and not source_root.is_dir():
        raise SystemExit(f'Custom extension source directory does not exist: {source_root}')

    for entry in LOCK['extensions'].values():
        path = root / entry['path']
        assert not path.is_symlink(), 'symlinked extension is refused'
        path.parent.mkdir(parents=True, exist_ok=True)
        assert path.parent.resolve().is_relative_to(root), 'extension path leaves destination'
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256']:
            continue
        if source_root is None:
            raise SystemExit(
                f"Missing locally compiled custom extension {path} (expected sha256 {entry['sha256']}); "
                'supply --source-dir or MDH_EXTENSIONS_SOURCE (official extensions from extensions.duckdb.org are never downloaded)'
            )
        candidates = [source_root / entry['path'], source_root / Path(entry['path']).name]
        found = next((p for p in candidates if p.exists() or p.is_symlink()), None)
        if found is None:
            raise SystemExit(
                f"Missing locally compiled custom extension {entry['path']} under {source_root}; "
                'official extensions from extensions.duckdb.org are never downloaded'
            )
        stage_file(found, path, entry['sha256'])

    manifest = root / 'manifest.json'
    assert not manifest.is_symlink(), 'symlinked manifest is refused'
    manifest.write_text(json.dumps(extension_manifest(), indent=2) + '\n')
    validate_extensions(root)
    print('Pinned custom DuckDB extensions prepared and verified')


if __name__ == '__main__':
    main()
