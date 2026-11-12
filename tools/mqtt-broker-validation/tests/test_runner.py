"""Negative gates: incomplete evidence, changed inputs, missing runs and cancellation."""
import copy
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from common import InputError, ROOT, digest, load_catalog, read_json, write_json
from inputs import CONFIG_FILES, config_inputs, dependencies, immutable_image
from reporting import summarize, validate_result
from profiles import bind, load_profiles
from schema_check import check
from run import Cancellation, supervise
from run_fixture import RUN_ID, fixture, resign


class Gates(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mqtt-runner-check-')
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.manifest, self.catalog, jobs = fixture(self.output)
        self.job = jobs[1]
        self.case = self.job['case']
        self.events = [dict(kind='case-start', run_id=RUN_ID, attempt_id=self.job['attempt_id'], target='live-unit', case_id=self.case['id'], profile_binding=self.job['profile_binding']),
                       dict(kind='live-target', endpoint=self.job['endpoint'], profile=self.case['profile']),
                       dict(kind='bytes', connection='unit', direction='tx', hex='c000'), dict(kind='bytes', connection='unit', direction='rx', hex='d000'),
                       *[dict(kind='assertion', assertion=name, passed=True) for name in self.case['required_assertions']],
                       dict(kind='profile-checked', binding=self.job['profile_binding']), dict(kind='case-end', status='PASS')]
        for index, event in enumerate(self.events):
            event.update(schema_version=1, sequence=index, seconds=index / 1000)

    def result(self, events=None, **overrides):
        path = Path(self.job['trace'])
        path.write_text(''.join(json.dumps(x) + '\n' for x in (events if events is not None else self.events)))
        record = dict(schema_version=1, run_id=RUN_ID, attempt_id=self.job['attempt_id'], target='live-unit', case_id=self.case['id'],
                      profile_binding=self.job['profile_binding'],
                      status='PASS', error=None, seconds=0.01, evidence=path.name,
                      evidence_sha256=digest(path), evidence_bytes=path.stat().st_size)
        record.update(overrides)
        write_json(self.job['result'], record)
        return validate_result(self.output, self.job, 0)

    def test_complete_evidence_can_pass_bounded_gate(self):
        self.assertEqual(self.result()['status'], 'PASS')

    def test_typed_event_fields_unknown_kinds_and_unknown_fields_are_rejected(self):
        for field, value in [('passed', 'true'), ('seconds', True), ('schema_version', True),
                             ('kind', 'unregistered-event'), ('unreviewed', True)]:
            events = copy.deepcopy(self.events)
            next(x for x in events if x['kind'] == 'assertion')[field] = value
            self.assertEqual(self.result(events)['status'], 'ERROR')
        events = copy.deepcopy(self.events)
        next(x for x in events if x['kind'] == 'bytes')['hex'] = 'c0x0'
        self.assertEqual(self.result(events)['status'], 'ERROR')

    def test_event_gaps_duplicates_reordering_and_time_regression_are_rejected(self):
        for mutation in ['gap', 'duplicate', 'reorder', 'time']:
            events = copy.deepcopy(self.events)
            if mutation == 'gap':
                events[2]['sequence'] += 100
            elif mutation == 'duplicate':
                events[2]['sequence'] = events[1]['sequence']
            elif mutation == 'reorder':
                events[2], events[3] = events[3], events[2]
            else:
                events[2]['seconds'] = events[-1]['seconds'] + 1
            self.assertEqual(self.result(events)['status'], 'ERROR')

    def test_duplicate_case_boundary_is_rejected_even_with_valid_sequence(self):
        for boundary in ['case-start', 'case-end']:
            events = copy.deepcopy(self.events)
            events.insert(1, copy.deepcopy(next(x for x in events if x['kind'] == boundary)))
            for index, event in enumerate(events):
                event.update(sequence=index, seconds=index / 1000)
            self.assertEqual(self.result(events)['status'], 'ERROR')

    def test_worker_result_contract_rejects_wrong_types_versions_and_unknown_fields(self):
        for change in [dict(schema_version=True), dict(schema_version=2), dict(seconds=True),
                       dict(evidence_bytes=True), dict(error='failed but says PASS'), dict(unreviewed=True)]:
            self.assertEqual(self.result(**change)['status'], 'ERROR')

    def test_nonfinite_and_duplicate_key_worker_json_cannot_pass(self):
        for injected in ['"status":"FAIL","status":"PASS"', '"seconds":NaN',
                         '"seconds":Infinity', '"seconds":1e999']:
            self.result()
            path = Path(self.job['result'])
            value = path.read_text()
            old = '"status": "PASS"' if 'status' in injected else '"seconds": 0.01'
            path.write_text(value.replace(old, injected))
            self.assertEqual(validate_result(self.output, self.job, 0)['status'], 'ERROR')

    def test_aggregation_rejects_duplicates_unplanned_and_stale_results(self):
        manifest = self.manifest
        row = self.aggregate_record('tool', 'TOOL-CAL-001', 'PASS')
        for records in [[row, row], [dict(row, case_id='unplanned')], [dict(row, run_id='old-run')],
                        [dict(row, evidence_validated=False)]]:
            with self.assertRaises(InputError):
                summarize(manifest, records, self.catalog)
        with self.assertRaises(InputError):
            summarize(resign(dict(manifest, plan=manifest['plan'] * 2)), [row], self.catalog)

    def test_summary_schema_cannot_upgrade_claim_or_hide_invalid_counts_and_results(self):
        row = self.aggregate_record('tool', 'TOOL-CAL-001', 'PASS')
        original = summarize(self.manifest, [row], self.catalog)
        for mutation in ['counts', 'claim', 'cause', 'validated', 'unknown']:
            summary = copy.deepcopy(original)
            if mutation == 'counts':
                summary['counts']['PASS'] = True
            elif mutation == 'claim':
                summary['full_conformance'] = 'Complete'
            elif mutation == 'cause':
                summary['results'][0]['error'] = 'failed'
            elif mutation == 'validated':
                summary['results'][0]['evidence_validated'] = False
            else:
                summary['unreviewed_approval'] = True
            with self.assertRaises(InputError):
                check('summary', summary)

    def test_profile_result_and_trace_cannot_substitute_or_omit_planned_binding(self):
        wrong = dict(self.job['profile_binding'], sha256='0' * 64)
        self.assertEqual(self.result(profile_binding=wrong)['status'], 'ERROR')
        self.assertEqual(self.result(profile_binding=None)['status'], 'ERROR')
        for kind in ['case-start', 'profile-checked']:
            events = copy.deepcopy(self.events)
            event = next(x for x in events if x['kind'] == kind)
            event['profile_binding' if kind == 'case-start' else 'binding'] = wrong
            self.assertEqual(self.result(events)['status'], 'ERROR')
        self.assertEqual(self.result([x for x in self.events if x['kind'] != 'profile-checked'])['status'], 'ERROR')

    def test_missing_required_assertion_cannot_pass(self):
        self.assertEqual(self.result([x for x in self.events if x.get('assertion') != 'paho-delivery'])['status'], 'ERROR')

    def test_false_assertion_cannot_pass(self):
        self.events[4]['passed'] = False
        self.assertEqual(self.result()['status'], 'ERROR')

    def test_absent_or_empty_receive_capture_cannot_pass(self):
        self.events[3]['hex'] = ''
        self.assertEqual(self.result()['status'], 'ERROR')

    def test_trace_hash_and_paths_are_checked(self):
        for changes in [dict(evidence_sha256='0' * 64), dict(evidence='../trace.jsonl')]:
            self.assertEqual(self.result(**changes)['status'], 'ERROR')
        self.result()
        Path(self.job['trace']).unlink()
        self.assertEqual(validate_result(self.output, self.job, 0)['status'], 'ERROR')

    def test_nonzero_worker_exit_cannot_pass(self):
        self.result()
        self.assertEqual(validate_result(self.output, self.job, 1)['status'], 'ERROR')

    def test_attempt_identity_prevents_old_result_reuse(self):
        self.assertEqual(self.result(attempt_id='previous')['status'], 'ERROR')

    def test_symlink_evidence_is_rejected(self):
        self.result()
        Path(self.job['trace']).rename(self.output / 'original.jsonl')
        Path(self.job['trace']).symlink_to(self.output / 'original.jsonl')
        self.assertEqual(validate_result(self.output, self.job, 0)['status'], 'ERROR')

    def test_scoped_gate_preserves_reference_blocked(self):
        manifest, catalog, _ = fixture(self.output, ('mqtt-server', 'reference'))
        manifest['required_targets'] = ['tool', 'mqtt-server']
        resign(manifest)
        records = [dict(self.aggregate_record(row['target'], row['case_id'], 'BLOCKED' if row['target'] == 'reference' else 'PASS'),
                        profile_binding=manifest['target_profiles'][row['target']]) for row in manifest['plan']]
        summary = summarize(manifest, records, catalog)
        self.assertEqual(summary['exit_code'], 0)
        self.assertEqual(summary['target_gates']['reference'], 'BLOCKED')
        manifest['required_targets'].append('reference')
        self.assertEqual(summarize(resign(manifest), records, catalog)['exit_code'], 3)
        manifest['required_targets'].remove('reference')
        manifest['require_conformance'] = True
        self.assertEqual(summarize(resign(manifest), records, catalog)['exit_code'], 3)

    def test_missing_planned_case_blocks_gate(self):
        result = summarize(self.manifest, [], self.catalog)
        self.assertEqual(result['results'][0]['status'], 'NOT_TESTED')
        self.assertEqual(result['exit_code'], 3)

    def test_cleanup_or_input_change_blocks_every_scope(self):
        manifest = self.manifest
        record = self.aggregate_record('tool', 'TOOL-CAL-001', 'PASS')
        for flag in ['cleanup_incomplete', 'input_changed', 'interrupted']:
            changed = dict(manifest, **{flag: True})
            self.assertEqual(summarize(changed, [record], self.catalog)['exit_code'], 3)

    def aggregate_record(self, target, case_id, status):
        return dict(schema_version=1, run_id=RUN_ID, attempt_id='attempt-' + target, target=target,
                    case_id=case_id, status=status, seconds=0, error=None if status == 'PASS' else 'unit blocked',
                    profile_binding=None, evidence='trace.jsonl', evidence_sha256='0' * 64,
                    evidence_bytes=1, evidence_types=['E-EXEC'], evidence_validated=True)

    def test_catalog_rejects_duplicates_and_unknown_mappings(self):
        lock = read_json(ROOT / 'upstream.lock.json')
        original = read_json(ROOT / 'catalog.json')
        for mutation in ['duplicate', 'unknown', 'malformed', 'runner']:
            catalog = copy.deepcopy(original)
            if mutation == 'duplicate':
                catalog['cases'].append(catalog['cases'][0])
            elif mutation == 'unknown':
                catalog['cases'][1]['obligations'] = ['UNKNOWN']
            elif mutation == 'malformed':
                catalog['cases'][0]['required_assertions'] = 'not a list'
            else:
                catalog['cases'][0]['runner'] = []
            path = self.output / 'catalog.json'
            write_json(path, catalog)
            with self.assertRaises(InputError):
                load_catalog(path, lock)

    def test_bad_dependency_manifest_rejected_before_execution(self):
        path = self.output / 'dependencies.json'
        write_json(path, dict(schema_version=1, lock_sha256='0' * 64))
        with self.assertRaises(InputError):
            dependencies(path, read_json(ROOT / 'upstream.lock.json'))

    def test_public_profile_rejects_sources_credentials_and_remote_listener(self):
        for relative in CONFIG_FILES:
            path = self.output / 'config' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative.startswith('mqtt/'):
                path.write_text('[mqtt.core]\nenabled=true\nlistener="127.0.0.1:1883"\n[mqtt.security]\nanonymous_access=true\n')
            else:
                path.write_text('[application]\nname="unit"\n')
        first = self.output / 'config/mqtt-development.toml'
        for extra in ['\n[[sources]]\nname="business"\n', '\n[auth]\npassword="secret"\n', '\n[pgwire]\nlisten="192.168.0.1:5432"\n']:
            first.write_text('[application]\nname="unit"\n' + extra)
            with self.assertRaises(InputError):
                config_inputs(self.output)

    def test_mutable_image_rejected(self):
        with self.assertRaises(InputError):
            immutable_image('product:latest')

    def test_bad_cli_catalog_has_input_exit(self):
        path = self.output / 'bad.json'
        path.write_text('{}')
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'run.py'), '--check', '--catalog', str(path)], capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_real_worker_deadline_allows_signal_cleanup(self):
        marker = self.output / 'cleaned'
        script = self.output / 'peer.py'
        script.write_text('import signal, time\nfrom pathlib import Path\n'
                          f'def stop(*args):\n    Path({str(marker)!r}).write_text("cleaned")\n    raise SystemExit(0)\n'
                          'signal.signal(signal.SIGTERM, stop)\nwhile True: time.sleep(0.1)\n')
        process = supervise([sys.executable, str(script)], self.output / 'process.log', 0.3, 2, Cancellation())
        self.assertTrue(process['timed_out'])
        self.assertEqual(process['returncode'], 0)
        self.assertTrue(marker.is_file())

    def test_cancelled_supervisor_stops_real_child(self):
        cancellation = Cancellation()
        cancellation(signal.SIGTERM, None)
        process = supervise([sys.executable, '-c', 'import time; time.sleep(30)'], self.output / 'cancel.log', 10, 1, cancellation)
        self.assertTrue(process['interrupted'])
        self.assertNotEqual(process['returncode'], 0)


if __name__ == '__main__':
    unittest.main()
