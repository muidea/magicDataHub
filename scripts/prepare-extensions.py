#!/usr/bin/env python3
"""Prepare, fetch (optional pinned GitHub Releases), and verify custom DuckDB extensions."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import posixpath
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from acceptance.fixtures import LOCK, extension_manifest, validate_extensions

MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 192 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120
BANNED_EXTENSION_HOSTS = (
    'extensions.duckdb.org',
    'community-extensions.duckdb.org',
    'duckdb.org',
)
GITHUB_TOKEN_HOSTS = ('github.com', 'api.github.com')


def validate_release_url(url: str) -> str:
    """Enforce HTTPS URLs and hard-ban official DuckDB extension repositories."""
    if not isinstance(url, str) or not url.strip():
        raise SystemExit('Release URL must be a non-empty HTTPS URL')
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not host:
        raise SystemExit(f'Unsupported or non-HTTPS extension release URL: {url}')
    for banned in BANNED_EXTENSION_HOSTS:
        if host == banned or host.endswith('.' + banned):
            raise SystemExit(
                f'Official DuckDB extension repository host is forbidden: {url}; '
                'only pinned custom extension releases are allowed'
            )
    return parsed.geturl()


def decompress_and_verify(
    raw_bytes: bytes,
    is_gzip: bool,
    expected_sha256: str,
    max_decompressed: int = MAX_DECOMPRESSED_BYTES,
    max_download: int = MAX_DOWNLOAD_BYTES,
) -> bytes:
    """Bound compressed/uncompressed sizes and verify SHA-256 of the extension binary."""
    if len(raw_bytes) > max_download:
        raise SystemExit(
            f'Extension download exceeds maximum allowed size ({len(raw_bytes)} > {max_download} bytes)'
        )
    if is_gzip:
        out = io.BytesIO()
        total = 0
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw_bytes), mode='rb') as gz:
                while True:
                    chunk = gz.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_decompressed:
                        raise SystemExit(
                            f'Decompressed extension exceeds maximum allowed size ({max_decompressed} bytes)'
                        )
                    out.write(chunk)
        except (OSError, EOFError, ValueError) as exc:
            raise SystemExit(f'Failed to decompress gzipped extension payload: {exc}') from exc
        data = out.getvalue()
    else:
        if len(raw_bytes) > max_decompressed:
            raise SystemExit(
                f'Extension payload exceeds maximum allowed size ({len(raw_bytes)} > {max_decompressed} bytes)'
            )
        data = raw_bytes

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        raise SystemExit(
            f'Checksum mismatch for extension payload: expected {expected_sha256}, got {actual_sha256}'
        )
    return data


class _SameHostAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strip Authorization headers when redirecting across hosts (e.g. GitHub -> S3 CDN)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_newurl = validate_release_url(urllib.parse.urljoin(req.full_url, newurl))
        next_req = super().redirect_request(req, fp, code, msg, headers, safe_newurl)
        if next_req is None:
            return None
        old_host = (urllib.parse.urlsplit(req.full_url).hostname or '').lower()
        new_host = (urllib.parse.urlsplit(safe_newurl).hostname or '').lower()
        if new_host != old_host:
            for key in {k for k in (*next_req.headers, *next_req.unredirected_hdrs) if k.lower() == 'authorization'}:
                next_req.remove_header(key)
        return next_req


def _parse_github_release_asset_url(url: str) -> tuple[str, str, str, str] | None:
    """Parse https://github.com/<owner>/<repo>/releases/download/<tag>/<asset> into components."""
    parsed = urllib.parse.urlsplit(url)
    if (parsed.hostname or '').lower() != 'github.com':
        return None
    parts = [part for part in parsed.path.split('/') if part]
    if len(parts) < 6 or parts[2] != 'releases' or parts[3] != 'download':
        return None
    owner, repo, tag = parts[0], parts[1], urllib.parse.unquote(parts[4])
    asset_name = urllib.parse.unquote('/'.join(parts[5:]))
    if not all((owner, repo, tag, asset_name)):
        return None
    return owner, repo, tag, asset_name


def _read_bounded_response(response, max_bytes: int, source_label: str) -> bytes:
    final_url = response.geturl()
    if final_url:
        validate_release_url(final_url)
    content_length = response.headers.get('Content-Length')
    if content_length is not None and content_length.isdigit() and int(content_length) > max_bytes:
        raise SystemExit(
            f'Remote extension {source_label} Content-Length ({content_length}) exceeds limit ({max_bytes})'
        )
    buf = io.BytesIO()
    total = 0
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise SystemExit(f'Remote extension {source_label} download exceeds limit ({max_bytes} bytes)')
        buf.write(chunk)
    return buf.getvalue()


