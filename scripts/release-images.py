#!/usr/bin/env python3
"""Build (and optionally verify/push) release core service and on-demand OCI plugin images."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ('sqlserver', 'oracle', 'db2', 'hana', 'dameng')
PLUGIN_EXT_KEY = {
    'sqlserver': 'sqlserver',
    'oracle': 'oracle',
    'db2': 'odbc_catalog',
    'hana': 'odbc_catalog',
    'dameng': 'odbc_catalog',
}
SERVICES = ('mqtt-server', 'fedengine-server', 'datahub-server')


def run(arguments, **kwargs):
    subprocess.run(arguments, cwd=ROOT, check=True, **kwargs)


def inspect_built(tag: str) -> dict:
    info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', tag], text=True))[0]
    digest = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag], text=True).strip()
    return {'image': tag, 'id': digest, 'size_bytes': int(info['Size'])}


def parse_plugins(raw_arg: str) -> list[str]:
    selected = []
    for raw in raw_arg.split(','):
        name = raw.strip()
        if not name:
            continue
        if name not in PLUGINS:
            raise SystemExit(f'Unknown plugin {name!r}; expected one of {PLUGINS}')
        if name not in selected:
            selected.append(name)
    if not selected:
        raise SystemExit('--plugins must specify at least one valid plugin')
    return selected


def needed_extensions(target: str, selected_plugins: list[str]) -> list[str]:
    keys = []
    if target in ('all', 'core-only'):
        keys.extend(['postgres', 'mysql'])
    if target in ('all', 'plugins-only'):
        for plugin in selected_plugins:
            ext = PLUGIN_EXT_KEY[plugin]
            if ext not in keys:
                keys.append(ext)
    return keys


def build_with_prepared_extensions(
    args,
    selected_plugins: list[str],
    extensions_dir: Path,
) -> None:
    build_cmd = [
        sys.executable, str(ROOT / 'scripts/build-images.py'),
        '--tag-prefix', args.tag_prefix,
        '--profile', args.profile,
        '--version-tag', args.version,
        '--extensions-dir', str(extensions_dir),
        '--vendor-drivers-source', args.vendor_drivers_source,
    ]
    if args.target in ('all', 'core-only'):
        build_cmd += ['--duckdb-library-dir', str(args.duckdb_library_dir)]
    if args.target == 'plugins-only':
        build_cmd += ['--plugins-only', '--plugins', ','.join(selected_plugins)]
    elif args.target == 'all':
        build_cmd += ['--build-plugins', '--plugins', ','.join(selected_plugins)]
    if args.vendor_drivers_tarball is not None:
        build_cmd += ['--vendor-drivers-tarball', str(args.vendor_drivers_tarball)]
    if args.prebuilt_bin_dir is not None:
        build_cmd += ['--prebuilt-bin-dir', str(args.prebuilt_bin_dir)]
    if args.build_meta is not None:
        build_cmd += ['--build-meta', str(args.build_meta)]
    if args.also_latest:
        build_cmd.append('--also-latest')
    if args.require_clean:
        build_cmd.append('--require-clean')
    run(build_cmd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag-prefix', default='ghcr.io/muidea', help='OCI registry prefix (default: ghcr.io/muidea)')
    parser.add_argument('--version', required=True, help='release tag (e.g. v0.1.0)')
    parser.add_argument('--profile', choices=['dev', 'release'], default='release')
    parser.add_argument('--target', choices=['all', 'core-only', 'plugins-only'], default='all')
    parser.add_argument('--plugins', default=','.join(PLUGINS), help='comma-separated plugin subset')
    parser.add_argument('--also-latest', action='store_true', help='also tag :latest alongside :<version>')
    parser.add_argument('--extensions-dir', type=Path, help='already prepared extensions directory (skips prepare-extensions.py)')
    parser.add_argument('--extensions-mode', choices=['local', 'release'],
                        help='prepare extensions from --extensions-source-dir or remote GitHub Releases')
    parser.add_argument('--extensions-source-dir', type=Path,
                        default=ROOT.parent / 'modorch-fedengine/data/extensions',
                        help='directory containing locally compiled custom DuckDB 2.0.0 extensions')
    parser.add_argument('--extensions-base-url', help='optional HTTPS base URL override for release extension fetches')
    parser.add_argument('--duckdb-library-dir', type=Path,
                        default=ROOT / 'target/duckdb-download/x86_64-unknown-linux-gnu/v2.0.0')
    parser.add_argument('--vendor-drivers-source', choices=['auto', 'image', 'tarball'], default='auto')
    parser.add_argument('--vendor-drivers-tarball', type=Path,
                        default=ROOT / 'data/runtime-base/odbc-vendor-drivers-x86_64.tar.gz')
    parser.add_argument('--prebuilt-bin-dir', type=Path, help='directory containing prebuilt Rust binaries')
    parser.add_argument('--build-meta', type=Path, help='build metadata JSON generated by build-images.py --write-build-meta')
    parser.add_argument('--require-clean', action='store_true', help='require clean Git inputs across all repos')
    parser.add_argument('--verify', action='store_true', help='run check-images.sh and/or verify-plugins.py after build')
    parser.add_argument('--push', action='store_true', help='push built images (requires --require-clean)')
    parser.add_argument('--manifest-out', type=Path, help='optional path to write release image digest manifest JSON')
    args = parser.parse_args()

    assert re.fullmatch(r'[a-z0-9]+(?:[._/-][a-z0-9]+)*', args.tag_prefix), 'invalid registry prefix'
    assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', args.version), 'invalid version tag'
    if args.push and not args.require_clean:
        raise SystemExit('--push requires --require-clean so dirty development trees are never published')

    selected_plugins = parse_plugins(args.plugins) if args.target in ('all', 'plugins-only') else []
    ext_keys = needed_extensions(args.target, selected_plugins)
    vendor_tarball = args.vendor_drivers_tarball if args.vendor_drivers_tarball.is_file() else None
    args.vendor_drivers_tarball = vendor_tarball

    if args.extensions_dir is not None:
        build_with_prepared_extensions(args, selected_plugins, args.extensions_dir)
    else:
        ext_mode = args.extensions_mode or ('local' if args.extensions_source_dir.is_dir() else 'release')
        with tempfile.TemporaryDirectory(prefix='mdh-release-ext-') as ext_tmp:
            prep_cmd = [
                sys.executable, str(ROOT / 'scripts/prepare-extensions.py'),
                '--dest', ext_tmp,
                '--only', ','.join(ext_keys),
            ]
            if ext_mode == 'release':
                prep_cmd.append('--fetch-releases')
                if args.extensions_base_url:
                    prep_cmd += ['--extensions-base-url', args.extensions_base_url]
            else:
                prep_cmd += ['--source-dir', str(args.extensions_source_dir)]
            run(prep_cmd)
            build_with_prepared_extensions(args, selected_plugins, Path(ext_tmp))

    if args.verify:
        if args.target in ('all', 'core-only'):
            run([
                str(ROOT / 'scripts/check-images.sh'),
                '--tag-prefix', args.tag_prefix,
                '--profile', args.profile,
                '--tag', args.version,
            ])
        if args.target == 'all':
            run([
                sys.executable, str(ROOT / 'scripts/verify-plugins.py'),
                '--tag-prefix', args.tag_prefix,
                '--version', args.version,
                '--plugins', ','.join(selected_plugins),
            ])
        elif args.target == 'plugins-only':
            run([
                sys.executable, str(ROOT / 'scripts/verify-plugins.py'),
                '--tag-prefix', args.tag_prefix,
                '--version', args.version,
                '--plugins-only',
                '--plugins', ','.join(selected_plugins),
            ])

    images = []
    if args.target in ('all', 'core-only'):
        images += [f'{args.tag_prefix}/{svc}:{args.version}' for svc in SERVICES]
    if args.target in ('all', 'plugins-only'):
        images += [f'{args.tag_prefix}/datahub-plugin-{p}:{args.version}' for p in selected_plugins]

    latest_images = []
    if args.also_latest:
        if args.target in ('all', 'core-only'):
            latest_images += [f'{args.tag_prefix}/{svc}:latest' for svc in SERVICES]
        if args.target in ('all', 'plugins-only'):
            latest_images += [f'{args.tag_prefix}/datahub-plugin-{p}:latest' for p in selected_plugins]

    records = [inspect_built(tag) for tag in images]
    if args.push:
        for tag in images + latest_images:
            run(['docker', 'push', tag])

    summary = {
        'tag_prefix': args.tag_prefix,
        'version': args.version,
        'profile': args.profile,
        'target': args.target,
        'plugins': selected_plugins,
        'pushed': args.push,
        'also_latest': args.also_latest,
        'images': records,
    }
    if args.manifest_out:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_out.write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
