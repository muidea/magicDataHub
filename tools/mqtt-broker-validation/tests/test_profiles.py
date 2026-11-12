"""Profile declarations cannot replace host identity or configuration evidence."""
import copy
from pathlib import Path
import tempfile
import unittest

from common import InputError, ROOT, digest, load_catalog, read_json
from inputs import config_inputs
from profiles import bind, load_profiles, verify_binding
from schema_check import check, schema_definition


class Profiles(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='mqtt-profile-check-')
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.config = self.output / 'config'
        self.configs, _ = config_inputs(ROOT.parents[1])
        self.reset_config()
        self.profiles = load_profiles()
        self.case = next(x for x in load_catalog(ROOT / 'catalog.json', read_json(ROOT / 'upstream.lock.json'))['cases']
                         if x['id'] == 'MQTT311-CONNECT-001')

    def reset_config(self):
        for relative, body in self.configs.items():
            path = self.config / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)

    def job(self, target):
        binding = bind(target, self.profiles, self.config)
        key = target.removeprefix('live-')
        profile, source = self.profiles[key]
        snapshot = self.output / (profile['id'] + '.json')
        snapshot.write_bytes(source.read_bytes())
        return dict(target=target, case=self.case, config=str(self.config),
                    profile_binding=binding, profile_file=str(snapshot))

    def test_two_hosts_have_distinct_profiles_and_effective_configuration_hashes(self):
        bindings = []
        for target in ['mqtt-server', 'datahub-server']:
            job = self.job(target)
            binding = verify_binding(job)
            self.assertEqual(binding['configuration_status'], 'snapshot-checked')
            self.assertEqual(binding['config_sha256'], {p: digest(self.config / p)
                              for p in self.profiles[target][0]['configuration_files']})
            bindings.append(binding)
        self.assertNotEqual(bindings[0]['id'], bindings[1]['id'])
        self.assertNotEqual(bindings[0]['sha256'], bindings[1]['sha256'])

    def test_live_alias_does_not_claim_configuration_verification(self):
        for target in ['live-mqtt-server', 'live-datahub-server', 'live-another-broker']:
            binding = bind(target, self.profiles, self.output / 'nonexistent')
            self.assertEqual(binding['configuration_status'], 'unverified')
            self.assertEqual(binding['mode'], 'live-network-only')
            self.assertEqual(binding['config_sha256'], {})
        self.assertEqual(bind('live-another-broker', self.profiles, self.config)['id'], 'mqtt311-live-unverified')

    def test_swapping_host_or_mode_is_rejected(self):
        job = self.job('mqtt-server')
        for replacement in [bind('datahub-server', self.profiles, self.config),
                            bind('live-mqtt-server', self.profiles, self.config)]:
            with self.assertRaises(InputError):
                verify_binding(dict(job, profile_binding=replacement))

    def test_changed_or_symlinked_profile_snapshot_is_rejected(self):
        job = self.job('mqtt-server')
        path = Path(job['profile_file'])
        original = path.read_bytes()
        path.write_bytes(original + b'\n')
        with self.assertRaises(InputError):
            verify_binding(job)
        path.unlink()
        path.symlink_to(self.profiles['mqtt-server'][1])
        with self.assertRaises(InputError):
            verify_binding(job)

    def test_configuration_drift_and_optional_features_cannot_keep_profile(self):
        relative = 'mqtt/standalone.toml'
        mutations = [lambda s: s.replace('max_connections = 64', 'max_connections = 65'),
                     lambda s: s.replace('enabled = true', 'enabled = 1'),
                     lambda s: s + '\n[mqtt.tls]\ncertificate_chain="test.pem"\n',
                     lambda s: s.replace('anonymous_access = true', 'anonymous_access = false')]
        for mutate in mutations:
            self.reset_config()
            (self.config / relative).write_text(mutate(self.configs[relative]))
            with self.assertRaises(InputError):
                bind('mqtt-server', self.profiles, self.config)
        self.reset_config()
        path = self.config / 'combined-development.toml'
        path.write_text(self.configs['combined-development.toml'].replace('mqtt/combined.toml', 'mqtt/standalone.toml'))
        with self.assertRaises(InputError):
            bind('datahub-server', self.profiles, self.config)

    def test_case_protocol_and_family_are_bound(self):
        job = self.job('mqtt-server')
        for field, value in [('protocol', 'mqtt5'), ('profile', 'different-family')]:
            with self.assertRaises(InputError):
                verify_binding(dict(job, case={**job['case'], field: value}))

    def test_effective_configuration_bytes_are_bound_even_when_semantics_match(self):
        job = self.job('datahub-server')
        path = self.config / 'fedengine/combined.toml'
        path.write_text(path.read_text() + '\n# changed after planning\n')
        with self.assertRaisesRegex(InputError, 'binding mismatch'):
            verify_binding(job)

    def test_schema_rejects_missing_unknown_wrong_types_and_unreviewed_claims(self):
        original = self.profiles['mqtt-server'][0]
        for mutation in ['missing', 'unknown', 'wrong-type', 'approved', 'duplicate']:
            value = copy.deepcopy(original)
            if mutation == 'missing':
                del value['excluded_claims']
            elif mutation == 'unknown':
                value['production_approved'] = True
            elif mutation == 'wrong-type':
                value['schema_version'] = True
            elif mutation == 'approved':
                value['normative_review'] = 'approved'
            else:
                value['excluded_claims'].append(value['excluded_claims'][0])
            with self.assertRaises(InputError):
                check('profile', value)

    def test_schema_interpreter_rejects_unsupported_validation_keyword(self):
        with self.assertRaises(InputError):
            schema_definition({'type': 'object', 'allOf': []})


if __name__ == '__main__':
    unittest.main()