def _download_github_private_asset(
    owner: str,
    repo: str,
    tag: str,
    asset_name: str,
    token: str,
    max_bytes: int,
    timeout: int,
) -> bytes:
    """Download a GitHub Release asset via REST API (supports private extension repositories)."""
    if not re.fullmatch(r'[A-Za-z0-9._-]+', owner) or not re.fullmatch(r'[A-Za-z0-9._-]+', repo):
        raise SystemExit(f'Invalid GitHub repository slug: {owner}/{repo}')
    api_url = (
        f'https://api.github.com/repos/{owner}/{repo}/releases/tags/'
        f'{urllib.parse.quote(tag, safe="")}'
    )
    opener = urllib.request.build_opener(_SameHostAuthRedirectHandler())
    meta_req = urllib.request.Request(
        api_url,
        headers={
            'User-Agent': 'magicDataHub-prepare-extensions',
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {token}',
        },
    )
    try:
        with opener.open(meta_req, timeout=timeout) as resp:
            release_meta = json.loads(resp.read(4 * 1024 * 1024).decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f'Failed to query GitHub Release API {api_url}: HTTP {exc.code}') from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f'Failed to query GitHub Release API {api_url}: {exc.reason}') from exc

    asset = next((item for item in release_meta.get('assets', []) if item.get('name') == asset_name), None)
    if asset is None or not asset.get('url'):
        raise SystemExit(f'Release {owner}/{repo}@{tag} does not contain asset {asset_name!r}')

    asset_url = validate_release_url(asset['url'])
    asset_req = urllib.request.Request(
        asset_url,
        headers={
            'User-Agent': 'magicDataHub-prepare-extensions',
            'Accept': 'application/octet-stream',
            'Authorization': f'Bearer {token}',
        },
    )
    try:
        with opener.open(asset_req, timeout=timeout) as resp:
            return _read_bounded_response(resp, max_bytes, asset_url)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f'Failed to download private release asset {asset_url}: HTTP {exc.code}') from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f'Failed to download private release asset {asset_url}: {exc.reason}') from exc


