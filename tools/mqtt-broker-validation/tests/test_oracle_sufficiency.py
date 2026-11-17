import copy
import hashlib
import unittest

from common import InputError, ROOT, load_catalog, read_json
from oracle_sufficiency import MIXED_IDS, evaluate_sufficiency, load_ledger, validate_ledger


class OracleSufficiencyTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog(ROOT / 'catalog.json', read_json(ROOT / 'upstream.lock.json'))
        self.ledger = load_ledger()
        self.all_case_ids = {
            cid
            for section in (
                'selected_id_oracles',
                'compound_atom_oracles',
                'mixed_qos_draft_oracles',
            )
            for entry in self.ledger[section]
            for cid in entry['case_ids']
        } | {
            cid
            for entry in self.ledger['internal_obligation_oracles']
            for cid in entry['required_cases'] + entry['conditional_cases']
        } | {
            cid
            for entry in self.ledger['selected_id_oracles']
            for group in entry['alternative_groups']
            for cid in group
        }

    def _all_passing_outcomes(self):
        return [
            {'case_id': cid, 'status': 'PASS', 'evidence_validated': True}
            for cid in sorted(self.all_case_ids)
        ]

    def test_current_ledger_binds_all_oracles_events_verifiers_tests_and_mixed_digests(self):
        self.assertEqual(len(self.ledger['selected_id_oracles']), 18)
        self.assertEqual(len(self.ledger['compound_atom_oracles']), 12)
        self.assertEqual(len(self.ledger['mixed_qos_draft_oracles']), 3)
        self.assertEqual(len(self.ledger['internal_obligation_oracles']), 9)
        self.assertEqual(self.ledger['independent_approvals'], 0)
        self.assertEqual(self.ledger['review_status'], 'pending')
        self.assertEqual(self.ledger['full_conformance'], 'Partial')
        mixed_ids = {item['id']: item for item in self.ledger['mixed_qos_draft_oracles']}
        self.assertEqual(set(mixed_ids), set(MIXED_IDS))
        for oid, item in mixed_ids.items():
            digest = hashlib.sha256(item['source_text'].encode()).hexdigest()
            self.assertEqual(item['source_sha256'], digest)
            self.assertIn(f'[{oid}]', item['source_text'])

    def test_metadata_cannot_claim_approval_runtime_pass_or_full_conformance(self):
        mutations = (
            {'independent_approvals': 1},
            {'review_status': 'approved'},
            {'full_conformance': 'CONFORMANT'},
            {'runtime_results': 'all-passed'},
        )
        for patch in mutations:
            broken = copy.deepcopy(self.ledger)
            broken.update(patch)
            with self.assertRaises(InputError):
                validate_ledger(broken, catalog=self.catalog)

        broken_claim = copy.deepcopy(self.ledger)
        broken_claim['selected_id_oracles'][0]['evidence_claim'] = 'CompleteCoverage'
        with self.assertRaises(InputError):
            validate_ledger(broken_claim, catalog=self.catalog)

    def test_unknown_events_verifier_test_ref_or_tampered_mixed_sha_rejected(self):
        bad_event = copy.deepcopy(self.ledger)
        bad_event['selected_id_oracles'][0]['required_event_kinds'].append('invented-event')
        with self.assertRaises(InputError):
            validate_ledger(bad_event, catalog=self.catalog)

        bad_verifier = copy.deepcopy(self.ledger)
        bad_verifier['compound_atom_oracles'][0]['parent_verifier'] = 'connection.nonexistent_fn'
        with self.assertRaises(InputError):
            validate_ledger(bad_verifier, catalog=self.catalog)

        bad_test = copy.deepcopy(self.ledger)
        bad_test['internal_obligation_oracles'][0]['negative_counterexamples'].append('test_fake.test_none')
        with self.assertRaises(InputError):
            validate_ledger(bad_test, catalog=self.catalog)

        bad_sha = copy.deepcopy(self.ledger)
        bad_sha['mixed_qos_draft_oracles'][0]['source_sha256'] = '0' * 64
        with self.assertRaises(InputError):
            validate_ledger(bad_sha, catalog=self.catalog)

        bad_marker = copy.deepcopy(self.ledger)
        bad_marker['mixed_qos_draft_oracles'][0]['source_text'] = 'No official tag'
        bad_marker['mixed_qos_draft_oracles'][0]['source_sha256'] = hashlib.sha256(b'No official tag').hexdigest()
        with self.assertRaises(InputError):
            validate_ledger(bad_marker, catalog=self.catalog)

    def test_applicability_and_should_deviation_rules_enforced(self):
        missing_excl = copy.deepcopy(self.ledger)
        missing_excl['selected_id_oracles'][0]['applicability'] = 'excluded'
        missing_excl['selected_id_oracles'][0]['exclusion_rationale'] = None
        with self.assertRaises(InputError):
            validate_ledger(missing_excl, catalog=self.catalog)

        missing_cond = copy.deepcopy(self.ledger)
        missing_cond['internal_obligation_oracles'][0]['applicability'] = 'conditional'
        missing_cond['internal_obligation_oracles'][0]['condition'] = None
        with self.assertRaises(InputError):
            validate_ledger(missing_cond, catalog=self.catalog)

        empty_should = copy.deepcopy(self.ledger)
        empty_should['selected_id_oracles'][0]['normative_level'] = 'SHOULD'
        empty_should['selected_id_oracles'][0]['should_deviation'] = ''
        with self.assertRaises(InputError):
            validate_ledger(empty_should, catalog=self.catalog)

        valid_should = copy.deepcopy(self.ledger)
        valid_should['selected_id_oracles'][0]['normative_level'] = 'SHOULD'
        valid_should['selected_id_oracles'][0]['should_deviation'] = 'Bounded timing deviation recorded'
        summary = evaluate_sufficiency(valid_should, self._all_passing_outcomes())
        self.assertEqual(len(summary['should_deviations']), 1)
        self.assertEqual(summary['should_deviations'][0]['id'], valid_should['selected_id_oracles'][0]['id'])

    def test_non_offsetting_verdicts_and_alternative_groups(self):
        # 1. Single FAIL cannot be offset even if all other items pass
        outcomes = self._all_passing_outcomes()
        outcomes = [
            {'case_id': r['case_id'], 'status': 'FAIL', 'evidence_validated': True}
            if r['case_id'] == 'MQTT311-RETAIN-DELETE-001' else r
            for r in outcomes
        ]
        res_fail = evaluate_sufficiency(self.ledger, outcomes)
        self.assertEqual(res_fail['conformance_verdict'], 'FAIL')
        self.assertEqual(res_fail['overall_status'], 'Partial')

        # 2. PASS with evidence_validated=False escalates to ERROR
        outcomes = [
            {'case_id': r['case_id'], 'status': 'PASS', 'evidence_validated': False}
            if r['case_id'] == 'MQTT311-CONNECT-001' else r
            for r in self._all_passing_outcomes()
        ]
        res_err = evaluate_sufficiency(self.ledger, outcomes)
        self.assertEqual(res_err['conformance_verdict'], 'ERROR')

        # 3. Single BLOCKED prevents PASS/CONFORMANT
        outcomes = [
            {'case_id': r['case_id'], 'status': 'BLOCKED', 'evidence_validated': False}
            if r['case_id'] == 'MQTT311-TOPIC-MATCH-001' else r
            for r in self._all_passing_outcomes()
        ]
        res_blk = evaluate_sufficiency(self.ledger, outcomes)
        self.assertEqual(res_blk['conformance_verdict'], 'BLOCKED')
        self.assertEqual(res_blk['overall_status'], 'Partial')

        # 4. Missing outcome counts as NOT_TESTED
        outcomes = [r for r in self._all_passing_outcomes() if r['case_id'] != 'MQTT311-MIXED-QOS-001']
        res_nt = evaluate_sufficiency(self.ledger, outcomes)
        self.assertEqual(res_nt['conformance_verdict'], 'NOT_TESTED')

        # 5. Alternative group: RECOVER fails or is NOT_TESTED, but FLOW+FANOUT+DUPLEX group passes
        outcomes = [r for r in self._all_passing_outcomes() if r['case_id'] != 'MQTT311-QOS2-RECOVER-001']
        # Note: other entries referencing RECOVER still count as NOT_TESTED globally,
        # so isolate a single-entry alternative_groups check via summary item inspection.
        res_alt = evaluate_sufficiency(self.ledger, outcomes)
        by_id = {item['id']: item for item in res_alt['items']}
        self.assertEqual(by_id['MQTT-2.3.1-6']['status'], 'PASS')

        # 6. All PASS + validated evidence stays INCONCLUSIVE & Partial without independent human approval
        res_all_ok = evaluate_sufficiency(self.ledger, self._all_passing_outcomes(), independent_approvals=0)
        self.assertEqual(res_all_ok['counts']['FAIL'], 0)
        self.assertEqual(res_all_ok['counts']['ERROR'], 0)
        self.assertEqual(res_all_ok['counts']['BLOCKED'], 0)
        self.assertEqual(res_all_ok['counts']['NOT_TESTED'], 0)
        self.assertEqual(res_all_ok['overall_status'], 'Partial')
        self.assertEqual(res_all_ok['conformance_verdict'], 'INCONCLUSIVE')


if __name__ == '__main__':
    unittest.main()
