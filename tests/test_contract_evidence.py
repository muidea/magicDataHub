"""Evidence gates reject missing, ignored, failed and incomplete contract runs."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('contracts', ROOT / 'scripts/check-contracts.py')
contracts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contracts)


class ContractEvidence(unittest.TestCase):
    def setUp(self):
        self.mapping = json.loads((ROOT / 'tests/contracts/mapping.json').read_text())
        self.names = {name for record in self.mapping.values() for name in record.get('remote', [])}
        self.output = '\n'.join(f'test {name} ... ok' for name in sorted(self.names))

    def test_exact_successes_cover_only_requested_scope(self):
        ids = contracts.verify('remote', self.output, self.mapping)
        self.assertIn('MDH-COMP-012', ids)
        self.assertNotIn('MDH-COMP-001', ids)

    def test_missing_ignored_and_lookalike_do_not_pass(self):
        for output in ['', self.output.replace('... ok', '... ignored'), self.output.replace(' ... ok', '_extra ... ok')]:
            with self.subTest(output=output):
                with self.assertRaises(ValueError):
                    contracts.verify('remote', output, self.mapping)

    def test_failure_invalidates_earlier_success(self):
        with self.assertRaises(ValueError):
            contracts.verify('remote', self.output + '\ntest another_case ... FAILED\n', self.mapping)

    def test_mapping_must_keep_all_twelve_contracts(self):
        del self.mapping['MDH-COMP-012']
        with self.assertRaises(ValueError):
            contracts.verify('remote', self.output, self.mapping)

    def test_empty_evidence_is_not_a_completed_rule(self):
        self.mapping['MDH-COMP-012']['remote'] = []
        with self.assertRaises(ValueError):
            contracts.verify('remote', self.output, self.mapping)
