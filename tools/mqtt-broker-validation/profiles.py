"""Declared target profiles and checked bindings; live configuration stays unverified."""
from pathlib import Path
import json
import tomllib

from common import InputError, ROOT, digest, read_json
from schema_check import check

PROFILE_IDS = {'mqtt-server': 'mdh-mqtt311-standalone', 'datahub-server': 'mdh-mqtt311-combined',
               'reference': 'mosquitto-mqtt311-reference', 'external': 'mqtt311-live-unverified'}
CONFIGS = {'mqtt-server': ['mqtt-development.toml', 'mqtt/standalone.toml'],
           'datahub-server': ['combined-development.toml', 'mqtt/combined.toml', 'fedengine/combined.toml']}


def load_profiles():
    profiles = {}
    for target, identity in PROFILE_IDS.items():
        path = ROOT / 'profiles' / (identity + '.json')
        if path.is_symlink():
            raise InputError('symlinked target profile')
        profile = read_json(path)
        check('profile', profile)
        if profile['id'] != identity or profile['target'] != target or profile['configuration_files'] != CONFIGS.get(target, []):
            raise InputError('profile target/configuration mismatch')
        expected = {'mqtt-server': 'mqtt-only', 'datahub-server': 'mqtt-and-fedengine',
                    'reference': 'reference', 'external': 'external-unverified'}
        if profile['composition'] != expected[target]:
            raise InputError('profile composition mismatch')
        product = target in CONFIGS
        if (profile['failure_policy'] != ('FailFast' if product else 'unverified') or
                profile['storage'] != ('unverified' if target == 'external' else 'memory') or
                profile['extensions_enabled'] != (None if target == 'external' else []) or
                profile['limits'] != ({'max_connections': 64, 'max_sessions': 128} if product else {})):
            raise InputError('unsupported target profile declaration')
        profiles[target] = (profile, path)
    return profiles


def profile_target(target):
    if target == 'tool':
        return None
    name = target.removeprefix('live-')
    return name if name in {'mqtt-server', 'datahub-server', 'reference'} else 'external'


def snapshot_hashes(profile, directory):
    hashes, parsed = {}, {}
    for relative in profile['configuration_files']:
        path = directory / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
            raise InputError('missing or external profile configuration')
        hashes[relative] = digest(path)
        try:
            parsed[relative] = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as error:
            raise InputError('invalid profile configuration') from error
    if profile['target'] in CONFIGS:
        host_file, mqtt_file = profile['configuration_files'][:2]
        host, mqtt = parsed[host_file], parsed[mqtt_file]
        combined = profile['target'] == 'datahub-server'
        expected_host = {'application', 'runtime', 'mqtt'} | ({'fedengine'} if combined else set())
        if set(host) != expected_host or host['mqtt'] != {'config_file': mqtt_file}:
            raise InputError('profile host composition/configuration mismatch')
        if combined and host['fedengine'] != {'config_file': 'fedengine/combined.toml'}:
            raise InputError('profile FedEngine reference mismatch')
        expected = {'core': {'enabled': True, 'listener': '127.0.0.1:0', **profile['limits']},
                    'security': {'anonymous_access': True}}
        if json.dumps(mqtt, sort_keys=True, default=str) != json.dumps({'mqtt': expected}, sort_keys=True):
            raise InputError('effective MQTT configuration disagrees with target profile')
    return hashes


def bind(target, profiles, directory):
    key = profile_target(target)
    if key is None:
        return None
    profile, path = profiles[key]
    live = target.startswith('live-')
    binding = dict(id=profile['id'], version=profile['version'], sha256=digest(path), family=profile['family'],
                   mode='live-network-only' if live else 'owned-instance',
                   configuration_status='unverified' if live else 'snapshot-checked' if key in CONFIGS else 'controlled-reference',
                   config_sha256={} if live or key not in CONFIGS else snapshot_hashes(profile, directory))
    check('profile-binding', binding)
    return binding


def verify_binding(job):
    """Run in both child and parent; never trust a result's self-declared profile."""
    expected = bind(job['target'], load_profiles(), Path(job['config']))
    binding = job.get('profile_binding')
    if binding is not None:
        check('profile-binding', binding)
    if binding != expected:
        raise InputError('target profile binding mismatch')
    if expected is not None:
        if job['case'].get('profile') != expected['family'] or job['case'].get('protocol') != 'mqtt311':
            raise InputError('case protocol/family differs from target profile')
        path = Path(job['profile_file'])
        if path.is_symlink() or not path.is_file() or digest(path) != expected['sha256']:
            raise InputError('profile snapshot digest mismatch')
        check('profile', read_json(path))
    return binding
