"""Contract failures must stop execution, not merely decorate the final report."""
import copy
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from common import InputError, ROOT, load_catalog, read_json, write_json
from execution_contracts import check_manifest, check_request
from lifecycle import allocate_resources
from run_fixture import fixture, resign
from schema_check import check, load_schema
import worker


class ExecutionContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mqtt-execution-check-')
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.manifest, self.catalog, self.jobs = fixture(self.output)
        self.job = self.jobs[1]
        self.request = self.output / (Path(self.job['trace']).stem + '.request.json')

    def test_current_catalog_and_all_inline_contracts_match_standalone_schemas(self):
        check('catalog', self.catalog)
        for case in self.catalog['cases']:
            check('case', case)
        for obligation in self.catalog['obligations']:
            check('obligation', obligation)
        self.assertEqual(load_schema('catalog')['properties']['cases']['items'], load_schema('case'))
        self.assertEqual(load_schema('request')['properties']['case'], load_schema('case'))
        self.assertEqual(load_schema('catalog')['properties']['obligations']['items'], load_schema('obligation'))
        check_request(self.job, self.request, before_execution=True)

    def test_catalog_rejects_unknown_metadata_runner_options_live_scope_and_approval(self):
        lock = read_json(ROOT / 'upstream.lock.json')
        for mutate in [lambda c: c.update(approved=True),
                       lambda c: c['cases'][0].update(entrypoint='paho'),
                       lambda c: c['cases'][1].update(live_safe=True),
                       lambda c: next(case for case in c['cases'] if case['runner']=='upstream')['upstream']['parameters'].update(clean_session='false'),
                       lambda c: c['obligations'][0].update(review_status='approved'),
                       lambda c: c['obligations'][0].update(official_ids=['MQTT-FAKE'])]:
            value = copy.deepcopy(self.catalog)
            mutate(value)
            path = self.output / 'catalog-negative.json'
            write_json(path, value)
            with self.assertRaises(InputError):
                load_catalog(path, lock)

    def test_manifest_rejects_rehashed_semantically_invalid_plans_and_target_sets(self):
        for mutate in [lambda m: m['plan'].append(m['plan'][0]),
                       lambda m: m['plan'].reverse(),
                       lambda m: m['plan'][1].update(case_id='MQTT311-SYS-TOPIC-001'),
                       lambda m: m['plan'][1].update(case_id='UPSTREAM-MOSQ-001'),
                       lambda m: m.update(required_targets=['tool']),
                       lambda m: m['live_endpoints'].update({'live-unit': ['127.0.0.1', 65536]}),
                       lambda m: m['target_profiles']['live-unit'].update(configuration_status='snapshot-checked'),
                       lambda m: m.update(completed_utc='2026-11-09T00:00:00+00:00')]:
            value = copy.deepcopy(self.manifest)
            mutate(value)
            with self.assertRaises(InputError):
                check_manifest(resign(value), self.catalog)

    def test_manifest_rejects_stale_hashes_wrong_types_unknown_fields_and_missing_fields(self):
        for change in [dict(plan_sha256='0' * 64), dict(execution_sha256='0' * 64), dict(schema_version=True),
                       dict(case_timeout_seconds=0), dict(cleanup_grace_seconds=True), dict(approved=True)]:
            with self.assertRaises(InputError):
                check_manifest(dict(self.manifest, **change), self.catalog)
        value = copy.deepcopy(self.manifest)
        del value['source_files']
        with self.assertRaises(InputError):
            check_manifest(value, self.catalog)

    def test_request_binds_run_plan_case_target_endpoint_paths_and_profile(self):
        for change in [dict(plan_index=0), dict(schema_version=2), dict(run_id='20261110T000000Z-000000000000'),
                       dict(execution_sha256='0' * 64), dict(target='live-other'),
                       dict(endpoint=['127.0.0.1', 2883]), dict(endpoint=['192.168.0.1', 1883]),
                       dict(config='/tmp/unplanned-config'), dict(upstream='/tmp/unplanned-upstream'),
                       dict(trace=str(self.output / 'other.jsonl')), dict(manifest='/tmp/unplanned-manifest.json'),
                       dict(profile_file=str(ROOT / 'profiles/mqtt311-live-unverified.json')),
                       dict(profile_binding=None), dict(image='sha256:' + '0' * 64),
                       dict(case={**self.job['case'], 'required_assertions': ['weakened-assertion']}),
                       dict(upstream_sha256='0' * 64), dict(resources={}), dict(approved=True)]:
            with self.assertRaises(InputError):
                check_request(dict(self.job, **change), self.request, before_execution=True)

    def test_changed_missing_extra_and_symlinked_frozen_inputs_are_rejected(self):
        path = self.output / 'input/catalog.json'
        body = path.read_bytes()
        path.write_bytes(body + b'\n')
        with self.assertRaises(InputError):
            check_request(self.job, self.request)
        path.unlink()
        with self.assertRaises(InputError):
            check_request(self.job, self.request)
        path.symlink_to(ROOT / 'catalog.json')
        with self.assertRaises(InputError):
            check_request(self.job, self.request)
        path.unlink()
        path.write_bytes(body)
        (path.parent / 'extra.json').write_text('{}')
        with self.assertRaises(InputError):
            check_request(self.job, self.request)

    def test_recomputed_snapshot_digests_cannot_reuse_the_original_request(self):
        from execution_contracts import input_identity
        catalog = copy.deepcopy(self.catalog)
        catalog['cases'][-1]['required_assertions'] = ['weakened-assertion']
        write_json(self.output / 'input/catalog.json', catalog)
        manifest = copy.deepcopy(self.manifest)
        manifest['input_files'] = input_identity(self.output)
        write_json(self.output / 'manifest.json', resign(manifest))
        with self.assertRaisesRegex(InputError, 'another run or execution'):
            check_request(self.job, self.request)

    def test_completed_runs_and_existing_artifacts_cannot_be_replayed(self):
        for name in ['trace', 'log', 'result']:
            path = Path(self.job[name])
            path.write_text('existing execution')
            with self.assertRaises(InputError):
                check_request(self.job, self.request, before_execution=True)
            self.assertEqual(path.read_text(), 'existing execution')
            path.unlink()
        manifest = dict(self.manifest, completed_utc='2026-11-10T00:00:01+00:00')
        write_json(self.output / 'manifest.json', manifest)
        with self.assertRaisesRegex(InputError, 'already completed'):
            check_request(self.job, self.request, before_execution=True)

    def test_bad_worker_request_is_rejected_before_trace_network_or_container_calls(self):
        for change in [dict(endpoint=['127.0.0.1', 2883]), dict(trace='/tmp/unplanned-write.jsonl'),
                       dict(case={**self.job['case'], 'entrypoint': 'system_topics'})]:
            write_json(self.request, dict(self.job, **change))
            with patch.object(worker, 'Trace') as trace, patch.object(worker, 'Instance') as instance, \
                    patch('socket.create_connection') as network, patch.object(worker.scenarios, 'paho') as scenario:
                with self.assertRaises(InputError):
                    worker.main(str(self.request))
                for operation in [trace, instance, network, scenario]:
                    operation.assert_not_called()
            self.assertFalse(Path(self.job['trace']).exists())

    def test_owned_instance_resources_are_bound_to_token_names_and_marker(self):
        _, _, jobs = fixture(self.output, ('mqtt-server',))
        job = jobs[1]
        resource = allocate_resources()
        self.addCleanup(lambda: shutil.rmtree(resource['directory'], ignore_errors=True))
        job['resources'] = resource
        request = self.output / (Path(job['trace']).stem + '.request.json')
        check_request(job, request, before_execution=True)
        for changes in [dict(token='f' * 32), dict(name='mqtt-validation-' + 'f' * 20),
                        dict(volume='mqtt-validation-' + 'f' * 20 + '-state'), dict(directory='/tmp')]:
            with self.assertRaises(InputError):
                check_request(dict(job, resources={**resource, **changes}), request, before_execution=True)
        write_json(Path(resource['directory']) / '.owner.json', {'token': 'foreign'})
        with self.assertRaises(InputError):
            check_request(job, request, before_execution=True)

    def test_parent_cleans_its_allocated_journal_when_request_validation_fails(self):
        import run
        dependency_file = self.output / 'dependencies.json'
        write_json(dependency_file, {})
        args = run.parser().parse_args(['--targets', 'reference', '--cases', 'MQTT311-CONNECT-001',
                                      '--dependencies', str(dependency_file), '--output', str(self.output / 'rejected-run')])
        resource = dict(token='a' * 32, name='mqtt-validation-' + 'a' * 20,
                        volume='mqtt-validation-' + 'a' * 20 + '-state', directory='/tmp/not-created')
        dep = dict(python_launcher=sys.executable, upstream='/nonexecuted-test-upstream')
        with patch.object(run, 'dependencies', return_value=dep), \
                patch.object(run, 'command', return_value='[{"Id":"sha256:' + '0' * 64 + '"}]'), \
                patch.object(run, 'allocate_resources', return_value=resource), \
                patch.object(run, 'check_request', side_effect=InputError('rejected request')), \
                patch.object(run, 'supervise') as supervise, \
                patch.object(run, 'cleanup', return_value=dict(complete=True, forced=False, resources_found=False, errors=[])) as cleanup:
            self.assertEqual(run.execute(args), 3)
            supervise.assert_not_called()
            cleanup.assert_called_once_with(resource)


if __name__ == '__main__':
    unittest.main()
