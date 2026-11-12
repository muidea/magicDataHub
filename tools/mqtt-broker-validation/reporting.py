"""Evidence validation, scoped gates and bounded (never full) protocol claims."""
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from calibration_model import MUTATIONS, RECOVERY_BRANCHES
from common import InputError, decode_json, digest, read_json, write_json
from profiles import verify_binding
from execution_contracts import check_manifest, check_request
from schema_check import check

STATUSES = ('PASS', 'FAIL', 'BLOCKED', 'ERROR', 'NOT_TESTED')


def evidence_events(output, record):
    relative = Path(record['evidence'])
    if relative.is_absolute() or len(relative.parts) != 1:
        raise InputError('evidence path must be an output-local file')
    path = output / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(output.resolve()):
        raise InputError('evidence missing or outside output')
    if path.stat().st_size != record['evidence_bytes'] or digest(path) != record['evidence_sha256']:
        raise InputError('evidence size or hash mismatch')
    events = [decode_json(line) for line in path.read_text().splitlines()]
    if not events or not all(isinstance(x, dict) and isinstance(x.get('kind'), str) for x in events):
        raise InputError('invalid evidence events')
    for event in events:
        check('event', event)
    if [x['sequence'] for x in events] != list(range(len(events))):
        raise InputError('event sequence has gaps, duplicates or reordering')
    times = [x['seconds'] for x in events]
    if times != sorted(times):
        raise InputError('event monotonic time moved backwards')
    for kind in ['case-start', 'case-end']:
        if sum(x['kind'] == kind for x in events) != 1:
            raise InputError('duplicate or missing case boundary')
    return events


def validate_result(output, job, returncode):
    """A worker's PASS is only provisional; missing or contradictory evidence blocks it."""
    base = dict(run_id=job['run_id'], target=job['target'], case_id=job['case']['id'], attempt_id=job['attempt_id'],
                status='ERROR', error=None, seconds=0, evidence_validated=False)
    try:
        check_request(job, Path(job['manifest']).parent / (Path(job['trace']).stem + '.request.json'))
        if output.resolve() != Path(job['manifest']).parent:
            raise InputError('result output differs from planned run directory')
        binding = verify_binding(job)
        record = read_json(job['result'])
        check('worker-result', record)
        if not isinstance(record, dict):
            raise InputError('worker result must be an object')
        if record.get('profile_binding') != binding:
            raise InputError('worker profile binding differs from planned target')
        if record['evidence'] != Path(job['trace']).name:
            raise InputError('worker evidence file differs from indexed plan')
        for key, expected in [('run_id', job['run_id']), ('attempt_id', job['attempt_id']),
                              ('target', job['target']), ('case_id', job['case']['id'])]:
            if record.get(key) != expected:
                raise InputError('worker result identity mismatch')
        status = record.get('status')
        if (status == 'PASS') != (record['error'] is None):
            raise InputError('worker status and error cause disagree')
        if status not in STATUSES[:-1] or returncode != {'PASS': 0, 'FAIL': 1, 'BLOCKED': 3, 'ERROR': 3}.get(status):
            raise InputError('worker status and process exit disagree')
        if not isinstance(record.get('seconds'), (float, int)) or record['seconds'] < 0:
            raise InputError('invalid worker duration')
        events = evidence_events(output, record)
        first, last = events[0], events[-1]
        if first != {**first, 'kind': 'case-start', 'run_id': job['run_id'], 'attempt_id': job['attempt_id'],
                     'target': job['target'], 'case_id': job['case']['id'], 'profile_binding': binding} or last.get('kind') != 'case-end' or last.get('status') != status:
            raise InputError('incomplete or mismatched evidence boundaries')
        checked = [x for x in events if x['kind'] == 'profile-checked']
        if len(checked) != 1 or checked[0].get('binding') != binding:
            raise InputError('missing or mismatched checked profile evidence')
        if job['resources'] is not None:
            cleanups = [x for x in events if x['kind'] == 'cleanup']
            if not cleanups or any(x.get('complete') is not True or x.get('forced') is not False for x in cleanups):
                base['cleanup_incomplete'] = True
                raise InputError('missing or unsuccessful checked instance cleanup')
        if status == 'PASS':
            check_pass(events, job)
        record['evidence_types'] = job['case']['evidence_types'] + (['E-HOST'] if job['resources'] else [])
        record['evidence_validated'] = True
        if status == 'FAIL' and job['case']['runner'] == 'upstream':
            record['classification'] = 'upstream-difference-needs-review'
        return record
    except (InputError, OSError, ValueError, KeyError, TypeError) as error:
        base['error'] = 'evidence rejected: ' + str(error)
        return base


