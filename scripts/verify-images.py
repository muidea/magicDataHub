#!/usr/bin/env python3
"""Run the three local images non-root/read-only with disposable volumes and sources."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import uuid

from acceptance.fixtures import Sources

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('federation', Path(__file__).with_name('verify-federation.py'))
federation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(federation)
smoke = federation.smoke


def run(arguments, **kwargs):
    return subprocess.check_output(arguments, text=True, timeout=60, stderr=subprocess.PIPE, **kwargs).strip()


def inspect_image(image, directory, service):
    config = json.loads(run(['docker', 'image', 'inspect', image]))[0]['Config']
    assert config['User'] == '65532:65532'
    assert 'FEDENGINE_PASSWORD' not in '\n'.join(config['Env'])
    name = run(['docker', 'create', image, '--version'])
    try:
        run(['docker', 'cp', name + ':/opt/magicdatahub/provenance.json', str(directory / 'provenance.json')])
        provenance = json.loads((directory / 'provenance.json').read_text())
        assert provenance['service'] == service and provenance['framework_version'] == '0.0.3'
        assert provenance['library_identity'] == json.loads((ROOT / 'scripts/source-identity.json').read_text())
        if service == 'mqtt-server':
            assert provenance['native_files'] == {}
            missing = subprocess.run(['docker', 'cp', name + ':/opt/magicdatahub/lib/libduckdb.so', str(directory / 'unexpected.so')], capture_output=True)
            assert missing.returncode != 0
        else:
            pinned = json.loads((ROOT / 'docker/runtime.lock.json').read_text())
            assert provenance['native_files']['lib/libduckdb.so'] == pinned['libduckdb_sha256']
        return provenance
    finally:
        run(['docker', 'rm', name])


class Container:
    def __init__(self, image, config, volume, environment):
        self.name = 'mdh-image-run-' + uuid.uuid4().hex[:12]
        command = ['docker', 'run', '--rm', '--name', self.name, '--network', 'host', '--read-only',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
                   '--label', 'magicdatahub.disposable=image-test',
                   '--mount', f'type=bind,src={config.resolve()},dst=/opt/magicdatahub/config,readonly']
        if volume:
            command += ['--mount', f'type=volume,src={volume},dst=/opt/magicdatahub/var']
        for key in ['FEDENGINE_PASSWORD', 'MDH_READER_PASSWORD', 'MDH_TEST_PG_DSN', 'MDH_TEST_MYSQL_DSN']:
            if key in environment:
                command += ['--env', key + '=' + environment[key]]
        self.host = smoke.Process(None, None, '/', os.environ.copy(), command=command + [image])

    def ready(self):
        return self.host.ready()

    def stop(self, sig):
        self.host.stop(sig)

    def close(self):
        # Release a failed test's container before terminating the attached CLI.
        removed = subprocess.run(['docker', 'rm', '--force', self.name], capture_output=True, text=True, timeout=30)
        self.host.close()
        assert removed.returncode == 0 or 'No such container' in removed.stderr, 'owned container cleanup failed'


def readable(directory):
    os.chmod(directory, 0o755)
    for path in directory.rglob('*'):
        os.chmod(path, 0o755 if path.is_dir() else 0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag-prefix', required=True)
    parser.add_argument('--profile', choices=['dev', 'release'], default='dev')
    parser.add_argument('--tag', help='image tag override (defaults to --profile)')
    parser.add_argument('--service', choices=['mqtt-server', 'fedengine-server', 'datahub-server'], help='diagnostic subset; emits no full-image success marker')
    args = parser.parse_args()
    tag_name = args.tag or args.profile
    assert re.fullmatch(r'[a-z0-9]+(?:[._/-][a-z0-9]+)*', args.tag_prefix), 'invalid image prefix'
    assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', tag_name), 'invalid image tag'
    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    with Sources() as sources:
        for service in ([args.service] if args.service else ['mqtt-server', 'fedengine-server', 'datahub-server']):
            image = args.tag_prefix + '/' + service + ':' + tag_name
            with tempfile.TemporaryDirectory(prefix='mdh-image-probe-') as temporary:
                directory = Path(temporary)
                provenance = inspect_image(image, directory, service)
                assert provenance['profile'] == args.profile, 'image tag/profile provenance mismatch'
                config_dir = directory / 'config'
                config_dir.mkdir()
                volume = None
                env = dict(sources.environment(), FEDENGINE_PASSWORD=smoke.PASSWORD, MDH_READER_PASSWORD=smoke.PASSWORD)
                if service == 'mqtt-server':
                    (config_dir / 'host.toml').write_text('[application]\nname="image-mqtt"\n[mqtt]\nconfig_file="mqtt.toml"\n')
                    (config_dir / 'mqtt.toml').write_text('[mqtt.core]\nenabled=true\nlistener="127.0.0.1:0"\nmax_connections=2\nmax_sessions=4\n[mqtt.security]\nanonymous_access=true\n')
                else:
                    federation.configuration(config_dir, service == 'datahub-server', Path('/opt/magicdatahub/extensions'))
                    file = config_dir / 'fedengine.toml'
                    file.write_text(file.read_text().replace('state/', '/opt/magicdatahub/var/instance/'))
                    volume = 'mdh-image-state-' + uuid.uuid4().hex[:12]
                    run(['docker', 'volume', 'create', '--label', 'magicdatahub.disposable=image-test', volume])
                try:
                    # Bind directory root is visible to UID65532; credentials remain in ephemeral env.
                    readable(directory)
                    container = Container(image, config_dir, volume, env)
                    blocked = None
                    try:
                        listeners = container.ready()
                        probe = ['python3', str(ROOT / 'scripts/probe-service.py')]
                        for protocol, endpoint in listeners.items():
                            probe += ['--' + protocol, endpoint[0] + ':' + str(endpoint[1])]
                        subprocess.run(probe, env=dict(os.environ, FEDENGINE_PASSWORD=smoke.PASSWORD), check=True, timeout=60)
                        if service == 'mqtt-server':
                            federation.mqtt.verify(listeners['mqtt'])
                            container.stop(signal.SIGTERM)
                        else:
                            with (federation.MQTTLoad(listeners['mqtt']) if service == 'datahub-server' else federation.NoMQTT()) as load:
                                federation.protocol(listeners['pgwire'])
                                receipt = federation.management(listeners['management'])
                                federation.crud(listeners, sources, load)
                                federation.source_cas(listeners['management'])
                                federation.portal_drain(listeners, load)
                                blocked = federation.blocked_write(listeners, sources, load)
                            federation.slow_io_shutdown(container.host, listeners, sources, blocked)
                    except BaseException:
                        diagnostics = ''.join(container.host.lines)[-6000:]
                        for value in [smoke.PASSWORD, federation.SOURCE_PASSWORD, *sources.environment().values()]:
                            diagnostics = diagnostics.replace(value, '[redacted]')
                        print(diagnostics, flush=True)
                        raise
                    finally:
                        try:
                            if blocked is not None and blocked.stream.fileno() != -1:
                                try:
                                    blocked.query('SELECT pg_advisory_unlock(11011011)')
                                finally:
                                    blocked.close()
                        finally:
                            container.close()
                    if volume:
                        container = Container(image, config_dir, volume, env)
                        try:
                            reopened = container.ready()
                            current = federation.get(reopened['management'], 'virtual-databases/api_owned')
                            assert current['oid'] == receipt['oid'] and current['revision'] == receipt['revision']
                            with federation.Client(reopened['pgwire'], database='pg_crm') as pg:
                                assert pg.query('SELECT value FROM public.mdh_items WHERE id=1') == [['20']]
                                pg.query('DROP TABLE public.mdh_items')
                            container.stop(signal.SIGINT)
                        finally:
                            container.close()
                    print(json.dumps({'image': image, 'status': 'Passed', 'profile': provenance['profile'],
                                      'non_root': True, 'read_only_root': True, 'clean_git_inputs': provenance['clean_git_inputs']}), flush=True)
                finally:
                    if volume:
                        run(['docker', 'volume', 'rm', volume])
    if not args.service:
        print('test MDH-IMAGES-001 ... ok', flush=True)


if __name__ == '__main__':
    main()
