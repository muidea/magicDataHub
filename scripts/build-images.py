#!/usr/bin/env python3
"""Build layered service images (with postgres/mysql built in) and on-demand OCI plugin images; never push."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

from acceptance.fixtures import LOCK, validate_extensions

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = json.loads((ROOT / 'docker/runtime.lock.json').read_text(encoding='utf-8'))
CORE_BASE_TAG = 'mdh-core-base:debian13'
ODBC_PLUGIN_BASE_TAG = 'mdh-odbc-plugin-base:local'
SERVICES = {
    'mqtt-server': 'mqtt-container.toml',
    'fedengine-server': 'fedengine-container.toml',
    'datahub-server': 'combined-container.toml',
}
BUILTIN_EXTENSION_KEYS = ('postgres', 'mysql')

_STAGE_SPEC = importlib.util.spec_from_file_location('stage_vendor_drivers', ROOT / 'scripts/stage-vendor-drivers.py')
_STAGE = importlib.util.module_from_spec(_STAGE_SPEC)
_STAGE_SPEC.loader.exec_module(_STAGE)

PLUGINS = {
    'sqlserver': {
        'ext_key': 'sqlserver',
        'shared_base': False,
        'driver_subdir': None,
        'odbc_ini': (
            'sqlserver.ini',
            '[FreeTDS]\n'
            'Description=TDS driver (Sybase/MS SQL)\n'
            'Driver=/usr/lib/x86_64-linux-gnu/odbc/libtdsodbc.so\n'
            'Setup=/usr/lib/x86_64-linux-gnu/odbc/libtdsS.so\n'
            'CPTimeout=\n'
            'CPReuse=\n'
            'UsageCount=1\n',
        ),
    },
    'oracle': {
        'ext_key': 'oracle',
        'shared_base': False,
        'driver_subdir': 'oracle',
        'odbc_ini': (
            'oracle.ini',
            '[Oracle 23 ODBC driver]\n'
            'Description=Oracle ODBC driver for Instant Client\n'
            'Driver=/opt/magicdatahub/plugins/drivers/oracle/libsqora.so.23.1\n'
            'Setup=\n'
            'FileUsage=1\n'
            'CPTimeout=\n'
            'CPReuse=\n',
        ),
    },
    'db2': {
        'ext_key': 'odbc_catalog',
        'shared_base': True,
        'driver_subdir': 'db2',
        'odbc_ini': (
            'db2.ini',
            '[IBM DB2 ODBC DRIVER]\n'
            'Description=IBM Db2 CLI ODBC driver\n'
            'Driver=/opt/magicdatahub/plugins/drivers/db2/clidriver/lib/libdb2.so\n'
            'Setup=\n'
            'FileUsage=1\n',
        ),
    },
    'hana': {
        'ext_key': 'odbc_catalog',
        'shared_base': True,
        'driver_subdir': 'hana',
        'odbc_ini': (
            'hana.ini',
            '[HDBODBC]\n'
            'Description=SAP HANA ODBC driver\n'
            'Driver=/opt/magicdatahub/plugins/drivers/hana/libodbcHDB.so\n'
            'Setup=\n'
            'FileUsage=1\n',
        ),
    },
    'dameng': {
        'ext_key': 'odbc_catalog',
        'shared_base': True,
        'driver_subdir': 'dameng',
        'odbc_ini': (
            'dameng.ini',
            '[DM8 ODBC DRIVER]\n'
            'Description=DM8 ODBC driver\n'
            'Driver=/opt/magicdatahub/plugins/drivers/dameng/libdodbc.so\n'
            'Setup=\n'
            'FileUsage=1\n',
        ),
    },
}


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
    return {
        'head': output(['git', '-C', str(directory), 'rev-parse', 'HEAD']),
        'dirty': bool(output(['git', '-C', str(directory), 'status', '--porcelain', '--untracked-files=all'])),
    }


def resolve_git_states(metadata_graph: dict) -> dict:
    """Resolve Git states across magicDataHub and its business libraries (sibling paths or pinned Git revs)."""
    states = {'magicDataHub': git_state(ROOT)}
    packages = {p['name']: p for p in metadata_graph['packages']}
    for name in ('modorch-mqtt', 'modorch-fedengine'):
        pkg = packages.get(name)
        if pkg is None:
            raise ValueError(f'Missing {name} package in cargo metadata graph')
        source = pkg.get('source')
        if source is None:
            repo_dir = ROOT.parent / name
            if not repo_dir.is_dir():
                raise ValueError(f'Local path source selected for {name}, but sibling directory is missing: {repo_dir}')
            states[name] = git_state(repo_dir)
        elif source.startswith('git+'):
            rev = source.rsplit('#', 1)[-1]
            if not re.fullmatch(r'[0-9a-f]{40}', rev):
                raise ValueError(f'Expected 40-hex pinned Git commit in Cargo source for {name}, got: {source}')
            states[name] = {'head': rev, 'dirty': False, 'source': source}
        else:
            raise ValueError(f'Unsupported Cargo source for {name}: {source}')
    return states


def compute_build_meta() -> tuple[dict, Path]:
    subprocess.run([sys.executable, str(ROOT / 'scripts/verify-dependency-identity.py')], cwd=ROOT, check=True)
    host = output(['rustc', '-vV'])
    cargo_target = os.environ.get('CARGO_BUILD_TARGET') or next(
        line[6:] for line in host.splitlines() if line.startswith('host: ')
    )
    metadata = json.loads(output([
        'cargo', 'metadata', '--locked', '--offline',
        '--filter-platform', cargo_target, '--format-version=1',
    ], cwd=ROOT))
    target = Path(metadata['target_directory'])
    if os.environ.get('CARGO_BUILD_TARGET'):
        assert os.environ['CARGO_BUILD_TARGET'] == 'x86_64-unknown-linux-gnu', 'unsupported artifact target'
        target /= os.environ['CARGO_BUILD_TARGET']
    states = resolve_git_states(metadata)
    clean = not any(s['dirty'] for s in states.values())
    identity = json.loads((ROOT / 'scripts/source-identity.json').read_text(encoding='utf-8'))
    meta = {
        'git_inputs': states,
        'clean_git_inputs': clean,
        'host_source_sha256': host_identity(),
        'library_identity': identity,
    }
    return meta, target


def builtin_extension_manifest():
    return {
        'duckdb_version': LOCK['duckdb_version'],
        'platform': 'linux_amd64',
        'extensions': {
            key: {field: LOCK['extensions'][key][field] for field in ['path', 'sha256']}
            for key in BUILTIN_EXTENSION_KEYS
        },
    }


def build_core_base():
    dockerfile_text = (ROOT / 'docker/runtime-core.Dockerfile').read_text(encoding='utf-8')
    assert 'ARG CORE_BASE_IMAGE=' + RUNTIME['core_base_image'] + '\n' in dockerfile_text, (
        'runtime-core.Dockerfile base differs from runtime.lock.json'
    )
    subprocess.run(
        [
            'docker', 'build', '--platform', RUNTIME['platform'],
            '--file', str(ROOT / 'docker/runtime-core.Dockerfile'),
            '--build-arg', 'CORE_BASE_IMAGE=' + RUNTIME['core_base_image'],
            '--tag', CORE_BASE_TAG,
            str(ROOT / 'docker'),
        ],
        check=True,
    )
    return CORE_BASE_TAG


def oci_labels(version_label: str, revision: str, created: str, title: str):
    return [
        '--label', 'org.opencontainers.image.source=https://github.com/muidea/magicDataHub',
        '--label', f'org.opencontainers.image.version={version_label}',
        '--label', f'org.opencontainers.image.revision={revision}',
        '--label', f'org.opencontainers.image.created={created}',
        '--label', f'org.opencontainers.image.title={title}',
    ]


def parse_selected_plugins(args) -> list[str]:
    if not (args.build_plugins or args.plugins_only):
        return []
    if args.plugins:
        selected = []
        for raw in args.plugins.split(','):
            name = raw.strip()
            if not name:
                continue
            if name not in PLUGINS:
                raise SystemExit(f'Unknown plugin {name!r}; expected one of {list(PLUGINS)}')
            if name not in selected:
                selected.append(name)
        if not selected:
            raise SystemExit('--plugins must specify at least one valid plugin name')
        return selected
    if args.plugin:
        return [args.plugin]
    return list(PLUGINS.keys())


def resolve_binary(prebuilt_bin_dir: Path | None, target_dir: Path | None, profile: str, profile_dir: str, package_name: str) -> Path:
    if prebuilt_bin_dir is not None:
        binary = prebuilt_bin_dir / package_name
        if not binary.is_file():
            raise SystemExit(f'Missing prebuilt binary {binary}')
        return binary
    command = ['cargo', 'build', '-p', package_name, '--locked', '--offline']
    if profile == 'release':
        command.append('--release')
    subprocess.run(command, cwd=ROOT, check=True)
    assert target_dir is not None
    binary = target_dir / profile_dir / package_name
    if not binary.is_file():
        raise SystemExit(f'Missing compiled binary {binary}')
    return binary


def build_plugin_images(
    args,
    selected_plugins: list[str],
    extensions_dir: Path,
    target_dir: Path | None,
    prebuilt_bin_dir: Path | None,
    profile_dir: str,
    tag_name: str,
    states: dict,
    clean: bool,
    created: str,
    remote_ci: bool,
):
    init_binary = resolve_binary(prebuilt_bin_dir, target_dir, args.profile, profile_dir, 'mdh-plugin-init')
    needs_shared_base = any(PLUGINS[p]['shared_base'] for p in selected_plugins)
    if needs_shared_base:
        base_dockerfile = (ROOT / 'docker/plugin-odbc-base.Dockerfile').read_text(encoding='utf-8')
        assert 'ARG BASE_IMAGE=' + RUNTIME['base_image'] + '\n' in base_dockerfile, (
            'plugin-odbc-base.Dockerfile base differs from runtime lock'
        )
        with tempfile.TemporaryDirectory(prefix='mdh-odbc-base-') as base_tmp:
            base_ctx = Path(base_tmp)
            (base_ctx / 'bin').mkdir()
            (base_ctx / 'mount-root/plugins').mkdir(parents=True)
            os.chmod(base_ctx / 'mount-root/plugins', 0o755)
            subprocess.run(['strip', '--strip-debug', '-o', str(base_ctx / 'bin/mdh-plugin-init'), str(init_binary)], check=True)
            os.chmod(base_ctx / 'bin/mdh-plugin-init', 0o755)
            odbc_entry = LOCK['extensions']['odbc_catalog']
            ext_dst = base_ctx / 'payload-ext/extensions' / odbc_entry['path']
            ext_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(extensions_dir / odbc_entry['path'], ext_dst)
            os.chmod(ext_dst, 0o644)
            subprocess.run(
                [
                    'docker', 'build', '--platform', RUNTIME['platform'],
                    '--file', str(ROOT / 'docker/plugin-odbc-base.Dockerfile'),
                    '--build-arg', 'BASE_IMAGE=' + RUNTIME['base_image'],
                    '--tag', ODBC_PLUGIN_BASE_TAG,
                ]
                + oci_labels(tag_name, states['magicDataHub']['head'], created, 'magicDataHub ODBC Plugin Base')
                + [str(base_ctx)],
                check=True,
            )

    built_plugins = []
    for plugin_name in selected_plugins:
        spec = PLUGINS[plugin_name]
        ext_key = spec['ext_key']
        ext_entry = LOCK['extensions'][ext_key]
        with tempfile.TemporaryDirectory(prefix=f'mdh-plugin-{plugin_name}-') as tmp:
            ctx = Path(tmp)
            drv_root = ctx / 'payload-driver'
            drv_root.mkdir()
            if not spec['shared_base']:
                (ctx / 'bin').mkdir()
                (ctx / 'mount-root/plugins').mkdir(parents=True)
                os.chmod(ctx / 'mount-root/plugins', 0o755)
                subprocess.run(['strip', '--strip-debug', '-o', str(ctx / 'bin/mdh-plugin-init'), str(init_binary)], check=True)
                os.chmod(ctx / 'bin/mdh-plugin-init', 0o755)
                ext_dst = ctx / 'payload-ext/extensions' / ext_entry['path']
                ext_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(extensions_dir / ext_entry['path'], ext_dst)
                os.chmod(ext_dst, 0o644)

            if spec['driver_subdir']:
                drv_dst_base = drv_root / 'drivers' / spec['driver_subdir']
                driver_source = _STAGE.stage_vendor(
                    vendor=plugin_name,
                    dest_dir=drv_dst_base,
                    source=args.vendor_drivers_source,
                    tarball=args.vendor_drivers_tarball,
                )
            else:
                driver_source = 'system:freetds'

            ini_name, ini_body = spec['odbc_ini']
            (drv_root / 'odbc').mkdir(parents=True, exist_ok=True)
            ini_file = drv_root / 'odbc' / ini_name
            ini_file.write_text(ini_body, encoding='utf-8')
            os.chmod(ini_file, 0o644)

            manifest = {
                'name': plugin_name,
                'version': tag_name,
                'duckdb_version': LOCK['duckdb_version'],
                'extension': {
                    'key': ext_key,
                    'path': ext_entry['path'],
                    'sha256': ext_entry['sha256'],
                },
                'driver_subdir': spec['driver_subdir'],
                'odbc_ini': ini_name,
            }
            manifest_file = drv_root / 'plugin.json'
            manifest_file.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
            os.chmod(manifest_file, 0o644)

            tag = f'{args.tag_prefix}/datahub-plugin-{plugin_name}:{tag_name}'
            if spec['shared_base']:
                cmd = [
                    'docker', 'build', '--platform', RUNTIME['platform'],
                    '--file', str(ROOT / 'docker/plugin-child.Dockerfile'),
                    '--build-arg', 'BASE_IMAGE=' + ODBC_PLUGIN_BASE_TAG,
                    '--tag', tag,
                ]
            else:
                cmd = [
                    'docker', 'build', '--platform', RUNTIME['platform'],
                    '--file', str(ROOT / 'docker/plugin.Dockerfile'),
                    '--build-arg', 'BASE_IMAGE=' + RUNTIME['base_image'],
                    '--tag', tag,
                ]
            cmd += oci_labels(tag_name, states['magicDataHub']['head'], created, f'magicDataHub Plugin: {plugin_name}')
            cmd += ['--label', f'io.muidea.datahub.driver-source={driver_source}']
            cmd.append(str(ctx))
            subprocess.run(cmd, check=True)
            if args.also_latest:
                subprocess.run(['docker', 'tag', tag, f'{args.tag_prefix}/datahub-plugin-{plugin_name}:latest'], check=True)
            print(json.dumps({
                'image': tag,
                'status': 'Built',
                'profile': args.profile,
                'kind': 'plugin',
                'plugin': plugin_name,
                'driver_source': driver_source,
                'clean_git_inputs': clean,
                'remote_reproducibility': 'VerifiedOnline' if remote_ci else 'NotRun',
            }), flush=True)
            built_plugins.append(tag)
    return built_plugins


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag-prefix', help='image prefix, e.g. ghcr.io/muidea')
    parser.add_argument('--profile', choices=['dev', 'release'], default='dev')
    parser.add_argument('--version-tag', help='version tag override (defaults to --profile)')
    parser.add_argument('--also-latest', action='store_true', help='also tag built images as :latest')
    parser.add_argument('--service', choices=SERVICES)
    parser.add_argument('--extensions-dir', type=Path)
    parser.add_argument('--duckdb-library-dir', type=Path)
    parser.add_argument('--build-plugins', action='store_true', help='also build optional OCI plugin Init Container images')
    parser.add_argument('--plugins-only', action='store_true', help='build only OCI plugin Init Container images (skip core services)')
    parser.add_argument('--plugins', help='comma-separated plugin subset (e.g. oracle,dameng)')
    parser.add_argument('--plugin', choices=sorted(PLUGINS), help='build a single plugin when building plugins')
    parser.add_argument('--vendor-drivers-source', choices=['auto', 'image', 'tarball'], default='auto',
                        help='source for commercial vendor ODBC drivers (default: auto)')
    parser.add_argument('--vendor-drivers-tarball', type=Path,
                        default=_STAGE.VENDOR_TARBALL_DEFAULT if _STAGE.VENDOR_TARBALL_DEFAULT.is_file() else None)
    parser.add_argument('--prebuilt-bin-dir', type=Path, help='directory containing prebuilt Rust binaries')
    parser.add_argument('--build-meta', type=Path, help='build metadata JSON generated by --write-build-meta')
    parser.add_argument('--write-build-meta', type=Path, help='compute and write build-meta JSON then exit')
    parser.add_argument('--require-clean', action='store_true', help='reject uncommitted host/business inputs before building')
    args = parser.parse_args()

    assert platform.system() == 'Linux' and platform.machine() == 'x86_64', 'only verified Linux amd64 artifacts are supported'

    if args.write_build_meta:
        meta, _ = compute_build_meta()
        args.write_build_meta.parent.mkdir(parents=True, exist_ok=True)
        args.write_build_meta.write_text(json.dumps(meta, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(meta, indent=2), flush=True)
        return

    if not args.tag_prefix:
        raise SystemExit('--tag-prefix is required')
    if not args.extensions_dir:
        raise SystemExit('--extensions-dir is required')

    tag_name = args.version_tag or args.profile
    assert re.fullmatch(r'[a-z0-9]+(?:[._/-][a-z0-9]+)*', args.tag_prefix), 'invalid image prefix'
    assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', tag_name), 'invalid image tag'

    selected_services = [] if args.plugins_only else ([args.service] if args.service else list(SERVICES))
    selected_plugins = parse_selected_plugins(args)
    if not selected_services and not selected_plugins:
        raise SystemExit('Nothing selected to build; specify services and/or --build-plugins / --plugins-only')

    needs_duckdb = any(s != 'mqtt-server' for s in selected_services)
    if selected_services:
        assert 'ARG BASE_IMAGE=' + RUNTIME['base_image'] + '\n' in (ROOT / 'docker/Dockerfile').read_text(encoding='utf-8'), (
            'Dockerfile base differs from runtime lock'
        )
    if needs_duckdb:
        if not args.duckdb_library_dir:
            raise SystemExit('--duckdb-library-dir is required when building fedengine-server or datahub-server')
        native = args.duckdb_library_dir.resolve() / 'libduckdb.so'
        assert native.is_file(), f'missing libduckdb.so at {native}'
        assert sha(native) == RUNTIME['libduckdb_sha256'], 'native DuckDB checksum mismatch'
    else:
        native = None

    needed_ext_keys = []
    if needs_duckdb:
        needed_ext_keys.extend(BUILTIN_EXTENSION_KEYS)
    for plugin_name in selected_plugins:
        key = PLUGINS[plugin_name]['ext_key']
        if key not in needed_ext_keys:
            needed_ext_keys.append(key)
    extensions = validate_extensions(args.extensions_dir, keys=needed_ext_keys)

    if args.build_meta:
        meta = json.loads(args.build_meta.read_text(encoding='utf-8'))
        states = meta['git_inputs']
        clean = bool(meta['clean_git_inputs'])
        source_before = meta['host_source_sha256']
        identity = meta['library_identity']
        assert host_identity() == source_before, 'host source changed after build-meta generation'
        target = None
    else:
        meta, target = compute_build_meta()
        states = meta['git_inputs']
        clean = meta['clean_git_inputs']
        source_before = meta['host_source_sha256']
        identity = meta['library_identity']

    if args.require_clean:
        assert clean, 'clean Git inputs are required; current local source must be committed and revalidated first'

    prebuilt_bin_dir = args.prebuilt_bin_dir.resolve() if args.prebuilt_bin_dir else None
    if prebuilt_bin_dir is None and target is None:
        raise SystemExit('--build-meta requires --prebuilt-bin-dir when building artifacts without cargo')

    created = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    profile_dir = 'debug' if args.profile == 'dev' else 'release'
    remote_ci = os.environ.get('GITHUB_ACTIONS') == 'true'

    core_base = None
    if needs_duckdb:
        core_base = build_core_base()

    for service in selected_services:
        binary = resolve_binary(prebuilt_bin_dir, target, args.profile, profile_dir, service)
        assert host_identity() == source_before, 'host source changed during artifact build'
        if not args.build_meta:
            subprocess.run([sys.executable, str(ROOT / 'scripts/verify-dependency-identity.py')], cwd=ROOT, check=True)
        with tempfile.TemporaryDirectory(prefix='mdh-image-context-') as temporary:
            context = Path(temporary)
            layer_duckdb = context / 'layer-duckdb'
            layer_extensions = context / 'layer-extensions'
            layer_app = context / 'layer-app'
            for directory in ['bin', 'config', 'var']:
                (layer_app / directory).mkdir(parents=True)
            layer_duckdb.mkdir()
            layer_extensions.mkdir()

            subprocess.run(['strip', '--strip-debug', '-o', str(layer_app / 'bin/service'), str(binary)], check=True)
            os.chmod(layer_app / 'bin/service', 0o755)
            profile_cfg = SERVICES[service]
            cfg_root = ROOT / 'config/container'
            shutil.copyfile(cfg_root / profile_cfg, layer_app / 'config/host.toml')
            if service != 'fedengine-server':
                shutil.copytree(cfg_root / 'mqtt', layer_app / 'config/mqtt')
            if service != 'mqtt-server':
                assert native is not None
                shutil.copytree(cfg_root / 'fedengine', layer_app / 'config/fedengine')
                (layer_duckdb / 'lib').mkdir(parents=True)
                shutil.copyfile(native, layer_duckdb / 'lib/libduckdb.so')
                manifest_d = layer_extensions / 'extensions/manifest.d'
                manifest_d.mkdir(parents=True)
                for key in BUILTIN_EXTENSION_KEYS:
                    entry = LOCK['extensions'][key]
                    destination = layer_extensions / 'extensions' / entry['path']
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(extensions / entry['path'], destination)
                    os.chmod(destination, 0o644)
                    fragment = {
                        'duckdb_version': LOCK['duckdb_version'],
                        'platform': 'linux_amd64',
                        'extensions': {key: {'path': entry['path'], 'sha256': entry['sha256']}},
                    }
                    (manifest_d / f'{key}.json').write_text(json.dumps(fragment, indent=2) + '\n', encoding='utf-8')
                (layer_extensions / 'extensions/manifest.json').write_text(
                    json.dumps(builtin_extension_manifest(), indent=2) + '\n', encoding='utf-8'
                )

            service_base = RUNTIME['base_image'] if service == 'mqtt-server' else core_base
            native_files = {}
            if (layer_duckdb / 'lib').exists():
                for p in (layer_duckdb / 'lib').rglob('*'):
                    if p.is_file():
                        native_files[p.relative_to(layer_duckdb).as_posix()] = sha(p)
            if (layer_extensions / 'extensions').exists():
                for p in (layer_extensions / 'extensions').rglob('*'):
                    if p.is_file():
                        native_files[p.relative_to(layer_extensions).as_posix()] = sha(p)

            provenance = {
                'service': service,
                'framework_version': '0.0.3',
                'profile': args.profile,
                'tag': tag_name,
                'base_image': service_base,
                'platform': RUNTIME['platform'],
                'builtin_extensions': list(BUILTIN_EXTENSION_KEYS) if service != 'mqtt-server' else [],
                'git_inputs': states,
                'clean_git_inputs': clean,
                'remote_ci_verified': remote_ci,
                'host_source_sha256': source_before,
                'library_identity': identity,
                'binary_sha256': sha(layer_app / 'bin/service'),
                'native_files': native_files,
            }
            (layer_app / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n', encoding='utf-8')
            tag = f'{args.tag_prefix}/{service}:{tag_name}'
            subprocess.run(
                [
                    'docker', 'build', '--platform', RUNTIME['platform'],
                    '--file', str(ROOT / 'docker/Dockerfile'),
                    '--build-arg', 'BASE_IMAGE=' + service_base,
                    '--tag', tag,
                ]
                + oci_labels(tag_name, states['magicDataHub']['head'], created, f'magicDataHub {service}')
                + [str(context)],
                check=True,
            )
            if args.also_latest:
                subprocess.run(['docker', 'tag', tag, f'{args.tag_prefix}/{service}:latest'], check=True)
            print(json.dumps({
                'image': tag,
                'status': 'Built',
                'profile': args.profile,
                'clean_git_inputs': clean,
                'remote_reproducibility': 'VerifiedOnline' if remote_ci else 'NotRun',
            }), flush=True)

    if selected_plugins:
        build_plugin_images(
            args=args,
            selected_plugins=selected_plugins,
            extensions_dir=extensions,
            target_dir=target,
            prebuilt_bin_dir=prebuilt_bin_dir,
            profile_dir=profile_dir,
            tag_name=tag_name,
            states=states,
            clean=clean,
            created=created,
            remote_ci=remote_ci,
        )


if __name__ == '__main__':
    main()
