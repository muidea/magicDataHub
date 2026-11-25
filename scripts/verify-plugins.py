#!/usr/bin/env python3
"""Verify modular core service and on-demand OCI plugin Init Containers over bridge -p ports."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = json.loads((ROOT / 'docker/runtime.lock.json').read_text(encoding='utf-8'))
SOURCES = json.loads((ROOT / 'scripts/acceptance/sources.json').read_text(encoding='utf-8'))
CORE_BASE_TAG = 'mdh-core-base:debian13'
ALL_PLUGINS = ('sqlserver', 'oracle', 'db2', 'hana', 'dameng')

PLUGIN_CHECKS = {
    'sqlserver': {
        'ext_key': 'sqlserver',
        'odbc_driver': 'FreeTDS',
        'so_path': '/usr/lib/x86_64-linux-gnu/odbc/libtdsodbc.so',
    },
    'oracle': {
        'ext_key': 'oracle',
        'odbc_driver': 'Oracle 23 ODBC driver',
        'so_path': '/opt/magicdatahub/plugins/drivers/oracle/libsqora.so.23.1',
    },
    'db2': {
        'ext_key': 'odbc_catalog',
        'odbc_driver': 'IBM DB2 ODBC DRIVER',
        'so_path': '/opt/magicdatahub/plugins/drivers/db2/clidriver/lib/libdb2.so',
    },
    'hana': {
        'ext_key': 'odbc_catalog',
        'odbc_driver': 'HDBODBC',
        'so_path': '/opt/magicdatahub/plugins/drivers/hana/libodbcHDB.so',
    },
    'dameng': {
        'ext_key': 'odbc_catalog',
        'odbc_driver': 'DM8 ODBC DRIVER',
        'so_path': '/opt/magicdatahub/plugins/drivers/dameng/libdodbc.so',
    },
}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def run(arguments, **kwargs):
    return subprocess.run(arguments, cwd=ROOT, text=True, check=True, **kwargs)


def out(arguments, **kwargs) -> str:
    return subprocess.check_output(arguments, cwd=ROOT, text=True, **kwargs).strip()


def ensure_core_base() -> None:
    inspect = subprocess.run(['docker', 'image', 'inspect', CORE_BASE_TAG], capture_output=True, text=True)
    if inspect.returncode == 0:
        return
    run([
        'docker', 'build', '--platform', RUNTIME['platform'],
        '--file', str(ROOT / 'docker/runtime-core.Dockerfile'),
        '--build-arg', 'CORE_BASE_IMAGE=' + RUNTIME['core_base_image'],
        '--tag', CORE_BASE_TAG,
        str(ROOT / 'docker'),
    ])


def parse_plugins(raw_arg: str) -> list[str]:
    selected = []
    for raw in raw_arg.split(','):
        name = raw.strip()
        if not name:
            continue
        if name not in PLUGIN_CHECKS:
            raise SystemExit(f'Unknown plugin {name!r}; expected one of {ALL_PLUGINS}')
        if name not in selected:
            selected.append(name)
    if not selected:
        raise SystemExit('--plugins must specify at least one valid plugin')
    return selected


def wait_ready(compose_cmd, env, pg_port, http_port, mqtt_port):
    deadline = time.monotonic() + 90
    while True:
        probe = subprocess.run(
            [
                sys.executable, str(ROOT / 'scripts/probe-service.py'),
                '--mqtt', f'127.0.0.1:{mqtt_port}',
                '--pgwire', f'127.0.0.1:{pg_port}',
                '--management', f'127.0.0.1:{http_port}',
            ],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        if probe.returncode == 0:
            break
        if time.monotonic() >= deadline:
            logs = out(compose_cmd + ['logs', '--no-color', 'datahub-server'], env=env)
            raise AssertionError(f'datahub-server did not become ready:\n{logs[-4000:]}')
        time.sleep(0.5)
    plugins_resp = json.loads(
        urllib.request.urlopen(f'http://127.0.0.1:{http_port}/api/v1/plugins', timeout=5).read()
    )
    return {item['kind']: item for item in plugins_resp['plugins']}


def stop_server(compose_cmd, env):
    cid = out(compose_cmd + ['ps', '-q', 'datahub-server'], env=env)
    assert cid, 'datahub-server container missing'
    run(compose_cmd + ['kill', '--signal', 'SIGTERM', 'datahub-server'], env=env)
    rc = out(['docker', 'wait', cid])
    assert rc == '0', f'unexpected exit code on SIGTERM: {rc}'
    logs = out(['docker', 'logs', cid], stderr=subprocess.STDOUT)
    assert 'GracefullyStopped' in logs and env['FEDENGINE_PASSWORD'] not in logs
    return logs


def verify_plugin_volume_odbc_and_extensions(volume_name: str, selected_plugins: list[str]) -> None:
    ensure_core_base()
    drivers = ' '.join(f'"{PLUGIN_CHECKS[p]["odbc_driver"]}"' for p in selected_plugins)
    so_paths = ' '.join(f'"{PLUGIN_CHECKS[p]["so_path"]}"' for p in selected_plugins)
    ext_checks = []
    seen_ext = set()
    for p in selected_plugins:
        ext_checks.append(f'test -s "/opt/magicdatahub/plugins/extensions/manifest.d/{p}.json"')
        ext_key = PLUGIN_CHECKS[p]['ext_key']
        if ext_key in seen_ext:
            continue
        seen_ext.add(ext_key)
        ext = SOURCES['extensions'][ext_key]
        ext_checks.append(
            f'echo "{ext["sha256"]}  /opt/magicdatahub/plugins/extensions/{ext["path"]}" | sha256sum -c - >/dev/null'
        )
    ext_script = '; '.join(ext_checks)
    odbc_script = (
        'set -eu; '
        f'{ext_script}; '
        f'for d in {drivers}; do '
        '  odbcinst -q -d -n "$d" >/dev/null; '
        'done; '
        f'for so in {so_paths}; do '
        '  test -s "$so"; '
        '  missing=$(ldd "$so" | awk "/not found/{print}"); '
        '  test -z "$missing" || { echo "missing deps in $so: $missing"; exit 1; }; '
        'done; '
        'echo "selected ODBC drivers and extension checksums verified"'
    )
    run([
        'docker', 'run', '--rm', '--entrypoint', 'sh',
        '--mount', f'type=volume,src={volume_name},dst=/opt/magicdatahub/plugins,readonly',
        CORE_BASE_TAG, '-c', odbc_script,
    ])


def verify_plugins_only_mode(tag_prefix: str, version: str, selected_plugins: list[str]) -> None:
    volume_name = 'mdh-plugin-only-vol-' + uuid.uuid4().hex[:12]
    run(['docker', 'volume', 'create', '--label', 'magicdatahub.disposable=plugin-test', volume_name])
    try:
        for plugin in selected_plugins:
            image = f'{tag_prefix}/datahub-plugin-{plugin}:{version}'
            first = out([
                'docker', 'run', '--rm',
                '--user', '65532:65532',
                '--read-only',
                '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges',
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                '--mount', f'type=volume,src={volume_name},dst=/plugins',
                image,
            ])
            first_json = json.loads(first.splitlines()[-1])
            assert first_json.get('event') == 'PluginInstalled', first

            rerun = out([
                'docker', 'run', '--rm',
                '--user', '65532:65532',
                '--read-only',
                '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges',
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                '--mount', f'type=volume,src={volume_name},dst=/plugins',
                image,
            ])
            rerun_json = json.loads(rerun.splitlines()[-1])
            assert rerun_json.get('event') == 'PluginInstalled', rerun
            assert rerun_json['copied'] == 0 and rerun_json['skipped'] > 0, rerun_json

        verify_plugin_volume_odbc_and_extensions(volume_name, selected_plugins)
        print('test MDH-PLUGINS-001 ... ok', flush=True)
    finally:
        subprocess.run(['docker', 'volume', 'rm', '-f', volume_name], cwd=ROOT, check=False, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag-prefix', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--plugins-only', action='store_true',
                        help='verify only Init Container plugins + core ODBC/ldd compatibility without core compose')
    parser.add_argument('--plugins', default=','.join(ALL_PLUGINS),
                        help='comma-separated plugin subset to verify')
    args = parser.parse_args()
    selected_plugins = parse_plugins(args.plugins)

    if args.plugins_only:
        verify_plugins_only_mode(args.tag_prefix, args.version, selected_plugins)
        return

    project = 'mdh-plugin-check-' + uuid.uuid4().hex[:12]
    compose_file = ROOT / 'deploy/docker-compose.yml'
    compose_cmd = ['docker', 'compose', '--project-name', project, '--file', str(compose_file)]
    pg_port, http_port, mqtt_port = free_port(), free_port(), free_port()
    env = dict(
        os.environ,
        FEDENGINE_PASSWORD=uuid.uuid4().hex,
        MDH_IMAGE_PREFIX=args.tag_prefix,
        MDH_IMAGE_TAG=args.version,
        MDH_BIND_HOST='127.0.0.1',
        MDH_PG_PORT=str(pg_port),
        MDH_HTTP_PORT=str(http_port),
        MDH_MQTT_PORT=str(mqtt_port),
    )
    profile_args = []
    for p in selected_plugins:
        profile_args += ['--profile', p]

    try:
        # 1. Core-only startup (no profiles): postgres/mysql built-in ready, optional plugins absent.
        run(compose_cmd + ['up', '--detach'], env=env)
        services_running = out(compose_cmd + ['ps', '-a', '--format', '{{.Service}}'], env=env).splitlines()
        assert services_running == ['datahub-server'], f'unexpected services without profile: {services_running}'
        plugins = wait_ready(compose_cmd, env, pg_port, http_port, mqtt_port)
        for kind in ('postgres', 'greenplum', 'kingbasees', 'mysql', 'mariadb', 'starrocks'):
            assert plugins[kind]['ready'] is True, plugins[kind]
            assert plugins[kind]['extension_source'] == 'builtin', plugins[kind]
        for kind in ALL_PLUGINS:
            assert plugins[kind]['ready'] is False, plugins[kind]
            assert plugins[kind]['extension_installed'] is False, plugins[kind]
        stop_server(compose_cmd, env)
        print('core-only bridge deployment check passed', flush=True)

        # 2. Selected-plugin startup: Init Containers install idempotently.
        run(compose_cmd + profile_args + ['up', '--detach', '--force-recreate'], env=env)
        for plugin in selected_plugins:
            cid = out(compose_cmd + profile_args + ['ps', '-a', '-q', f'plugin-{plugin}'], env=env)
            assert cid, f'missing init container for {plugin}'
            status = out(['docker', 'inspect', '--format', '{{.State.Status}}:{{.State.ExitCode}}', cid])
            assert status == 'exited:0', f'plugin-{plugin} failed: {status}'
            plogs = out(['docker', 'logs', cid], stderr=subprocess.STDOUT)
            assert '"event":"PluginInstalled"' in plogs, plogs

        # Verify idempotent rerun skips unchanged files (copied == 0).
        rerun_plugin = selected_plugins[0]
        rerun = out(
            compose_cmd + ['--profile', rerun_plugin, 'run', '--rm', '--no-deps', f'plugin-{rerun_plugin}'],
            env=env,
        )
        rerun_json = json.loads(rerun.splitlines()[-1])
        assert rerun_json['copied'] == 0 and rerun_json['skipped'] > 0, rerun_json

        plugins_all = wait_ready(compose_cmd, env, pg_port, http_port, mqtt_port)
        for kind in ('postgres', 'greenplum', 'kingbasees', 'mysql', 'mariadb', 'starrocks'):
            assert plugins_all[kind]['ready'] is True, plugins_all[kind]
            assert plugins_all[kind]['extension_source'] == 'builtin', plugins_all[kind]
        for kind in selected_plugins:
            assert plugins_all[kind]['ready'] is True, plugins_all[kind]
            assert plugins_all[kind]['extension_source'] == 'plugin', plugins_all[kind]
            assert plugins_all[kind]['odbc_driver_installed'] is True, plugins_all[kind]

        logs = stop_server(compose_cmd, env)
        assert 'without libodbc.so.2' not in logs, logs

        # 3. Verify odbcinst registration & zero missing dynamic libraries inside core runtime image.
        verify_plugin_volume_odbc_and_extensions(f'{project}_datahub-plugins', selected_plugins)
        print('test MDH-PLUGINS-001 ... ok', flush=True)
    finally:
        subprocess.run(
            compose_cmd + ['--profile', 'all', 'down', '--volumes', '--remove-orphans'],
            cwd=ROOT, env=env, check=False, timeout=60,
        )


if __name__ == '__main__':
    main()