def check_pass(events, job):
    case = job['case']
    if case.get('entrypoint') == 'connect':
        from connection import check_evidence
        check_evidence(events)
    assertions = [x for x in events if x['kind'] in {'assertion', 'upstream-assertion'}]
    passed = {x.get('assertion') for x in assertions if x.get('passed') is True}
    if not set(case['required_assertions']) <= passed:
        raise InputError('required assertions missing: ' + ','.join(sorted(set(case['required_assertions']) - passed)))
    failures = [x for x in assertions if x.get('passed') is not True]
    if case['runner'] == 'calibration':
        for label in ('known-mutation-detected', 'calibration-peer-completed'):
            rows = [x for x in assertions if x.get('assertion') == label and x.get('passed') is True]
            if len(rows) != len(MUTATIONS) or {x.get('mutation') for x in rows} != set(MUTATIONS):
                raise InputError('incomplete error-peer calibration')
        if len(failures) != len(MUTATIONS):
            raise InputError('unexpected calibration failure count')
        for mutation, assertion in MUTATIONS.items():
            rows = [x for x in failures if x.get('mutation') == mutation]
            if len(rows) != 1 or rows[0].get('assertion') != assertion:
                raise InputError('unexpected calibration failure assertions')
    elif failures:
        raise InputError('failed assertion cannot be PASS')
    for direction in ('tx', 'rx'):
        parts = [bytes.fromhex(x.get('hex', '')) for x in events if x['kind'] == 'bytes' and x.get('direction') == direction]
        if not any(parts):
            raise InputError('missing nonempty captured ' + direction + ' bytes')
    for name in case.get('required_events', []):
        if not any(x['kind'] == name for x in events):
            raise InputError('required event missing: ' + name)
    for event in events:
        if event['kind'] == 'observation' and (event.get('completed') is not True
                or event.get('seconds_required', 0) < 0.4
                or event.get('actual_seconds', 0) < event['seconds_required']):
            raise InputError('incomplete bounded negative observation')
    if case.get('entrypoint') in {'topics', 'system_topics'}:
        from topic_evidence import check_evidence
        check_evidence(events, system=case['entrypoint'] == 'system_topics')
    if case.get('topic_vector_count'):
        vectors = [x for x in events if x['kind'] == 'topic-vector']
        if len(vectors) != case['topic_vector_count'] or {x.get('index') for x in vectors} != set(range(case['topic_vector_count'])):
            raise InputError('incomplete topic vector execution')
    if case.get('entrypoint') == 'mixed_qos':
        from mixed_qos_evidence import check_evidence
        check_evidence(events)
    if case.get('entrypoint') == 'qos2_duplex':
        from qos2_duplex_evidence import check_evidence
        check_evidence(events)
    if case.get('entrypoint') == 'qos2_fanout':
        from qos2_fanout_evidence import check_evidence
        check_evidence(events)
    if case.get('entrypoint') == 'qos2_flow':
        from qos2_flow_evidence import check_evidence
        check_evidence(events)
    if case.get('entrypoint') == 'qos2_recovery':
        from qos2_evidence import check_evidence
        check_evidence(events)
        for kind in ['qos2-branch-completed', 'qos2-disconnect', 'qos2-clean-session-reset']:
            rows = [x for x in events if x['kind'] == kind]
            if len(rows) != 4 or {(x.get('direction'), x.get('phase')) for x in rows} != RECOVERY_BRANCHES:
                raise InputError('incomplete recovery branch evidence: ' + kind)
        for direction, phase in RECOVERY_BRANCHES:
            branch = [x for x in events if x.get('direction') == direction and x.get('phase') == phase]
            if len([x for x in branch if x['kind'] == 'observation']) != 1:
                raise InputError('missing recovery branch observation')
            checks = {x.get('assertion') for x in branch if x['kind'] == 'assertion' and x.get('passed') is True}
            required = {'qos2-session-present', 'qos2-single-application-delivery', 'qos2-health-control-and-single-delivery'}
            required |= ({'qos2-cut-correct-ACK', 'qos2-proxy-cleanup'} if direction == 'client-to-broker'
                         else {'qos2-PUBREL'})
            if direction == 'broker-to-client' and phase == 'AwaitPubRec':
                required |= {'qos2-retransmit-mid', 'qos2-retransmit-dup', 'qos2-retransmit-content'}
            if not required <= checks:
                raise InputError('required recovery branch assertions missing')
            sessions = [x.get('expected') for x in branch if x.get('assertion') == 'qos2-session-present']
            if sessions != [False, True]:
                raise InputError('missing initial and resumed session evidence')
            deliveries = [x for x in branch if x['kind'] == 'application-delivery']
            if (len(deliveries) != 2 or len({x.get('payload_hex') for x in deliveries}) != 2
                    or any(x.get('count') != 1 for x in deliveries)):
                raise InputError('missing single original/health application deliveries')
            done = next(x for x in branch if x['kind'] == 'qos2-branch-completed')
            if done.get('original_deliveries') != 1 or done.get('health_deliveries') != 1:
                raise InputError('incomplete recovery branch delivery counts')
        cuts = [x for x in events if x['kind'] == 'fault-cut']
        if (len(cuts) != 2 or {x.get('phase') for x in cuts} != {'AwaitPubRec', 'AwaitPubComp'}
                or any(x.get('forwarded') is not False or x.get('direction') != 'client-to-broker' for x in cuts)):
            raise InputError('missing real ACK cut boundaries')
        for x in cuts:
            expected = 0x50 if x['phase'] == 'AwaitPubRec' else 0x70
            if x.get('ack_kind') != expected or not x.get('ack_body_hex'):
                raise InputError('wrong ACK cut phase')
    if case.get('entrypoint') == 'retained':
        from retained import check_evidence
        check_evidence(events)
    if case['runner'] == 'upstream':
        selection = [x for x in events if x['kind'] == 'upstream-selection']
        if len(selection) != 1 or any(selection[0].get(k) != v for k, v in case['upstream'].items()):
            raise InputError('upstream invocation evidence mismatch')
        if selection[0].get('sha256') != job['upstream_sha256']:
            raise InputError('upstream source hash mismatch')
    if job['resources'] is not None:
        targets = [x for x in events if x['kind'] == 'target']
        if len(targets) != 1 or targets[0].get('image_id') != job['image_id'] or targets[0].get('container') != job['resources']['name']:
            raise InputError('actual target image/container differs from run manifest')
        stops = [x for x in events if x['kind'] == 'host-stop']
        if len(stops) != 1 or stops[0].get('exit_code') != '0':
            raise InputError('missing successful host exit')
        if job['target'] != 'reference' and 'GracefullyStopped' not in stops[0].get('logs', ''):
            raise InputError('missing checked graceful shutdown')
    elif job['target'] != 'tool':
        targets = [x for x in events if x['kind'] == 'live-target']
        if len(targets) != 1 or targets[0].get('endpoint') != job['endpoint']:
            raise InputError('live endpoint evidence mismatch')


