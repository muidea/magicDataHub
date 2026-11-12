"""Owned Docker lifecycle with a journal usable after worker interruption."""
import json
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid

from common import command, read_json, write_json


def allocate_resources():
    token = uuid.uuid4().hex
    directory = Path(tempfile.mkdtemp(prefix='mqtt-broker-instance-'))
    directory.chmod(0o755)
    write_json(directory / '.owner.json', {'token': token})
    return dict(token=token, name='mqtt-validation-' + token[:20],
                volume='mqtt-validation-' + token[:20] + '-state', directory=str(directory))


def inspect_optional(kind, name):
    arguments = ['docker', 'volume', 'inspect', name] if kind == 'volume' else ['docker', 'inspect', '--type', 'container', name]
    result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
    if result.returncode:
        if 'no such' in result.stderr.lower():
            return None
        raise RuntimeError('cannot inspect owned Docker resource')
    return json.loads(result.stdout)[0]


def cleanup(resources, trace=None, *, normal=False, kind=None, endpoint=None):
    """Only exact journaled names whose Docker ownership labels match are mutated."""
    errors, forced, resources_found = [], False, False
    old = {}
    # Let cleanup finish within its own bounded commands after the first interrupt.
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, signal.SIG_IGN)
    try:
        container = inspect_optional('container', resources['name'])
        if container:
            resources_found = True
            if container['Config'].get('Labels', {}).get('mqtt-validation.owner') != resources['token']:
                raise RuntimeError('container ownership mismatch; resource preserved')
            try:
                if container['State']['Running']:
                    command('docker', 'kill', '--signal', 'SIGTERM', resources['name'], timeout=10)
                code = command('docker', 'wait', resources['name'], timeout=35).strip()
                logs = command('docker', 'logs', resources['name'], timeout=10)
                if trace:
                    trace.event('host-stop', exit_code=code, logs=logs)
                if normal and (code != '0' or kind != 'reference' and 'GracefullyStopped' not in logs):
                    errors.append('host did not complete successful checked shutdown')
                if normal and endpoint:
                    with socket.socket() as listener:
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        listener.bind(tuple(endpoint))
                command('docker', 'rm', '--volumes', resources['name'], timeout=10)
            except Exception as error:
                errors.append(type(error).__name__ + ': bounded host cleanup failed')
                forced = True
                command('docker', 'rm', '--force', '--volumes', resources['name'], timeout=10)
        volume = inspect_optional('volume', resources['volume'])
        if volume:
            resources_found = True
            if volume.get('Labels', {}).get('mqtt-validation.owner') != resources['token']:
                raise RuntimeError('volume ownership mismatch; resource preserved')
            command('docker', 'volume', 'rm', resources['volume'], timeout=10)
        directory = Path(resources['directory'])
        if directory.exists():
            if (directory.is_symlink() or directory.parent.resolve() != Path(tempfile.gettempdir()).resolve()
                    or not directory.name.startswith('mqtt-broker-instance-')
                    or read_json(directory / '.owner.json') != {'token': resources['token']}):
                raise RuntimeError('temporary directory ownership mismatch; preserved')
            shutil.rmtree(directory)
    except Exception as error:
        errors.append(str(error))
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    result = dict(complete=not errors, forced=forced, errors=errors, resources_found=resources_found)
    if trace:
        trace.event('cleanup', **result)
    return result


class Instance:
    def __init__(self, image, kind, configuration, resources, trace):
        self.image, self.kind = image, kind
        self.configuration, self.resources, self.trace = configuration, resources, trace
        self.endpoint = None
        self.config_digest = None

    def __enter__(self):
        directory = Path(self.resources['directory'])
        arguments = ['docker', 'run', '--detach', '--name', self.resources['name'],
                     '--label', 'mqtt-validation.owner=' + self.resources['token'],
                     '--network', 'host', '--read-only', '--cap-drop', 'ALL',
                     '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m']
        try:
            if self.kind == 'reference':
                with socket.socket() as stream:
                    stream.bind(('127.0.0.1', 0))
                    self.endpoint = ('127.0.0.1', stream.getsockname()[1])
                config = directory / 'mosquitto.conf'
                config.write_text(f'listener {self.endpoint[1]} 127.0.0.1\nallow_anonymous true\npersistence false\n')
                arguments += ['--user', '1883:1883', '--entrypoint', '/usr/sbin/mosquitto',
                              '--tmpfs', '/mosquitto/data:rw,nosuid,nodev,size=16m,uid=1883,gid=1883',
                              '--tmpfs', '/mosquitto/log:rw,nosuid,nodev,size=16m,uid=1883,gid=1883',
                              '--mount', f'type=bind,src={config},dst=/mosquitto/config/mosquitto.conf,readonly',
                              self.image, '-c', '/mosquitto/config/mosquitto.conf']
            else:
                config = directory / 'config'
                shutil.copytree(self.configuration, config)
                command('docker', 'volume', 'create', '--label', 'mqtt-validation.owner=' + self.resources['token'],
                        self.resources['volume'], timeout=10)
                profile = 'mqtt-development.toml' if self.kind == 'mqtt-server' else 'combined-development.toml'
                arguments += ['--mount', f'type=bind,src={config},dst=/opt/magicdatahub/config,readonly',
                              '--mount', f'type=volume,src={self.resources["volume"]},dst=/opt/magicdatahub/var',
                              '--env', 'FEDENGINE_PASSWORD=mqtt-validation-disposable-only',
                              self.image, '--config', '/opt/magicdatahub/config/' + profile]
            command(*arguments, timeout=20)
            ready, deadline = False, time.monotonic() + 30
            while time.monotonic() < deadline:
                logs = command('docker', 'logs', self.resources['name'], timeout=5)
                if self.kind == 'reference':
                    ready = 'mosquitto version' in logs and ' running' in logs
                else:
                    for line in logs.splitlines():
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if event.get('event') == 'Ready':
                            address = next(x['address'] for x in event['endpoints'] if x['protocol'] == 'mqtt')
                            host, port = address.rsplit(':', 1)
                            self.endpoint, ready = (host, int(port)), True
                            self.trace.event('host-ready', endpoints=event['endpoints'])
                if ready:
                    break
                state = inspect_optional('container', self.resources['name'])
                if state is None or not state['State']['Running']:
                    raise RuntimeError('test container exited before Ready')
                time.sleep(0.05)
            if not ready:
                raise TimeoutError('test container Ready timeout')
            with socket.create_connection(self.endpoint, timeout=3):
                pass
            identity = inspect_optional('container', self.resources['name'])
            self.trace.event('target', container=self.resources['name'], image_id=identity['Image'],
                             endpoint=list(self.endpoint), profile=self.kind,
                             readonly=identity['HostConfig']['ReadonlyRootfs'], user=identity['Config']['User'])
            return self
        except BaseException:
            cleanup(self.resources, self.trace)
            raise

    def __exit__(self, *_):
        result = cleanup(self.resources, self.trace, normal=True, kind=self.kind, endpoint=self.endpoint)
        if not result['complete']:
            raise RuntimeError('owned instance cleanup failed: ' + '; '.join(result['errors']))
