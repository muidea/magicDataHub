#!/usr/bin/env python3
"""Update pinned custom DuckDB extension release URL, version, and SHA-256 in scripts/acceptance/sources.json."""
import argparse
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / 'scripts/acceptance/sources.json'

_PREP_SPEC = importlib.util.spec_from_file_location('prepare_extensions', ROOT / 'scripts/prepare-extensions.py')
_PREP = importlib.util.module_from_spec(_PREP_SPEC)
_PREP_SPEC.loader.exec_module(_PREP)


def logical_scanner_filename(ext_key: str) -> str:
    return f'{ext_key}_scanner.duckdb_extension'


def parse_duckdb_extension_footer(data: bytes) -> dict[str, str]:
    """Parse and validate DuckDB v2.0.0 extension metadata from its 512-byte trailer."""
    if len(data) < 512:
        raise SystemExit(f'Extension binary is shorter than 512-byte DuckDB metadata footer ({len(data)} bytes)')
    trailer = data[-512:]
    fields = []
    for idx in range(8):
        slot = trailer[idx * 32:(idx + 1) * 32].rstrip(b'\x00')
        try:
            fields.append(slot.decode('utf-8', errors='strict'))
        except UnicodeDecodeError as exc:
            raise SystemExit(f'Invalid UTF-8 in DuckDB extension footer slot {idx}: {exc}') from exc

    abi, extension_version, duckdb_version, platform_name, format_version = fields[3:8]
    if format_version != '4' or duckdb_version != 'v2.0.0' or platform_name != 'linux_amd64':
        raise SystemExit(
            f'Unexpected DuckDB extension footer: abi={abi!r}, version={extension_version!r}, '
            f'duckdb={duckdb_version!r}, platform={platform_name!r}, format={format_version!r}'
        )
    if not extension_version:
        raise SystemExit('DuckDB extension footer is missing extension_version')
    return {
        'abi': abi,
        'extension_version': extension_version,
        'duckdb_version': duckdb_version,
        'platform': platform_name,
        'format_version': format_version,
    }


def inspect_release(url: str) -> tuple[str, dict[str, str]]:
    """Download a pinned extension release and return (uncompressed_sha256, parsed_footer)."""
    raw = _PREP.download_release(url)
    if urllib.parse.urlsplit(url).path.endswith('.gz'):
        out = io.BytesIO()
        total = 0
        with gzip.GzipFile(fileobj=io.BytesIO(raw), mode='rb') as gz:
            while True:
                chunk = gz.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _PREP.MAX_DECOMPRESSED_BYTES:
                    raise SystemExit(
                        f'Decompressed extension exceeds limit ({_PREP.MAX_DECOMPRESSED_BYTES} bytes)'
                    )
                out.write(chunk)
        data = out.getvalue()
    else:
        data = raw
    footer = parse_duckdb_extension_footer(data)
    return hashlib.sha256(data).hexdigest(), footer


def compute_sha256_from_release(url: str) -> str:
    sha256, _ = inspect_release(url)
    return sha256


def atomic_write_json(path: Path, payload: dict) -> None:
    text = json.dumps(payload, indent=2) + '\n'
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as staged:
        staged_path = Path(staged.name)
        try:
            staged.write(text)
            staged.flush()
            staged_path.chmod(0o644)
            staged_path.replace(path)
        finally:
            staged_path.unlink(missing_ok=True)


def main() -> None:
    sources = json.loads(SOURCES_PATH.read_text(encoding='utf-8'))
    valid_keys = list(sources['extensions'].keys())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ext', required=True, choices=valid_keys, help='Extension key in sources.json')
    parser.add_argument('--tag', required=True, help='Release tag (e.g. v2.0.0-abc1234567)')
    parser.add_argument('--url', help='Explicit HTTPS release asset URL override')
    parser.add_argument('--version', help='Short source revision stored in sources.json (default inferred from tag)')
    parser.add_argument('--sha256', help='Uncompressed extension SHA-256 (computed from --url if omitted)')
    args = parser.parse_args()

    entry = sources['extensions'][args.ext]
    repo = entry.get('repo')
    if not repo:
        raise SystemExit(f'Missing repo field for {args.ext} in {SOURCES_PATH}')
    duckdb_version = sources['duckdb_version']
    asset_name = f'{args.ext}_scanner-{duckdb_version}-linux_amd64.duckdb_extension.gz'
    release_url = args.url or f'https://github.com/{repo}/releases/download/{args.tag}/{asset_name}'
    release_url = _PREP.validate_release_url(release_url)

    if args.sha256:
        sha256 = args.sha256.lower()
        if not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise SystemExit(f'Invalid --sha256 hex digest: {args.sha256}')
        version = args.version or args.tag
    else:
        sha256, footer = inspect_release(release_url)
        version = args.version or footer['extension_version']

    before = dict(entry)
    entry['sha256'] = sha256
    entry['version'] = version
    entry['release_url'] = release_url
    atomic_write_json(SOURCES_PATH, sources)

    print(json.dumps({
        'extension': args.ext,
        'before': before,
        'after': entry,
    }, indent=2))


if __name__ == '__main__':
    sys.exit(main())