def gate_status(rows):
    if not rows or any(x['status'] in {'ERROR', 'BLOCKED', 'NOT_TESTED'} for x in rows):
        return 'BLOCKED'
    return 'FAIL' if any(x['status'] == 'FAIL' for x in rows) else 'PASS'


def summarize(manifest, records, catalog):
    """Derive from the plan, never from whichever results happen to be present."""
    check_manifest(manifest, catalog)
    planned = [(x['target'], x['case_id']) for x in manifest['plan']]
    keys = [(x['target'], x['case_id']) for x in records]
    if len(set(planned)) != len(planned) or len(set(keys)) != len(keys):
        raise InputError('duplicate planned or completed target/case')
    if not set(keys) <= set(planned):
        raise InputError('result outside selected plan')
    for row in records:
        if row.get('run_id') != manifest['run_id']:
            raise InputError('result from another run')
        if row['status'] == 'PASS' and row.get('evidence_validated') is not True:
            raise InputError('PASS lacks parent-validated evidence')
        if row['status'] == 'PASS' and row.get('profile_binding') != manifest['target_profiles'].get(row['target']):
            raise InputError('aggregate result profile differs from planned target')
    indexed = dict(zip(keys, records))
    results = []
    for job in manifest['plan']:
        key = (job['target'], job['case_id'])
        results.append(indexed.get(key, dict(target=key[0], case_id=key[1], status='NOT_TESTED',
                                            seconds=0, error='planned case has no completed result')))
    targets = list(dict.fromkeys(x['target'] for x in manifest['plan']))
    gates = {target: gate_status([x for x in results if x['target'] == target]) for target in targets}
    required = {target: gates.get(target, 'BLOCKED') for target in manifest['required_targets']}
    exit_code = 3 if any(x == 'BLOCKED' for x in required.values()) else 1 if 'FAIL' in required.values() else 0
    if manifest.get('interrupted') or manifest.get('cleanup_incomplete') or manifest.get('input_changed'):
        exit_code = 3
    if manifest.get('require_conformance') and exit_code == 0:
        exit_code = 3
    obligations = []
    for target in targets:
        if target == 'tool':
            continue
        for item in catalog['obligations']:
            rows = [indexed.get((target, case)) for case in item['required_cases']]
            observed = [r['case_id'] for r in rows if r and r['status'] == 'PASS']
            obligations.append(dict(target=target, obligation_id=item['id'], review_status=item['review_status'],
                 execution='PASS' if len(observed) == len(rows) else 'INCOMPLETE',
                 conformance='INCONCLUSIVE' if any(r and r['status'] != 'NOT_TESTED' for r in rows) else 'NOT_TESTED',
                 evidence_strength='TargetedEvidence' if observed else 'NotRun', observed_cases=observed,
                 missing_cases=[c for c in item['required_cases'] if c not in observed], limitations=item['limitations']))
    summary = dict(schema_version=1, run_id=manifest['run_id'], claim='representative MQTT3.1.1 validation',
         target_profiles=manifest.get('target_profiles', {}),
         full_conformance='Partial', normative_review='pending', gate_scope='selected scenarios on required targets only',
         target_gates=gates, required_targets=manifest['required_targets'], exit_code=exit_code,
         counts={s: sum(x['status'] == s for x in results) for s in STATUSES},
         planned_scenarios=len(manifest['plan']), inventory_scope='representative (not official coverage)',
         results=results, obligations=obligations)
    check('summary', summary)
    return summary


