#!/usr/bin/env python3
"""Stage commercial vendor ODBC driver trees from URL overrides, pinned runtime base image, or local tarball."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = json.loads((ROOT / 'docker/runtime.lock.json').read_text(encoding='utf-8'))
VENDOR_TARBALL_DEFAULT = ROOT / 'data/runtime-base/odbc-vendor-drivers-x86_64.tar.gz'
VENDOR_TARBALL_SHA256 = 'abf1e81006848298d820f54b8a8478988fd59d974b77bc7adb10d4d98d3e4a5b'
MAX_DRIVER_DOWNLOAD_BYTES = 512 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 180

VENDOR_SPECS = {
    'oracle': {
        'image_path': '/usr/lib/oracle/23/client64/lib',
        'tar_prefix': 'usr/lib/oracle/23/client64/lib',
        'dest_rel': '',
        'required_files': ('libsqora.so.23.1',),
        'env_url': 'MDH_ORACLE_ODBC_URL',
        'env_sha': 'MDH_ORACLE_ODBC_SHA256',
    },
    'db2': {
        'image_path': '/opt/ibm/db2/clidriver',
        'tar_prefix': 'opt/ibm/db2/clidriver',
        'dest_rel': 'clidriver',
        'required_files': ('clidriver/lib/libdb2.so',),
        'env_url': 'MDH_DB2_ODBC_URL',
        'env_sha': 'MDH_DB2_ODBC_SHA256',
    },
    'hana': {
        'image_path': '/opt/sap/hdbclient',
        'tar_prefix': 'opt/sap/hdbclient',
        'dest_rel': '',
        'required_files': ('libodbcHDB.so',),
        'env_url': 'MDH_HANA_ODBC_URL',
        'env_sha': 'MDH_HANA_ODBC_SHA256',
    },
    'dameng': {
        'image_path': '/opt/dameng',
        'tar_prefix': 'opt/dameng',
        'dest_rel': '',
        'required_files': (
            'libdodbc.so',
            'libdmdpi.so',
            'libdmfldr.so',
            'libcrypto.so.3',
            'libssl.so.3',
        ),
        'env_url': 'MDH_DAMENG_ODBC_URL',
        'env_sha': 'MDH_DAMENG_ODBC_SHA256',
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_vendor_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError('Vendor ODBC driver URL must be a non-empty HTTPS URL')
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or not host:
        raise ValueError(f'Vendor ODBC driver URL must use https://: {url}')
    return parsed.geturl()


def _validate_relative_member(rel_path: str) -> Path:
    parts = Path(rel_path).parts
    if not parts or any(part in ('', '.', '..', '/') for part in parts):
        raise ValueError(f'Unsafe archive entry path: {rel_path}')
    return Path(rel_path)


def _validate_symlink_target(dest_resolved: Path, target: Path, linkname: str) -> None:
    if not linkname or linkname.startswith('/'):
        raise ValueError(f'Unsafe absolute or empty symlink in driver archive: {target} -> {linkname}')
    rel_parent = target.parent.relative_to(dest_resolved).as_posix()
    joined = posixpath.join(rel_parent, linkname) if rel_parent != '.' else linkname
    norm = posixpath.normpath(joined)
    if norm == '..' or norm.startswith('../') or norm.startswith('/'):
        raise ValueError(f'Unsafe symlink escapes extraction root: {target} -> {linkname}')


def safe_tar_extract_stream(tf: tarfile.TarFile, prefix: str, dest_dir: Path) -> None:
    """Safely extract matching entries from an open TarFile into dest_dir with normalized modes."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    prefix_clean = posixpath.normpath(prefix).strip('/')
    prefix_slash = (prefix_clean + '/') if prefix_clean and prefix_clean != '.' else ''

    for member in tf:
        if member.name in ('', '.', './'):
            continue
        raw_name = member.name[2:] if member.name.startswith('./') else member.name
        if raw_name.startswith('/') or '..' in raw_name.split('/'):
            raise ValueError(f'Unsafe archive entry path: {member.name}')
        rel_posix = posixpath.normpath(raw_name)
        if prefix_slash:
            if rel_posix == prefix_clean:
                continue
            if not rel_posix.startswith(prefix_slash):
                continue
            sub_rel = rel_posix[len(prefix_slash):]
        else:
            sub_rel = rel_posix
        if not sub_rel or sub_rel == '.':
            continue
        rel_path = _validate_relative_member(sub_rel)
        target = dest_resolved / rel_path

        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o755)
        elif member.issym():
            _validate_symlink_target(dest_resolved, target, member.linkname)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(member.linkname)
        elif member.islnk():
            link_rel = member.linkname.lstrip('./')
            if prefix_slash and link_rel.startswith(prefix_slash):
                link_rel = link_rel[len(prefix_slash):]
            link_path = dest_resolved / _validate_relative_member(link_rel)
            if not link_path.is_file():
                raise ValueError(f'Unsafe or unresolved hardlink in archive: {member.name} -> {member.linkname}')
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            shutil.copyfile(link_path, target)
            target.chmod(link_path.stat().st_mode & 0o777)
        elif member.isreg():
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            src = tf.extractfile(member)
            if src is None:
                raise ValueError(f'Unable to read regular tar member: {member.name}')
            with src, target.open('wb') as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
            executable = bool(member.mode & 0o111)
            target.chmod(0o755 if executable else 0o644)
        else:
            raise ValueError(f'Unsupported tar member type for {member.name}')


