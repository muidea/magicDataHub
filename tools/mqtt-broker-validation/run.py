#!/usr/bin/env python3
"""Bounded, process-isolated MQTT3.1.1 validation; full conformance stays Partial."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

from common import InputError, ROOT, command, digest, load_catalog, read_json, source_identity, write_json
from inputs import config_inputs, dependencies, immutable_image
from execution_contracts import check_manifest, check_request, execution_hash, input_identity
from lifecycle import allocate_resources, cleanup
from profiles import bind, load_profiles, profile_target
from reporting import validate_result, write_reports
from schema_check import load_schema
from upstream_audit import load_assessment
from normative_map import load_mapping


class Cancellation:
    def __init__(self):
        self.signum = None

    def __call__(self, signum, _frame):
        self.signum = signum


def supervise(arguments, log, timeout, grace, cancellation):
    """Terminate the worker first, allowing its finally blocks; kill its group last."""
    started = time.monotonic()
    interrupted, timed_out, terminate_at = False, False, None
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    with Path(log).open('w') as output:
        process = subprocess.Popen(arguments, stdout=output, stderr=subprocess.STDOUT,
                                   env=environment, start_new_session=True)
        try:
            while process.poll() is None:
                now = time.monotonic()
                if terminate_at is None and (cancellation.signum or now - started >= timeout):
                    interrupted, timed_out = bool(cancellation.signum), not bool(cancellation.signum)
                    process.terminate()
                    terminate_at = now
                if terminate_at is not None and now - terminate_at >= grace:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
    return dict(returncode=process.returncode, timed_out=timed_out, interrupted=interrupted,
                seconds=time.monotonic() - started)


def endpoint(value):
    try:
        name, address = value.split('=', 1)
        host, port = address.rsplit(':', 1)
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,40}', name) or host != '127.0.0.1' or not 1 <= int(port) <= 65535:
            raise ValueError()
        return 'live-' + name, [host, int(port)]
    except ValueError as error:
        raise argparse.ArgumentTypeError('use NAME=127.0.0.1:PORT') from error


def csv(value):
    items = value.split(',')
    if not all(items) or len(set(items)) != len(items):
        raise InputError('selection must be a nonempty list without duplicates')
    return items


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--check', action='store_true', help='validate catalog/lock structure without Docker or dependencies')
    result.add_argument('--list', action='store_true', help='list stable case IDs without running')
    result.add_argument('--catalog', type=Path, default=ROOT / 'catalog.json')
    result.add_argument('--dependencies', type=Path, default=Path.home() / '.cache/mqtt-broker-validation/dependencies.json')
    result.add_argument('--upstream', type=Path, help='optional consistency check against dependency manifest')
    result.add_argument('--host-project', type=Path)
    result.add_argument('--mqtt-image')
    result.add_argument('--combined-image')
    result.add_argument('--reference-image')
    result.add_argument('--output', type=Path)
    result.add_argument('--live', action='append', type=endpoint, default=[])
    result.add_argument('--targets', default='reference,mqtt-server,datahub-server', help='tool calibration is always included')
    result.add_argument('--cases', help='comma separated stable IDs; tool calibration always included')
    result.add_argument('--required-targets', help='defaults to all selected targets; tool/live targets always required')
    result.add_argument('--case-timeout', type=float, default=90)
    result.add_argument('--cleanup-grace', type=float, default=80)
    result.add_argument('--require-conformance', action='store_true', help='Partial cannot satisfy this gate')
    return result


def execute(args):
    lock = read_json(ROOT / 'upstream.lock.json')
    catalog = load_catalog(args.catalog, lock)
    load_assessment(catalog=catalog, lock=lock)
    load_mapping(catalog=catalog)
    profiles = load_profiles()
    for name in ['profile', 'profile-binding', 'event', 'worker-result', 'summary', 'case', 'obligation', 'catalog', 'manifest', 'request']:
        load_schema(name)
    if args.check or args.list:
        if args.list:
            for case in catalog['cases']:
                print(case['id'] + '  ' + case['name'])
        else:
            print(f'Catalog valid: {len(catalog["cases"])} cases, {len(catalog["obligations"])} internal evidence scopes; {len(profiles)} declared profiles; Partial.')
        return 0
    if not 0 < args.case_timeout <= 3600 or not 1 <= args.cleanup_grace <= 180:
        raise InputError('invalid positive timeout/cleanup budget')
    if args.output is None or args.output.exists() or args.output.is_symlink():
        raise InputError('a new --output directory is required')
    selected = csv(args.targets)
    if not set(selected) <= {'tool', 'reference', 'mqtt-server', 'datahub-server'}:
        raise InputError('unknown isolated target')
    live = dict(args.live)
    if len(live) != len(args.live):
        raise InputError('duplicate live target')
    targets = ['tool'] + [x for x in selected if x != 'tool'] + list(live)
    required = csv(args.required_targets) if args.required_targets else targets.copy()
    if not set(required) <= set(targets):
        raise InputError('required target was not selected')
    required = list(dict.fromkeys(['tool'] + required + list(live)))
    ids = csv(args.cases) if args.cases else [x['id'] for x in catalog['cases']]
    if not set(ids) <= {x['id'] for x in catalog['cases']}:
        raise InputError('unknown case selection')
    chosen = [x for x in catalog['cases'] if x['id'] in ids and x['runner'] != 'calibration']
    if any(x != 'tool' for x in selected) and not chosen:
        raise InputError('isolated target has no selected protocol cases')
    isolated = [x for x in targets if x in {'reference', 'mqtt-server', 'datahub-server'}]
    dep = dependencies(args.dependencies, lock, args.upstream) if isolated or live else None
    python = dep['python_launcher'] if dep else sys.executable
    images, image_ids = {}, {}
    for target, supplied in [('reference', args.reference_image or lock['reference_image']),
                              ('mqtt-server', args.mqtt_image), ('datahub-server', args.combined_image)]:
        if target not in isolated:
            continue
        if supplied is None:
            raise InputError('missing image for ' + target)
        images[target] = immutable_image(supplied)
        if target == 'reference' and supplied != lock['reference_image']:
            raise InputError('reference image must match upstream.lock.json')
        image_ids[target] = json.loads(command('docker', 'image', 'inspect', supplied))[0]['Id']
    configs, host = {}, None
    if set(isolated) & {'mqtt-server', 'datahub-server'}:
        if args.host_project is None:
            raise InputError('--host-project required for magicDataHub images')
        configs, host = config_inputs(args.host_project.resolve())
    output = args.output.resolve()
    output.mkdir(parents=True)
    config_dir = output / 'input/config'
    config_dir.mkdir(parents=True)
    for relative, body in configs.items():
        path = config_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    write_json(output / 'input/catalog.json', catalog)
    write_json(output / 'input/upstream.lock.json', lock)
    profile_dir = output / 'input/profiles'
    profile_dir.mkdir()
    for profile, path in profiles.values():
        (profile_dir / path.name).write_bytes(path.read_bytes())
    target_profiles = {target: bind(target, profiles, config_dir) for target in targets}
    source = source_identity(ROOT)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:12]
    calibration = next(x for x in catalog['cases'] if x['runner'] == 'calibration')
    plan = [('tool', calibration)]
    plan += [(target, case) for target in isolated for case in chosen]
    if live:
        paho_cases = [x for x in catalog['cases'] if x.get('entrypoint') == 'paho' and x.get('live_safe')]
        if len(paho_cases) != 1:
            raise InputError('live validation requires exactly one catalogued safe Paho scenario')
        plan += [(target, paho_cases[0]) for target in live]
    plan_identity = [{'target': target, 'case_id': case['id']} for target, case in plan]
    manifest = dict(schema_version=1, run_id=run_id, created_utc=datetime.now(timezone.utc).isoformat(),
             python_version=sys.version, worker_python=python, profile='mqtt311-tcp-anonymous-memory',
             target_profiles=target_profiles,
             source_files=source, input_files=input_identity(output),
             catalog_sha256=digest(args.catalog), lock_sha256=digest(ROOT / 'upstream.lock.json'),
             dependencies_sha256=digest(args.dependencies) if dep else None, upstream_commit=lock['mosquitto']['commit'],
             upstream_path=dep['upstream'] if dep else None,
             paho_version=lock['paho']['version'] if dep else None, host_project=host,
             images={t: {'requested': images[t], 'actual_id': image_ids[t]} for t in isolated}, live_endpoints=live,
             required_targets=required, case_timeout_seconds=args.case_timeout, cleanup_grace_seconds=args.cleanup_grace,
             require_conformance=args.require_conformance, plan=plan_identity,
             plan_sha256=hashlib.sha256(json.dumps(plan_identity, sort_keys=True).encode()).hexdigest(),
             interrupted=False, cleanup_incomplete=False, input_changed=False)
    manifest['execution_sha256'] = execution_hash(manifest)
    def save_manifest():
        check_manifest(manifest, catalog)
        write_json(output / 'manifest.json', manifest)
    save_manifest()
    cancellation, old, records = Cancellation(), {}, []
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, cancellation)
    try:
        for index, (target, case) in enumerate(plan):
            if cancellation.signum or manifest['cleanup_incomplete']:
                break
            stem = f'{index:03d}-{target}-{case["id"]}'
            job = dict(schema_version=1, manifest=str(output / 'manifest.json'), plan_index=index,
                       execution_sha256=manifest['execution_sha256'],
                       run_id=run_id, attempt_id=uuid.uuid4().hex, target=target, case=case,
                       trace=str(output / (stem + '.jsonl')), log=str(output / (stem + '.log')),
                       result=str(output / (stem + '.result.json')), config=str(config_dir),
                       upstream=dep['upstream'] if dep else None, endpoint=live.get(target),
                       image=images.get(target), image_id=image_ids.get(target), resources=None)
            job['profile_binding'] = target_profiles[target]
            key = profile_target(target)
            job['profile_file'] = str(profile_dir / profiles[key][1].name) if key is not None else None
            if case['runner'] == 'upstream':
                job['upstream_sha256'] = lock['mosquitto']['files']['test/broker/' + case['upstream']['file']]
            if target in isolated:
                job['resources'] = allocate_resources()
            request = output / (stem + '.request.json')
            fallback = None
            try:
                check_request(job, request, before_execution=True)
                # Write the ownership journal before the child may create resources.
                write_json(request, job)
                process = supervise([python, '-B', str(ROOT / 'worker.py'), str(request)],
                                    output / (stem + '.worker.log'), args.case_timeout, args.cleanup_grace, cancellation)
                record = validate_result(output, job, process['returncode'])
                if process['timed_out'] or process['interrupted']:
                    record.update(status='ERROR', evidence_validated=False,
                                  error='case deadline exceeded' if process['timed_out'] else 'run interrupted')
                record['process'] = process
            except Exception as error:
                record = dict(run_id=run_id, target=target, case_id=case['id'], attempt_id=job['attempt_id'],
                              status='ERROR', error=type(error).__name__ + ': ' + str(error), seconds=0, evidence_validated=False)
            finally:
                if job['resources'] is not None:
                    fallback = cleanup(job['resources'])
                    # Recheck exact names after the worker/CLI group has ended.
                    if not fallback['complete'] or fallback['forced']:
                        manifest['cleanup_incomplete'] = True
            if record.get('cleanup_incomplete'):
                manifest['cleanup_incomplete'] = True
            if fallback is not None:
                record['parent_cleanup'] = fallback
                if fallback.get('resources_found'):
                    record.update(status='ERROR', evidence_validated=False, error='parent had to recover worker-owned Docker resources')
            records.append(record)
            write_json(output / 'results.json', records)
            print(f'{target}: {case["id"]}: {record["status"]}' + (f' ({record["error"]})' if record.get('error') else ''), flush=True)
            manifest['interrupted'] = bool(cancellation.signum)
            save_manifest()
            write_reports(output, manifest, records, catalog)
        manifest['interrupted'] = bool(cancellation.signum)
        try:
            if (source != source_identity(ROOT) or manifest['catalog_sha256'] != digest(args.catalog)
                    or input_identity(output) != manifest['input_files']):
                manifest['input_changed'] = True
            if dep:
                dependencies(args.dependencies, lock, args.upstream)
                if manifest['dependencies_sha256'] != digest(args.dependencies):
                    manifest['input_changed'] = True
        except Exception:
            manifest['input_changed'] = True
        manifest['completed_utc'] = datetime.now(timezone.utc).isoformat()
        save_manifest()
        summary = write_reports(output, manifest, records, catalog)
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    print(json.dumps({k: summary[k] for k in ('full_conformance', 'target_gates', 'counts', 'exit_code')}, ensure_ascii=False))
    return summary['exit_code']


def main():
    args = parser().parse_args()
    try:
        return execute(args)
    except InputError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        print('runner infrastructure failed: ' + type(error).__name__, file=sys.stderr)
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
