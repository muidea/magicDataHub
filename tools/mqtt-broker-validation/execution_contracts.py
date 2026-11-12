"""Bind structural contracts to the selected plan and frozen run inputs.

Hashes detect drift and mismatches; they are not signatures or independent approval.
The parent retains its own ownership journal for cleanup, never a reloaded request.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile

from common import InputError, ROOT, check_upstream, digest, load_catalog, read_json, validate_catalog
from profiles import CONFIGS, PROFILE_IDS, load_profiles, profile_target, verify_binding
from schema_check import check

ISOLATED = {'reference', 'mqtt-server', 'datahub-server'}
PROGRESS = {'execution_sha256', 'interrupted', 'cleanup_incomplete', 'input_changed', 'completed_utc'}


def hash_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def execution_hash(manifest):
    return hash_value({key: value for key, value in manifest.items() if key not in PROGRESS})


def input_identity(output):
    directory = Path(output) / 'input'
    if directory.resolve() != directory:
        raise InputError('noncanonical frozen input directory')
    paths = sorted(directory.rglob('*'))
    if any(path.is_symlink() for path in paths):
        raise InputError('symlink in frozen input inventory')
    return {str(path.relative_to(directory)): digest(path) for path in paths if path.is_file()}


def check_manifest(manifest, catalog):
    check('manifest', manifest)
    lock = read_json(ROOT / 'upstream.lock.json')
    validate_catalog(catalog, lock)
    plan = manifest['plan']
    if manifest['plan_sha256'] != hash_value(plan) or manifest['execution_sha256'] != execution_hash(manifest):
        raise InputError('run plan or execution identity mismatch')
    if manifest['case_timeout_seconds'] <= 0:
        raise InputError('nonpositive case deadline')
    try:
        created = datetime.fromisoformat(manifest['created_utc'])
        if 'completed_utc' in manifest and datetime.fromisoformat(manifest['completed_utc']) < created:
            raise ValueError('completion precedes creation')
    except ValueError as error:
        raise InputError('invalid run timestamp') from error
    cases = {case['id']: case for case in catalog['cases']}
    keys = [(row['target'], row['case_id']) for row in plan]
    if len(set(keys)) != len(keys) or keys[0] != ('tool', 'TOOL-CAL-001'):
        raise InputError('duplicate plan entry or missing leading calibration')
    targets = {target for target, _ in keys}
    isolated = targets & ISOLATED
    live = {target for target in targets if target.startswith('live-')}
    if targets != {'tool'} | isolated | live:
        raise InputError('unknown planned target')
    if set(manifest['target_profiles']) != targets or set(manifest['images']) != isolated or set(manifest['live_endpoints']) != live:
        raise InputError('planned target/profile/image/endpoint sets disagree')
    for image in manifest['images'].values():
        if image['requested'].startswith('sha256:') and image['requested'] != image['actual_id']:
            raise InputError('requested local image ID differs from actual ID')
    if 'reference' in isolated and manifest['images']['reference']['requested'] != lock['reference_image']:
        raise InputError('reference image differs from dependency lock')
    required = set(manifest['required_targets'])
    if not {'tool'} | live <= required or not required <= targets:
        raise InputError('required target selection differs from plan')
    for target, identity in keys:
        case = cases.get(identity)
        if case is None or (target == 'tool') != (case['runner'] == 'calibration'):
            raise InputError('case does not belong to planned target')
        if target in live and (case.get('entrypoint') != 'paho' or case.get('live_safe') is not True):
            raise InputError('live target requires the safe Paho scenario')
    if any(sum(t == target for t, _ in keys) != 1 for target in live | {'tool'}):
        raise InputError('tool/live target has extra scenarios')
    product = isolated & set(CONFIGS)
    host = manifest['host_project']
    if bool(product) != (host is not None):
        raise InputError('host configuration identity missing or unplanned')
    expected_inputs = {'catalog.json', 'upstream.lock.json'} | {'profiles/' + identity + '.json' for identity in PROFILE_IDS.values()}
    if host is not None:
        config_files = set().union(*CONFIGS.values())
        if set(host['configs']) != config_files:
            raise InputError('host configuration inventory mismatch')
        expected_inputs |= {'config/' + name for name in config_files}
        for name, hashes in host['configs'].items():
            if manifest['input_files'].get('config/' + name) != hashes['effective_sha256']:
                raise InputError('effective configuration digest mismatch')
    if set(manifest['input_files']) != expected_inputs or not manifest['source_files']:
        raise InputError('frozen input inventory mismatch or missing source identity')
    profiles = load_profiles()
    for target, binding in manifest['target_profiles'].items():
        key = profile_target(target)
        if key is None:
            if binding is not None:
                raise InputError('calibration cannot claim a broker profile')
            continue
        profile, path = profiles[key]
        network = target in live
        configs = {} if network or key not in CONFIGS else {name: host['configs'][name]['effective_sha256'] for name in CONFIGS[key]}
        expected = dict(id=profile['id'], version=profile['version'], sha256=digest(path), family=profile['family'],
                        mode='live-network-only' if network else 'owned-instance', config_sha256=configs,
                        configuration_status='unverified' if network else 'snapshot-checked' if key in CONFIGS else 'controlled-reference')
        if binding != expected or manifest['input_files']['profiles/' + path.name] != digest(path):
            raise InputError('manifest target profile/configuration mismatch')
    needs_dependencies = bool(isolated or live)
    if any((manifest[key] is not None) != needs_dependencies for key in ['dependencies_sha256', 'paho_version', 'upstream_path']):
        raise InputError('dependency identity missing or unplanned')
    return manifest


def local_file(path, *, must_exist=True):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise InputError('noncanonical or symlinked run file')
    if must_exist and not path.is_file():
        raise InputError('missing frozen run file')
    return path


def check_request(job, request_path, *, before_execution=False):
    """Read-only checks must finish before Trace, network, or Docker side effects."""
    check('request', job)
    request_path = local_file(request_path, must_exist=False)
    output = request_path.parent
    expected_manifest = output / 'manifest.json'
    if job['manifest'] != str(expected_manifest):
        raise InputError('request manifest is outside its run directory')
    manifest = read_json(local_file(expected_manifest))
    # Check every frozen file, not only the subset used by this case.
    check('manifest', manifest)
    for relative, sha in manifest['input_files'].items():
        path = local_file(output / 'input' / relative)
        if not path.is_relative_to(output / 'input') or digest(path) != sha:
            raise InputError('frozen run input changed')
    if input_identity(output) != manifest['input_files']:
        raise InputError('frozen input inventory changed')
    lock = read_json(output / 'input/upstream.lock.json')
    catalog = load_catalog(output / 'input/catalog.json', lock)
    check_manifest(manifest, catalog)
    if before_execution and ('completed_utc' in manifest or any(manifest[key] for key in ['interrupted', 'cleanup_incomplete', 'input_changed'])):
        raise InputError('run has already completed or stopped')
    if manifest['lock_sha256'] != digest(ROOT / 'upstream.lock.json') or lock != read_json(ROOT / 'upstream.lock.json'):
        raise InputError('run dependency lock mismatch')
    if manifest['upstream_commit'] != lock['mosquitto']['commit'] or manifest['paho_version'] not in (None, lock['paho']['version']):
        raise InputError('run dependency version mismatch')
    if job['run_id'] != manifest['run_id'] or job['execution_sha256'] != manifest['execution_sha256']:
        raise InputError('request belongs to another run or execution plan')
    index = job['plan_index']
    if index >= len(manifest['plan']) or manifest['plan'][index] != {'target': job['target'], 'case_id': job['case']['id']}:
        raise InputError('request is not the indexed plan entry')
    case = next(case for case in catalog['cases'] if case['id'] == job['case']['id'])
    if job['case'] != case:
        raise InputError('request case differs from frozen catalog')
    stem = f'{index:03d}-{job["target"]}-{case["id"]}'
    if request_path.name != stem + '.request.json':
        raise InputError('request filename differs from indexed plan')
    for field, suffix in [('trace', '.jsonl'), ('log', '.log'), ('result', '.result.json')]:
        path = local_file(job[field], must_exist=False)
        if path != output / (stem + suffix):
            raise InputError('request artifact path differs from indexed plan')
        if before_execution and path.exists():
            raise InputError('refusing to overwrite existing execution artifacts')
    target = job['target']
    image = manifest['images'].get(target, {})
    if (job['image'], job['image_id'], job['endpoint']) != (image.get('requested'), image.get('actual_id'), manifest['live_endpoints'].get(target)):
        raise InputError('request image or endpoint differs from plan')
    if job['config'] != str(output / 'input/config') or job['upstream'] != manifest['upstream_path']:
        raise InputError('request configuration/upstream path mismatch')
    key = profile_target(target)
    expected_profile = str(output / 'input/profiles' / (PROFILE_IDS[key] + '.json')) if key else None
    if job['profile_file'] != expected_profile or job['profile_binding'] != manifest['target_profiles'][target]:
        raise InputError('request profile differs from plan')
    verify_binding(job)
    if case['runner'] == 'upstream':
        expected_sha = lock['mosquitto']['files']['test/broker/' + case['upstream']['file']]
        if job.get('upstream_sha256') != expected_sha:
            raise InputError('request upstream digest mismatch')
        check_upstream(Path(job['upstream']), lock)
    elif 'upstream_sha256' in job:
        raise InputError('unexpected upstream case digest')
    resources = job['resources']
    if (resources is not None) != (target in ISOLATED):
        raise InputError('request resource ownership mode differs from target')
    if resources is not None:
        name = 'mqtt-validation-' + resources['token'][:20]
        directory = Path(resources['directory'])
        if resources['name'] != name or resources['volume'] != name + '-state':
            raise InputError('request resource names differ from ownership token')
        if directory.resolve() != directory or directory.is_symlink() or directory.parent != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith('mqtt-broker-instance-'):
            raise InputError('request resource directory outside owned temporary namespace')
        # Normal worker cleanup removes this directory before parent validation.
        if before_execution or directory.exists():
            if read_json(local_file(directory / '.owner.json')) != {'token': resources['token']}:
                raise InputError('request resource directory ownership mismatch')
    return manifest
