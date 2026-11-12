"""Complete versioned run fixtures, without Docker or external dependencies."""
import sys
import uuid

from common import ROOT, digest, load_catalog, read_json, source_identity, write_json
from execution_contracts import execution_hash, hash_value, input_identity
from inputs import config_inputs
from profiles import bind, load_profiles, profile_target

RUN_ID = '20261110T000000Z-0123456789ab'


def fixture(output, targets=('live-unit',)):
    lock = read_json(ROOT / 'upstream.lock.json')
    catalog = load_catalog(ROOT / 'catalog.json', lock)
    profiles = load_profiles()
    config = output / 'input/config'
    config.mkdir(parents=True, exist_ok=True)
    write_json(output / 'input/catalog.json', catalog)
    write_json(output / 'input/upstream.lock.json', lock)
    profile_dir = output / 'input/profiles'
    profile_dir.mkdir(exist_ok=True)
    for _, path in profiles.values():
        (profile_dir / path.name).write_bytes(path.read_bytes())
    host = None
    if set(targets) & {'mqtt-server', 'datahub-server'}:
        configs, host = config_inputs(ROOT.parents[1])
        for name, body in configs.items():
            path = config / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
    calibration = next(case for case in catalog['cases'] if case['runner'] == 'calibration')
    paho = next(case for case in catalog['cases'] if case.get('entrypoint') == 'paho')
    plan = [('tool', calibration)] + [(target, paho) for target in targets if target != 'tool']
    identities = [dict(target=target, case_id=case['id']) for target, case in plan]
    manifest = dict(schema_version=1, run_id=RUN_ID, created_utc='2026-11-10T00:00:00+00:00',
        python_version=sys.version, worker_python=sys.executable, profile='mqtt311-tcp-anonymous-memory',
        target_profiles={target: bind(target, profiles, config) for target, _ in plan},
        source_files=source_identity(ROOT), input_files=input_identity(output), catalog_sha256=digest(ROOT / 'catalog.json'),
        lock_sha256=digest(ROOT / 'upstream.lock.json'), dependencies_sha256='0' * 64 if len(plan) > 1 else None,
        upstream_commit=lock['mosquitto']['commit'], upstream_path='/nonexecuted-test-upstream' if len(plan) > 1 else None,
        paho_version=lock['paho']['version'] if len(plan) > 1 else None, host_project=host,
        images={target: dict(requested=lock['reference_image'] if target == 'reference' else 'sha256:' + '0' * 64, actual_id='sha256:' + '0' * 64)
                for target in targets if not target.startswith('live-') and target != 'tool'},
        live_endpoints={target: ['127.0.0.1', 1883] for target in targets if target.startswith('live-')},
        required_targets=list(dict.fromkeys(target for target, _ in plan)), case_timeout_seconds=90,
        cleanup_grace_seconds=80, require_conformance=False, plan=identities, plan_sha256=hash_value(identities),
        interrupted=False, cleanup_incomplete=False, input_changed=False)
    manifest['execution_sha256'] = execution_hash(manifest)
    write_json(output / 'manifest.json', manifest)
    jobs = []
    for index, (target, case) in enumerate(plan):
        stem = f'{index:03d}-{target}-{case["id"]}'
        key = profile_target(target)
        image = manifest['images'].get(target, {})
        jobs.append(dict(schema_version=1, manifest=str(output / 'manifest.json'), plan_index=index,
            execution_sha256=manifest['execution_sha256'], run_id=RUN_ID, attempt_id=uuid.uuid4().hex,
            target=target, case=case, trace=str(output / (stem + '.jsonl')), log=str(output / (stem + '.log')),
            result=str(output / (stem + '.result.json')), config=str(config), upstream=manifest['upstream_path'],
            endpoint=manifest['live_endpoints'].get(target), image=image.get('requested'), image_id=image.get('actual_id'),
            resources=None, profile_binding=manifest['target_profiles'][target],
            profile_file=str(profile_dir / profiles[key][1].name) if key else None))
    return manifest, catalog, jobs


def resign(manifest):
    manifest['plan_sha256'] = hash_value(manifest['plan'])
    manifest['execution_sha256'] = execution_hash(manifest)
    return manifest
