"""Validated dependency identities and minimal, public development configuration."""
import hashlib
import re
from pathlib import Path
import tomllib

from common import InputError, ROOT, check_upstream, command, digest, read_json
from prepare import wheel_members

CONFIG_FILES = ('mqtt-development.toml', 'mqtt/standalone.toml',
                'combined-development.toml', 'mqtt/combined.toml', 'fedengine/combined.toml')


def dependencies(path, lock, upstream=None):
    value = read_json(path)
    cache = path.resolve().parent
    if not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('lock_sha256') != digest(ROOT / 'upstream.lock.json'):
        raise InputError('dependency manifest does not match lock; run prepare.py')
    try:
        checkout = Path(value['upstream'])
        if upstream is not None and upstream.resolve() != checkout.resolve():
            raise InputError('--upstream disagrees with dependency manifest')
        check_upstream(checkout, lock)
        wheel = cache / lock['paho']['filename']
        if digest(wheel) != lock['paho']['sha256']:
            raise InputError('Paho artifact digest mismatch')
        with wheel_members(wheel.read_bytes()) as archive:
            expected = {x.filename: hashlib.sha256(archive.read(x)).hexdigest()
                        for x in archive.infolist() if not x.is_dir()}
        if expected != value['paho_files']:
            raise InputError('dependency manifest has altered Paho inventory')
        launcher = Path(value['python_launcher'])
        site = Path(command(str(launcher), '-I', '-c', 'import sysconfig; print(sysconfig.get_paths()["purelib"])').strip())
        if site != Path(value['site_packages']):
            raise InputError('dependency Python site-packages mismatch')
        for relative, sha in expected.items():
            candidate = site / relative
            if candidate.is_symlink() or not candidate.is_file() or digest(candidate) != sha:
                raise InputError('installed Paho digest mismatch: ' + relative)
        command(str(launcher), '-I', '-c', 'import paho.mqtt.client; import importlib.metadata; '
                'assert importlib.metadata.version("paho-mqtt") == "2.1.0"')
    except (KeyError, TypeError, OSError) as error:
        raise InputError('incomplete or unavailable dependency input') from error
    return value


def config_inputs(project):
    """Only five public profiles; reject actual sources and non-development endpoints."""
    configs, identity = {}, {}
    for relative in CONFIG_FILES:
        path = project / 'config' / relative
        if path.is_symlink() or not path.is_file():
            raise InputError('missing public config: ' + relative)
        body = path.read_text()
        try:
            parsed = tomllib.loads(body)
        except tomllib.TOMLDecodeError as error:
            raise InputError('invalid public TOML config') from error
        if relative.startswith('mqtt/'):
            core = parsed.get('mqtt', {}).get('core', {})
            if not core.get('enabled') or parsed.get('mqtt', {}).get('security', {}).get('anonymous_access') is not True:
                raise InputError('MQTT profile requires enabled anonymous development listener')
        if relative.startswith('fedengine/'):
            if parsed.get('sources') or parsed.get('datasources') or parsed.get('data_sources'):
                raise InputError('validation config must have no external sources')
            if parsed.get('duckdb', {}).get('install_extensions') is not False:
                raise InputError('validation must not install extensions')
            if parsed.get('users') != [{'name': 'fedengine', 'password_secret_ref': 'FEDENGINE_PASSWORD'}]:
                raise InputError('expected public SecretRef-only account')
        # Fail closed on newly introduced credentials, files, endpoints or source sections.
        allowed = {
            'mqtt-development.toml': {'application', 'runtime', 'mqtt'},
            'combined-development.toml': {'application', 'runtime', 'mqtt', 'fedengine'},
            'mqtt/standalone.toml': {'mqtt'}, 'mqtt/combined.toml': {'mqtt'},
            'fedengine/combined.toml': {'application', 'pgwire', 'duckdb', 'management', 'ddl', 'shutdown', 'users', 'virtual_databases'},
        }[relative]
        if set(parsed) - allowed:
            raise InputError('unreviewed config sections: ' + relative)
        def inspect(node):
            if isinstance(node, dict):
                for key, val in node.items():
                    if key in {'password', 'uri', 'dsn', 'host', 'source', 'sources', 'datasources'}:
                        raise InputError('unreviewed external config field: ' + key)
                    if key in {'listener', 'listen'} and val not in {'127.0.0.1:1883', '127.0.0.1:2883', '127.0.0.1:25432', '127.0.0.1:25433'}:
                        raise InputError('unexpected development endpoint')
                    if key == 'config_file' and val not in {'mqtt/standalone.toml', 'mqtt/combined.toml', 'fedengine/combined.toml'}:
                        raise InputError('unreviewed nested config')
                    inspect(val)
            elif isinstance(node, list):
                for val in node:
                    inspect(val)
        inspect(parsed)
        effective = re.sub(r'127\.0\.0\.1:(1883|2883|25432|25433)"', '127.0.0.1:0"', body)
        configs[relative] = effective
        identity[relative] = {'source_sha256': digest(path), 'effective_sha256': hashlib.sha256(effective.encode()).hexdigest()}
    git = command('git', '-C', str(project), 'rev-parse', 'HEAD').strip()
    dirty = command('git', '-C', str(project), 'status', '--porcelain', '--untracked-files=all').splitlines()
    return configs, {'revision': git, 'dirty_paths': dirty, 'configs': identity}


def immutable_image(value):
    if not re.fullmatch(r'(?:sha256:[0-9a-f]{64}|[a-zA-Z0-9./_:-]+@sha256:[0-9a-f]{64})', value):
        raise InputError('image requires immutable sha256 ID or registry digest')
    return value
