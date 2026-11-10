#!/usr/bin/env python3
"""Prepare only the checksum-pinned Linux amd64 DuckDB extensions, never install at startup."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tempfile

from acceptance.fixtures import LOCK, extension_manifest, validate_extensions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest', type=Path, required=True)
    args = parser.parse_args()
    assert platform.system() == 'Linux' and platform.machine() == 'x86_64', 'only Linux amd64 is pinned'
    root = args.dest.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for entry in LOCK['extensions'].values():
        path = root / entry['path']
        assert not path.is_symlink(), 'symlinked extension is refused'
        path.parent.mkdir(parents=True, exist_ok=True)
        assert path.parent.resolve().is_relative_to(root), 'extension path leaves destination'
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256']:
            continue
        url = 'https://extensions.duckdb.org/' + entry['path'] + '.gz'
        # Both compressed and expanded responses are bounded before publication.
        with tempfile.TemporaryFile() as compressed:
            subprocess.run(['curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https',
                            '--silent', '--show-error', '--retry', '2', '--max-time', '60',
                            '--max-filesize', str(128 * 1024 * 1024), url],
                           stdout=compressed, check=True, timeout=200)
            assert compressed.tell() <= 128 * 1024 * 1024, 'compressed extension too large'
            compressed.seek(0)
            with gzip.GzipFile(fileobj=compressed) as stream:
                data = stream.read(256 * 1024 * 1024 + 1)
        assert len(data) <= 256 * 1024 * 1024, 'expanded extension too large'
        assert hashlib.sha256(data).hexdigest() == entry['sha256'], 'download checksum mismatch'
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
            staged_path = Path(staged.name)
            try:
                staged.write(data)
                staged.flush()
                staged_path.chmod(0o644)
                staged_path.replace(path)
            finally:
                staged_path.unlink(missing_ok=True)
    manifest = root / 'manifest.json'
    assert not manifest.is_symlink(), 'symlinked manifest is refused'
    manifest.write_text(json.dumps(extension_manifest(), indent=2) + '\n')
    validate_extensions(root)
    print('Pinned DuckDB extensions prepared and verified')


if __name__ == '__main__':
    main()