def safe_tar_extract(tar_path: Path, prefix: str, dest_dir: Path) -> None:
    """Safely extract entries under prefix from a tar/tar.gz archive."""
    with tarfile.open(tar_path, 'r:*') as tf:
        safe_tar_extract_stream(tf, prefix, dest_dir)


def safe_zip_extract(zip_path: Path, dest_dir: Path) -> None:
    """Safely unpack a zip archive with path-traversal and symlink checks."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, 'r') as zf:
        for info in zf.infolist():
            name = info.filename.lstrip('./')
            if not name or name.endswith('/'):
                if name:
                    sub = dest_resolved / _validate_relative_member(name.rstrip('/'))
                    sub.mkdir(parents=True, exist_ok=True)
                    sub.chmod(0o755)
                continue
            target = dest_resolved / _validate_relative_member(name)
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                link_target = zf.read(info).decode('utf-8', errors='strict')
                _validate_symlink_target(dest_resolved, target, link_target)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(link_target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    target.unlink()
                with zf.open(info, 'r') as src, target.open('wb') as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                perm = (info.external_attr >> 16) & 0o777
                executable = bool(perm & 0o111) or target.suffix == '.so' or '.so.' in target.name
                target.chmod(0o755 if executable else 0o644)


def deduplicate_dameng(dameng_dir: Path) -> None:
    """Replace duplicate libcrypto.so/libssl.so regular files with symlinks to .so.3."""
    for base_name in ('libcrypto.so', 'libssl.so'):
        short = dameng_dir / base_name
        ver = dameng_dir / f'{base_name}.3'
        if short.is_file() and not short.is_symlink() and ver.is_file():
            if sha256_file(short) == sha256_file(ver):
                short.unlink()
                short.symlink_to(f'{base_name}.3')


def _copy_normalized_tree(src_dir: Path, dst_dir: Path) -> None:
    src_resolved = src_dir.resolve()
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_resolved = dst_dir.resolve()
    for root, dirs, files in os.walk(src_resolved, followlinks=False):
        rel_root = Path(root).relative_to(src_resolved)
        cur_dst = dst_resolved / rel_root
        cur_dst.mkdir(parents=True, exist_ok=True)
        cur_dst.chmod(0o755)
        for dname in dirs:
            dpath = Path(root) / dname
            dtarget = cur_dst / dname
            if dpath.is_symlink():
                link = os.readlink(dpath)
                _validate_symlink_target(dst_resolved, dtarget, link)
                if dtarget.exists() or dtarget.is_symlink():
                    dtarget.unlink()
                dtarget.symlink_to(link)
            else:
                dtarget.mkdir(parents=True, exist_ok=True)
                dtarget.chmod(0o755)
        for fname in files:
            fpath = Path(root) / fname
            ftarget = cur_dst / fname
            if ftarget.exists() or ftarget.is_symlink():
                ftarget.unlink()
            if fpath.is_symlink():
                link = os.readlink(fpath)
                _validate_symlink_target(dst_resolved, ftarget, link)
                ftarget.symlink_to(link)
            else:
                shutil.copyfile(fpath, ftarget)
                executable = bool(fpath.stat().st_mode & 0o111) or fname.endswith('.so') or '.so.' in fname
                ftarget.chmod(0o755 if executable else 0o644)


def _download_to_temp(url: str, dest_file: Path, expected_sha: str | None) -> str:
    safe_url = validate_vendor_url(url)
    request = urllib.request.Request(
        safe_url,
        headers={'User-Agent': 'magicDataHub-stage-vendor-drivers'},
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            content_length = response.headers.get('Content-Length')
            if content_length and content_length.isdigit() and int(content_length) > MAX_DRIVER_DOWNLOAD_BYTES:
                raise SystemExit(
                    f'Vendor driver download exceeds limit ({content_length} > {MAX_DRIVER_DOWNLOAD_BYTES})'
                )
            with dest_file.open('wb') as out:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DRIVER_DOWNLOAD_BYTES:
                        raise SystemExit(f'Vendor driver download exceeds limit ({MAX_DRIVER_DOWNLOAD_BYTES} bytes)')
                    digest.update(chunk)
                    out.write(chunk)
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise SystemExit(f'Failed to download vendor ODBC driver from {safe_url}: {exc}') from exc

    actual_sha = digest.hexdigest()
    if expected_sha:
        expected_norm = expected_sha.strip().lower()
        if not re.fullmatch(r'[0-9a-f]{64}', expected_norm):
            raise SystemExit(f'Invalid SHA-256 hex digest for vendor driver: {expected_sha}')
        if actual_sha != expected_norm:
            raise SystemExit(
                f'Vendor driver SHA-256 mismatch for {safe_url}: expected {expected_norm}, got {actual_sha}'
            )
    return actual_sha


def _extract_dameng_installer(installer_path: Path, extract_target: Path) -> None:
    payload_start = None
    with installer_path.open('rb') as stream:
        for line_no in range(1, 200):
            line = stream.readline()
            if not line:
                break
            match = re.match(rb'^skip=(\d+)\r?\n?$', line)
            if match:
                payload_start = int(match.group(1))
                break
        if payload_start is None or payload_start < 1:
            raise SystemExit(f'Invalid Dameng DMInstall.bin format: {installer_path}')
        stream.seek(0)
        for _ in range(payload_start - 1):
            stream.readline()
        with gzip.GzipFile(fileobj=stream, mode='rb') as gz, tarfile.open(fileobj=gz, mode='r|*') as tf:
            wanted = {
                'source/bin/libdodbc.so': 'libdodbc.so',
                'source/bin/libdmdpi.so': 'libdmdpi.so',
                'source/bin/libdmfldr.so': 'libdmfldr.so',
                'source/bin/dependencies/libcrypto.so': 'libcrypto.so',
                'source/bin/dependencies/libcrypto.so.3': 'libcrypto.so.3',
                'source/bin/dependencies/libssl.so': 'libssl.so',
                'source/bin/dependencies/libssl.so.3': 'libssl.so.3',
            }
            extract_target.mkdir(parents=True, exist_ok=True)
            for member in tf:
                norm = posixpath.normpath(member.name.lstrip('./'))
                if norm not in wanted or not member.isreg():
                    continue
                src = tf.extractfile(member)
                assert src is not None
                out_path = extract_target / wanted[norm]
                out_path.write_bytes(src.read())
                out_path.chmod(0o755)


def _stage_from_url(vendor: str, url: str, expected_sha: str | None, extract_target: Path) -> str:
    spec = VENDOR_SPECS[vendor]
    with tempfile.TemporaryDirectory(prefix=f'mdh-vendor-url-{vendor}-') as tmp:
        tmp_dir = Path(tmp)
        filename = posixpath.basename(urllib.parse.urlsplit(url).path) or 'driver.archive'
        archive_path = tmp_dir / filename
        archive_sha = _download_to_temp(url, archive_path, expected_sha)

        unpack_dir = tmp_dir / 'unpack'
        unpack_dir.mkdir()
        if zipfile.is_zipfile(archive_path):
            safe_zip_extract(archive_path, unpack_dir)
        elif tarfile.is_tarfile(archive_path):
            safe_tar_extract(archive_path, '', unpack_dir)
        else:
            raise SystemExit(f'Unsupported vendor archive format for {vendor}: {archive_path.name}')

        if vendor == 'oracle':
            hit = next((p for p in unpack_dir.rglob('libsqora.so.23.1') if p.is_file()), None)
            if hit is None:
                raise SystemExit(f'Oracle archive from {url} is missing libsqora.so.23.1')
            _copy_normalized_tree(hit.parent, extract_target)
        elif vendor == 'hana':
            hit = next((p for p in unpack_dir.rglob('libodbcHDB.so') if p.is_file()), None)
            if hit is None:
                raise SystemExit(f'SAP HANA archive from {url} is missing libodbcHDB.so')
            _copy_normalized_tree(hit.parent, extract_target)
        elif vendor == 'db2':
            hit = next((p for p in unpack_dir.rglob('libdb2.so') if p.is_file() and p.parent.name == 'lib'), None)
            if hit is None:
                raise SystemExit(f'IBM DB2 archive from {url} is missing lib/libdb2.so')
            _copy_normalized_tree(hit.parent.parent, extract_target)
        elif vendor == 'dameng':
            hit = next((p for p in unpack_dir.rglob('libdodbc.so') if p.is_file()), None)
            if hit is not None:
                for lib in ('libdodbc.so', 'libdmdpi.so', 'libdmfldr.so', 'libcrypto.so', 'libcrypto.so.3', 'libssl.so', 'libssl.so.3'):
                    found = next((p for p in unpack_dir.rglob(lib) if p.is_file()), None)
                    if found is None:
                        raise SystemExit(f'Dameng archive missing {lib}')
                    extract_target.mkdir(parents=True, exist_ok=True)
                    dst = extract_target / lib
                    shutil.copyfile(found, dst)
                    dst.chmod(0o755)
            else:
                installer = next((p for p in unpack_dir.rglob('DMInstall.bin') if p.is_file()), None)
                if installer is None:
                    iso = next((p for p in unpack_dir.rglob('*.iso') if p.is_file()), None)
                    if iso is not None:
                        iso_dir = tmp_dir / 'iso'
                        iso_dir.mkdir()
                        subprocess.run(['7z', 'x', str(iso), f'-o{iso_dir}', '-y'], check=True, stdout=subprocess.DEVNULL)
                        installer = next((p for p in iso_dir.rglob('DMInstall.bin') if p.is_file()), None)
                if installer is None:
                    raise SystemExit(f'Dameng archive from {url} contains neither libdodbc.so nor DMInstall.bin')
                _extract_dameng_installer(installer, extract_target)
            deduplicate_dameng(extract_target)
        else:
            raise SystemExit(f'Unsupported vendor: {vendor}')

    return f'url:{archive_sha}'


def _stage_from_image(vendor: str, extract_target: Path) -> str:
    spec = VENDOR_SPECS[vendor]
    odbc_image = RUNTIME['odbc_base_image']
    inspect = subprocess.run(['docker', 'image', 'inspect', odbc_image], capture_output=True, text=True)
    if inspect.returncode != 0:
        subprocess.run(['docker', 'pull', '--platform', RUNTIME['platform'], odbc_image], check=True)

    cid = subprocess.check_output(
        ['docker', 'create', '--platform', RUNTIME['platform'], odbc_image, '/bin/true'],
        text=True,
    ).strip()
    try:
        proc = subprocess.Popen(
            ['docker', 'cp', f'{cid}:{spec["image_path"]}', '-'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        tar_root = posixpath.basename(spec['image_path'].rstrip('/'))
        with tarfile.open(fileobj=proc.stdout, mode='r|*') as tf:
            safe_tar_extract_stream(tf, prefix=tar_root, dest_dir=extract_target)
        proc.stdout.close()
        stderr = proc.stderr.read().decode('utf-8', errors='replace') if proc.stderr else ''
        rc = proc.wait()
        if rc != 0:
            raise SystemExit(f'docker cp failed for {vendor} ({spec["image_path"]}): {stderr}')
    finally:
        subprocess.run(['docker', 'rm', '--force', cid], capture_output=True, check=False)

    if vendor == 'dameng':
        deduplicate_dameng(extract_target)
    return f'image:{odbc_image}'


def _stage_from_tarball(vendor: str, tarball: Path, extract_target: Path) -> str:
    tar_resolved = tarball.resolve()
    if not tar_resolved.is_file():
        raise SystemExit(f'Missing vendor drivers tarball: {tar_resolved}')
    actual_sha = sha256_file(tar_resolved)
    if actual_sha != VENDOR_TARBALL_SHA256:
        raise SystemExit(
            f'Vendor drivers tarball SHA256 mismatch for {tar_resolved}: '
            f'expected {VENDOR_TARBALL_SHA256}, got {actual_sha}'
        )
    safe_tar_extract(tar_resolved, VENDOR_SPECS[vendor]['tar_prefix'], extract_target)
    if vendor == 'dameng':
        deduplicate_dameng(extract_target)
    return f'tarball:{actual_sha}'


def verify_required_files(vendor: str, dest_dir: Path) -> None:
    for rel in VENDOR_SPECS[vendor]['required_files']:
        path = dest_dir / rel
        if not path.is_file() or path.stat().st_size == 0:
            raise SystemExit(f'Missing or empty required {vendor} ODBC driver file after staging: {path}')


def stage_vendor(
    vendor: str,
    dest_dir: Path,
    source: str = 'auto',
    tarball: Path | None = None,
    url: str | None = None,
    expected_sha: str | None = None,
) -> str:
    """Stage commercial ODBC driver tree for vendor into dest_dir; returns provenance string."""
    if vendor not in VENDOR_SPECS:
        raise SystemExit(f'Unsupported vendor {vendor!r}; expected one of {sorted(VENDOR_SPECS)}')
    if source not in ('auto', 'image', 'tarball'):
        raise SystemExit(f'Invalid vendor driver source mode: {source}')

    spec = VENDOR_SPECS[vendor]
    extract_target = dest_dir / spec['dest_rel'] if spec['dest_rel'] else dest_dir
    effective_url = url if url is not None else os.environ.get(spec['env_url'], '').strip()
    effective_sha = expected_sha if expected_sha is not None else os.environ.get(spec['env_sha'], '').strip() or None

    if effective_url:
        provenance = _stage_from_url(vendor, effective_url, effective_sha, extract_target)
    else:
        tar_candidate = tarball if tarball is not None else (VENDOR_TARBALL_DEFAULT if VENDOR_TARBALL_DEFAULT.is_file() else None)
        if source == 'tarball':
            if tar_candidate is None:
                raise SystemExit(
                    f'--vendor-drivers-source=tarball requested but tarball is missing: {VENDOR_TARBALL_DEFAULT}'
                )
            provenance = _stage_from_tarball(vendor, tar_candidate, extract_target)
        elif source == 'image':
            provenance = _stage_from_image(vendor, extract_target)
        else:
            if tar_candidate is not None and tar_candidate.is_file():
                provenance = _stage_from_tarball(vendor, tar_candidate, extract_target)
            else:
                provenance = _stage_from_image(vendor, extract_target)

    verify_required_files(vendor, dest_dir)
    return provenance


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vendor', required=True, choices=sorted(VENDOR_SPECS))
    parser.add_argument('--dest', type=Path, required=True)
    parser.add_argument('--source', choices=['auto', 'image', 'tarball'], default='auto')
    parser.add_argument('--tarball', type=Path, help='Local vendor tarball path override')
    parser.add_argument('--url', help='Per-vendor HTTPS archive override URL')
    parser.add_argument('--sha256', help='Expected archive SHA-256 when using --url')
    args = parser.parse_args()

    provenance = stage_vendor(
        vendor=args.vendor,
        dest_dir=args.dest,
        source=args.source,
        tarball=args.tarball,
        url=args.url,
        expected_sha=args.sha256,
    )
    print(json.dumps({
        'vendor': args.vendor,
        'dest': str(args.dest.resolve()),
        'driver_source': provenance,
    }))


if __name__ == '__main__':
    sys.exit(main())