def download_release(url: str, max_bytes: int = MAX_DOWNLOAD_BYTES, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    """Download a pinned custom extension release archive with bounded size."""
    safe_url = validate_release_url(url)
    host = (urllib.parse.urlsplit(safe_url).hostname or '').lower()
    mdh_token = os.environ.get('MDH_EXTENSIONS_TOKEN', '').strip()
    token = mdh_token or os.environ.get('GITHUB_TOKEN', '').strip()
    gh_parts = _parse_github_release_asset_url(safe_url)
    if gh_parts is not None and mdh_token:
        return _download_github_private_asset(*gh_parts, mdh_token, max_bytes, timeout)

    headers = {
        'User-Agent': 'magicDataHub-prepare-extensions',
        'Accept': 'application/octet-stream',
    }
    if token and host in GITHUB_TOKEN_HOSTS:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(safe_url, headers=headers)
    opener = urllib.request.build_opener(_SameHostAuthRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            return _read_bounded_response(response, max_bytes, safe_url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and gh_parts is not None and token:
            return _download_github_private_asset(*gh_parts, token, max_bytes, timeout)
        raise SystemExit(f'Failed to download custom extension release {safe_url}: HTTP {exc.code}') from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f'Failed to download custom extension release {safe_url}: {exc.reason}') from exc


def atomic_write_bytes(dest: Path, data: bytes, mode: int = 0o644) -> None:
    if dest.is_symlink():
        raise SystemExit(f'Symlinked extension destination is refused: {dest}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as staged:
        staged_path = Path(staged.name)
        try:
            staged.write(data)
            staged.flush()
            staged_path.chmod(mode)
            staged_path.replace(dest)
        finally:
            staged_path.unlink(missing_ok=True)


def stage_file(source: Path, dest: Path, expected_sha256: str) -> None:
    if source.is_symlink() or not source.is_file():
        raise SystemExit(f'Custom extension source must be a regular non-symlink file: {source}')
    data = decompress_and_verify(
        source.read_bytes(),
        is_gzip=source.name.endswith('.gz'),
        expected_sha256=expected_sha256,
    )
    atomic_write_bytes(dest, data)


def resolve_release_url(entry: dict, base_url: str | None) -> str:
    raw = entry.get('release_url')
    if not raw:
        raise SystemExit(f"Missing release_url in sources.json for {entry.get('path')}")
    validated = validate_release_url(raw)
    if base_url:
        base = validate_release_url(base_url).rstrip('/')
        filename = posixpath.basename(urllib.parse.urlsplit(validated).path)
        if not filename:
            raise SystemExit(f'Cannot derive release filename from {validated}')
        return validate_release_url(f'{base}/{filename}')
    return validated


def parse_selected_keys(only_arg: str | None) -> list[str]:
    all_keys = list(LOCK['extensions'].keys())
    if not only_arg:
        return all_keys
    selected = []
    for raw in only_arg.split(','):
        key = raw.strip()
        if not key:
            continue
        if key not in LOCK['extensions']:
            raise SystemExit(f'Unknown extension key {key!r}; valid keys: {all_keys}')
        if key not in selected:
            selected.append(key)
    if not selected:
        raise SystemExit('--only must specify at least one valid extension key')
    return selected


def prepare_extensions(
    dest: Path,
    source_dir: Path | None = None,
    fetch_releases: bool = False,
    extensions_base_url: str | None = None,
    only_keys: list[str] | None = None,
) -> Path:
    assert platform.system() == 'Linux' and platform.machine() == 'x86_64', 'only Linux amd64 is pinned'
    selected_keys = list(only_keys) if only_keys is not None else list(LOCK['extensions'].keys())
    root = dest.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source_root = source_dir.resolve() if source_dir is not None else None
    if source_root is not None and not source_root.is_dir():
        raise SystemExit(f'Custom extension source directory does not exist: {source_root}')
    if source_root is not None and fetch_releases:
        raise SystemExit('Specify either --source-dir or --fetch-releases, not both')

    for key in selected_keys:
        entry = LOCK['extensions'][key]
        path = root / entry['path']
        assert not path.is_symlink(), 'symlinked extension is refused'
        path.parent.mkdir(parents=True, exist_ok=True)
        assert path.parent.resolve().is_relative_to(root), 'extension path leaves destination'
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256']:
            continue
        if fetch_releases:
            url = resolve_release_url(entry, extensions_base_url)
            raw_bytes = download_release(url)
            data = decompress_and_verify(
                raw_bytes,
                is_gzip=urllib.parse.urlsplit(url).path.endswith('.gz'),
                expected_sha256=entry['sha256'],
            )
            atomic_write_bytes(path, data)
            continue
        if source_root is None:
            raise SystemExit(
                f"Missing locally compiled custom extension {path} (expected sha256 {entry['sha256']}); "
                'supply --source-dir, MDH_EXTENSIONS_SOURCE, or --fetch-releases '
                '(official extensions from extensions.duckdb.org are never downloaded)'
            )
        filename = Path(entry['path']).name
        candidates = [
            source_root / entry['path'],
            source_root / filename,
            source_root / f'{entry["path"]}.gz',
            source_root / f'{filename}.gz',
        ]
        found = next((p for p in candidates if p.exists() or p.is_symlink()), None)
        if found is None:
            raise SystemExit(
                f"Missing locally compiled custom extension {entry['path']} under {source_root}; "
                'official extensions from extensions.duckdb.org are never downloaded'
            )
        stage_file(found, path, entry['sha256'])

    manifest = root / 'manifest.json'
    assert not manifest.is_symlink(), 'symlinked manifest is refused'
    manifest.write_text(json.dumps(extension_manifest(selected_keys), indent=2) + '\n')
    return validate_extensions(root, selected_keys)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest', type=Path, required=True)
    parser.add_argument(
        '--source-dir',
        type=Path,
        default=Path(os.environ['MDH_EXTENSIONS_SOURCE']) if os.environ.get('MDH_EXTENSIONS_SOURCE') else None,
        help='Directory containing locally compiled custom DuckDB 2.0.0 extensions matching sources.json',
    )
    parser.add_argument(
        '--fetch-releases',
        action='store_true',
        help='Download pinned custom extension release assets defined in scripts/acceptance/sources.json',
    )
    parser.add_argument(
        '--extensions-base-url',
        help='Optional HTTPS base URL overriding download host/path for --fetch-releases',
    )
    parser.add_argument(
        '--only',
        help='Optional comma-separated extension subset (e.g. postgres,mysql or oracle)',
    )
    args = parser.parse_args()
    selected = parse_selected_keys(args.only)
    prepare_extensions(
        dest=args.dest,
        source_dir=args.source_dir,
        fetch_releases=args.fetch_releases,
        extensions_base_url=args.extensions_base_url,
        only_keys=selected,
    )
    print(f"Pinned custom DuckDB extensions prepared and verified: {','.join(selected)}")


if __name__ == '__main__':
    main()
