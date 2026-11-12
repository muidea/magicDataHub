"""Upstream assessment is pinned source analysis, never execution or approval."""
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from common import InputError, ROOT, digest, load_catalog, read_json
import upstream_audit as audit


class UpstreamAudit(unittest.TestCase):
    def setUp(self):
        self.value = read_json(ROOT / 'upstream-audit.json')
        self.lock = read_json(ROOT / 'upstream.lock.json')
        self.catalog = load_catalog(ROOT / 'catalog.json', self.lock)
        self.temporary = tempfile.TemporaryDirectory(prefix='mqtt-audit-check-')
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)

    def validate(self, value):
        return audit.validate_assessment(value, self.catalog, self.lock)

    def test_current_assessment_matches_every_integrated_invocation_and_keeps_candidates_separate(self):
        self.validate(self.value)
        integrated = [a for p in self.value['projects'] for a in p['assets'] if a['integration_status'] == 'integrated']
        self.assertEqual({a['catalog_case_id'] for a in integrated},
                         {c['id'] for c in self.catalog['cases'] if c['runner'] == 'upstream'})
        self.assertTrue(all(a['reuse_grade'] == 'B' for a in integrated))
        self.assertTrue(all(a['integration_status'] == 'not-integrated' for p in self.value['projects'][1:] for a in p['assets']))

    def test_metadata_cannot_claim_complete_inventory_runtime_pass_or_independent_approval(self):
        for change in [dict(complete_upstream_inventory=True), dict(normative_review='approved'),
                       dict(independent_review='approved'), dict(independent_approvals=1),
                       dict(independent_approvals=False), dict(runtime_results='PASS'),
                       dict(schema_version=True), dict(approved=True)]:
            with self.assertRaises(InputError):
                self.validate(dict(self.value, **change))

    def test_selected_projects_assets_and_executable_cases_cannot_be_omitted_or_duplicated(self):
        for mutate in [lambda v: v['projects'].pop(),
                       lambda v: v['projects'].append(v['projects'][0]),
                       lambda v: v['projects'][0]['assets'].pop(),
                       lambda v: v['projects'][0]['assets'].append(v['projects'][0]['assets'][0])]:
            value = copy.deepcopy(self.value)
            mutate(value)
            with self.assertRaises(InputError):
                self.validate(value)

    def test_repositories_revisions_adapter_and_cache_paths_are_checked(self):
        for mutate in [lambda v: v['projects'][0].update(revision='0' * 40),
                       lambda v: v['projects'][1].update(repository='https://example.invalid/source'),
                       lambda v: v['projects'][1].update(cache_directory='../outside'),
                       lambda v: v['projects'][1].update(cache_directory='/tmp/outside'),
                       lambda v: v['projects'][1].update(cache_directory=v['projects'][0]['cache_directory']),
                       lambda v: v['projects'][1].update(cache_directory=v['projects'][0]['cache_directory'] + '/nested'),
                       lambda v: v['projects'][1].update(reference='HEAD'),
                       lambda v: v.update(adapter_sha256='0' * 64),
                       lambda v: v['projects'][0]['files'].pop('test/mqtt5_opts.py')]:
            value = copy.deepcopy(self.value)
            mutate(value)
            with self.assertRaises(InputError):
                self.validate(value)

    def test_license_and_primary_source_evidence_must_reference_locked_safe_files(self):
        for mutate in [lambda p: p.update(license_evidence=[dict(file='README.md', line=1, text='not license')]),
                       lambda p: p['assets'][0].update(file='unlocked.py'),
                       lambda p: p['files'].update({'../outside': '0' * 64}),
                       lambda p: p['assets'][0]['evidence'][0].update(file='unlocked.py')]:
            value = copy.deepcopy(self.value)
            mutate(value['projects'][1])
            with self.assertRaises(InputError):
                self.validate(value)

    def test_existing_adapter_cannot_be_reclassified_as_native_or_change_locked_parameters(self):
        for changes in [dict(reuse_grade='A'), dict(entrypoints=['main']), dict(parameters={'proto_ver': 5}),
                        dict(protocols=['mqtt5']), dict(catalog_case_id='UNKNOWN'),
                        dict(parameters={'proto_ver': 4, 'clean_session': 'false'})]:
            value = copy.deepcopy(self.value)
            value['projects'][0]['assets'][0].update(changes)
            with self.assertRaises(InputError):
                self.validate(value)
        value = copy.deepcopy(self.value)
        value['projects'][0]['assets'][-1]['parameters']['clean_session'] = True
        with self.assertRaises(InputError):
            self.validate(value)

    def test_static_candidates_cannot_acquire_an_executable_case_or_integration_claim(self):
        for change in [dict(integration_status='integrated'), dict(catalog_case_id='UPSTREAM-MOSQ-001'),
                       dict(parameters={'proto_ver': 4})]:
            value = copy.deepcopy(self.value)
            value['projects'][1]['assets'][0].update(change)
            with self.assertRaises(InputError):
                self.validate(value)

    def source_fixture(self):
        directory = self.cache / 'source'
        directory.mkdir()
        path = directory / 'source.py'
        # Reading must not import this deliberately non-executable fixture.
        path.write_text('raise RuntimeError("must not execute")\n')
        evidence = dict(file='source.py', line=1, text=path.read_text().strip())
        project = dict(id='paho-testing', cache_directory='source', revision='a' * 40,
                       repository=audit.REPOSITORIES['paho-testing'], files={'source.py': digest(path)},
                       license_evidence=[evidence], assets=[dict(evidence=[evidence])])
        return project, path

    def verify_fixture(self, project):
        with patch.object(audit, 'command', side_effect=[project['revision'], project['repository'] + '.git']):
            return audit.verify_project(project, self.cache)

    def test_source_verification_reads_bytes_and_lines_without_importing_candidate(self):
        project, path = self.source_fixture()
        self.assertEqual(self.verify_fixture(project), path.parent)

    def test_source_drift_missing_file_symlink_and_stale_line_are_rejected(self):
        project, path = self.source_fixture()
        body = path.read_bytes()
        path.write_bytes(body + b'# changed\n')
        with self.assertRaises(InputError):
            self.verify_fixture(project)
        path.unlink()
        with self.assertRaises(InputError):
            self.verify_fixture(project)
        target = self.cache / 'other.py'
        target.write_bytes(body)
        path.symlink_to(target)
        with self.assertRaises(InputError):
            self.verify_fixture(project)
        path.unlink()
        path.write_bytes(body)
        project['license_evidence'][0]['line'] = 2
        with self.assertRaises(InputError):
            self.verify_fixture(project)

    def test_source_origin_and_revision_mismatch_are_rejected(self):
        project, _ = self.source_fixture()
        for values in [['0' * 40, project['repository']], [project['revision'], 'https://example.invalid/other']]:
            with patch.object(audit, 'command', side_effect=values), self.assertRaises(InputError):
                audit.verify_project(project, self.cache)

    def test_explicit_fetch_preserves_existing_directory_and_symlink_even_if_invalid(self):
        project, path = self.source_fixture()
        with patch.object(audit, 'command') as git:
            audit.fetch_missing(project, self.cache)
            git.assert_not_called()
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(self.cache / 'nonexistent-target', target_is_directory=True)
        with patch.object(audit, 'command') as git:
            audit.fetch_missing(project, self.cache)
            git.assert_not_called()
        self.assertTrue(path.parent.is_symlink())

    def test_missing_offline_cache_fails_without_creating_a_checkout(self):
        missing = self.cache / 'not-created'
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'upstream_audit.py'),
                                 '--verify-sources', '--cache-dir', str(missing)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('missing or noncanonical upstream checkout', result.stderr)
        self.assertFalse(missing.exists())

    def test_fetch_rejects_symlink_parent_before_creating_files_or_running_git(self):
        outside = self.cache / 'outside'
        outside.mkdir()
        (self.cache / 'linked').symlink_to(outside, target_is_directory=True)
        project = dict(self.value['projects'][1], cache_directory='linked/new-checkout')
        with patch.object(audit, 'command') as git, self.assertRaises(InputError):
            audit.fetch_missing(project, self.cache)
        git.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_runner_rejects_bad_assessment_before_dependency_or_resource_operations(self):
        import run
        with patch.object(run, 'load_assessment', side_effect=InputError('bad assessment')), \
                patch.object(run, 'dependencies') as dependencies, \
                patch.object(run, 'allocate_resources') as allocate, patch.object(run, 'supervise') as supervise:
            with self.assertRaises(InputError):
                run.execute(run.parser().parse_args(['--check']))
            for operation in [dependencies, allocate, supervise]:
                operation.assert_not_called()


if __name__ == '__main__':
    unittest.main()