def write_reports(output, manifest, records, catalog):
    summary = summarize(manifest, records, catalog)
    write_json(output / 'summary.json', summary)
    suite = ET.Element('testsuite', tests=str(len(summary['results'])), failures=str(summary['counts']['FAIL']),
                       errors=str(summary['counts']['ERROR']), skipped=str(summary['counts']['BLOCKED'] + summary['counts']['NOT_TESTED']))
    for result in summary['results']:
        node = ET.SubElement(suite, 'testcase', classname=result['target'], name=result['case_id'], time=str(result['seconds']))
        if result['status'] != 'PASS':
            category = 'skipped' if result['status'] in {'BLOCKED', 'NOT_TESTED'} else 'failure' if result['status'] == 'FAIL' else 'error'
            ET.SubElement(node, category, type=result['status'], message=result.get('error') or '')
    ET.ElementTree(suite).write(output / 'junit.xml', encoding='utf-8', xml_declaration=True)
    lines = ['# MQTT3.1.1 代表性验证', '', '完整符合性：Partial；规范语义审查：pending。', '',
             '门禁仅适用于所选场景和明确的必需目标；不代表完整官方条款覆盖率。', '',
             '必需目标：' + ', '.join(manifest['required_targets']), '',
             '| 目标 | 代表性门禁 |', '|---|---|']
    lines += [f'| {target} | {status} |' for target, status in summary['target_gates'].items()]
    lines += ['', '| 目标 | Profile / 版本 | 配置核验 |', '|---|---|---|']
    lines += [f'| {target} | {binding["id"]} / {binding["version"]} | {binding["configuration_status"]} |'
              for target, binding in summary['target_profiles'].items() if binding is not None]
    lines += ['', f'退出码：{summary["exit_code"]}', '', '| 目标 | 场景 | 执行状态 |', '|---|---|---|']
    lines += [f'| {r["target"]} | {r["case_id"]} | {r["status"]} |' for r in summary['results']]
    (output / 'summary.md').write_text('\n'.join(lines) + '\n')
    return summary
