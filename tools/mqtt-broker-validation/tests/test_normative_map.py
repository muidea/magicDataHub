"""A selected mapping binds evidence but cannot manufacture normative approval."""
import copy
import hashlib
from html import escape
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common import InputError, ROOT, read_json
import normative_map as mapping


class NormativeMapping(unittest.TestCase):
    def setUp(self):
        self.value = read_json(ROOT / 'normative-map.json')
        self.catalog = read_json(ROOT / 'catalog.json')
        self.temporary = tempfile.TemporaryDirectory(prefix='mqtt-mapping-check-')
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name)

    def validate(self, value):
        return mapping.validate_mapping(value, self.catalog)

    def source_fixture(self):
        paragraphs = ''.join('<p>' + p['source_text'] + '</p>' for p in self.value['paragraphs'])
        compounds = ''
        for group in self.value['compound_requirements']:
            for block in group['source_blocks']:
                tag = block['kind']
                compounds += '<ul>' if tag == 'ul-start' else '</ul>' if tag == 'ul-end' else '<' + tag + '>' + escape(block['text']) + '</' + tag + '>'
            compounds += ''.join('<p>' + escape(p['source_text']) + '</p>' for p in group['context_paragraphs'])
        body = (paragraphs + compounds + '<p class="AppendixHeading1">Appendix</p>').encode('cp1252')
        value = copy.deepcopy(self.value)
        value['source']['sha256'] = hashlib.sha256(body).hexdigest()
        path = self.cache / 'fixture.html'
        path.write_bytes(body)
        return value, path, body

    def test_selected_mapping_preserves_pending_state_and_excludes_qos0_obligation(self):
        self.validate(self.value)
        self.assertEqual({r['official_id'] for r in self.value['mappings']}, mapping.SELECTED_IDS)
        self.assertNotIn('MQTT-3.3.1-7', mapping.SELECTED_IDS)
        self.assertTrue(all(o['official_ids'] == [] for o in self.catalog['obligations']))

    def test_metadata_cannot_claim_approval_complete_coverage_or_runtime_pass(self):
        for changes in [dict(mapping_status='complete'), dict(normative_review='approved'),
                        dict(independent_approvals=1), dict(independent_approvals=False),
                        dict(runtime_results='PASS'), dict(schema_version=True), dict(approved=True)]:
            with self.subTest(changes=changes), self.assertRaises(InputError):
                self.validate(dict(self.value, **changes))
        for changes in [dict(review_status='approved'), dict(evidence_claim='CompleteCoverage')]:
            value = copy.deepcopy(self.value)
            value['mappings'][0].update(changes)
            with self.assertRaises(InputError):
                self.validate(value)

    def test_missing_duplicate_or_unselected_official_id_is_rejected(self):
        for mutate in [lambda v: v['mappings'].pop(),
                       lambda v: v['mappings'].append(v['mappings'][0]),
                       lambda v: v['mappings'][0].update(official_id='MQTT-3.3.1-7'),
                       lambda v: v['paragraphs'].append(v['paragraphs'][0])]:
            value = copy.deepcopy(self.value)
            mutate(value)
            with self.assertRaises(InputError):
                self.validate(value)

    def test_connection_ids_cannot_be_attached_to_retained_case_or_lose_gap_binding(self):
        for changes in [dict(case_id='MQTT311-RETAIN-DELETE-001'),
                        dict(required_phases=['pingresp-received']),
                        dict(required_assertions=['expected-control-packet'])]:
            value=copy.deepcopy(self.value)
            next(r for r in value['mappings'] if r['official_id']=='MQTT-3.2.0-1').update(changes)
            with self.assertRaises(InputError): self.validate(value)
        value=copy.deepcopy(self.value)
        value['gaps'][-1]['case_id']='MQTT311-RETAIN-DELETE-001'
        with self.assertRaises(InputError): self.validate(value)

    def test_qos2_mapping_requires_all_four_branches_and_its_own_case(self):
        for changes in [dict(case_id='MQTT311-CONNECT-001'),dict(required_phases=['broker-to-client/AwaitPubRec']),
                        dict(scenario_version='1.0.0')]:
            value=copy.deepcopy(self.value)
            next(r for r in value['mappings'] if r['official_id']=='MQTT-4.4.0-1').update(changes)
            with self.assertRaises(InputError): self.validate(value)
        self.assertNotIn('MQTT-4.3.3-1',mapping.SELECTED_IDS)
        self.assertNotIn('MQTT-4.3.3-2',mapping.SELECTED_IDS)

    def test_topic_mapping_is_bound_to_selected_vectors_case_and_version(self):
        for changes in [dict(case_id='MQTT311-QOS2-RECOVER-001'),
                        dict(required_phases=['vector-0']), dict(scenario_version='1.0.0')]:
            value=copy.deepcopy(self.value)
            next(r for r in value['mappings'] if r['official_id']=='MQTT-4.7.2-1').update(changes)
            with self.assertRaises(InputError): self.validate(value)
        # Valid wildcard matching probes do not prove invalid syntax rejection.
        self.assertNotIn('MQTT-4.7.1-2',mapping.SELECTED_IDS)
        self.assertNotIn('MQTT-4.7.1-3',mapping.SELECTED_IDS)

    def test_source_paragraph_version_assertion_and_per_id_phase_bindings_are_checked(self):
        for mutate in [lambda v: v['paragraphs'][0].update(sha256='0' * 64),
                       lambda v: v['mappings'][0].update(paragraph_id='RETAIN-P4'),
                       lambda v: v['mappings'][0].update(scenario_version='1.0.0'),
                       lambda v: v['mappings'][0].update(required_assertions=['unrelated']),
                       lambda v: v['mappings'][0].update(required_phases=['cleanup-completed']),
                       lambda v: v['source'].update(url='https://example.invalid/spec')]:
            value = copy.deepcopy(self.value)
            mutate(value)
            with self.assertRaises(InputError):
                self.validate(value)
        catalog = copy.deepcopy(self.catalog)
        next(c for c in catalog['cases'] if c.get('entrypoint') == 'retained')['scenario_version'] = '1.0.0'
        with self.assertRaises(InputError):
            mapping.validate_mapping(self.value, catalog)

    def test_source_verification_checks_digest_paragraphs_and_main_body_boundary(self):
        value, path, body = self.source_fixture()
        self.assertEqual(mapping.verify_source(value, path), path)
        for changed in [body + b'changed', body.replace(b'AppendixHeading1', b'OtherHeading'),
                        body.replace(b'future subscribers', b'other subscribers')]:
            path.write_bytes(changed)
            candidate = copy.deepcopy(value)
            if changed != body + b'changed':
                candidate['source']['sha256'] = hashlib.sha256(changed).hexdigest()
            with self.assertRaises(InputError):
                mapping.verify_source(candidate, path)

    def test_compound_inventory_imports_all_musts_without_promoting_parent_mapping(self):
        groups=self.value['compound_requirements']
        self.assertEqual([len(g['atoms']) for g in groups],[7,5])
        self.assertEqual([len(g['source_blocks']) for g in groups],[10,8])
        for group in groups:
            self.assertNotIn(group['official_id'], mapping.SELECTED_IDS)
            self.assertEqual(group['mapping_status'],'inventory-only')
            self.assertTrue(all(a['evidence_status']=='partial-selected-evidence' for a in group['atoms']))

    def test_compound_atoms_cannot_omit_must_change_span_role_case_or_claim_approval(self):
        mutations=[lambda g:g['atoms'].pop(),lambda g:g['source_blocks'].pop(2),
                   lambda g:g['atoms'][0].update(must_offset=1),
                   lambda g:g['atoms'][0].update(source_text='MUST do something'),
                   lambda g:g['atoms'][0].update(normative_level='MUST NOT'),
                   lambda g:g['atoms'][0].update(required_branches=['client-to-broker/AwaitPubRec']),
                   lambda g:g['atoms'][0].update(scenario_version='2.0.0'),
                   lambda g:g['atoms'][0].update(oracle='qos2_evidence.receiver_proof'),
                   lambda g:g['atoms'][0].update(evidence_status='complete'),
                   lambda g:g.update(normative_review='approved'),
                   lambda g:g.update(independent_approvals=1),
                   lambda g:g.update(mapping_status='mapped'),
                   lambda g:g['context_paragraphs'].pop(),
                   lambda g:g['context_paragraphs'][0].update(source_text='The Packet Identifier becomes available after PUBACK.')]
        for mutate in mutations:
            value=copy.deepcopy(self.value);mutate(value['compound_requirements'][0])
            with self.subTest(mutation=mutate),self.assertRaises(InputError):self.validate(value)

    def test_compound_supplement_is_bound_to_flow_case_phases_role_and_pending_review(self):
        supplements=[a['supplemental_evidence'] for g in self.value['compound_requirements'] for a in g['atoms']]
        self.assertEqual(sum(e['case_id']=='MQTT311-QOS2-FLOW-001' for s in supplements for e in s),11)
        self.assertEqual(sum(e['case_id']=='MQTT311-QOS2-FANOUT-001' for s in supplements for e in s),5)
        for changes in [dict(case_id='MQTT311-QOS2-RECOVER-001'),dict(scenario_version='3.0.0'),
                        dict(server_role='receiver'),dict(required_phases=['health-completed']),
                        dict(oracle='qos2_evidence.check_evidence'),dict(review_status='approved'),
                        dict(evidence_status='complete')]:
            value=copy.deepcopy(self.value)
            value['compound_requirements'][0]['atoms'][0]['supplemental_evidence'][0].update(changes)
            with self.subTest(changes=changes),self.assertRaises(InputError):self.validate(value)
        value=copy.deepcopy(self.value)
        value['compound_requirements'][0]['atoms'][0]['supplemental_evidence']=[]
        with self.assertRaises(InputError):self.validate(value)
        catalog=copy.deepcopy(self.catalog)
        next(c for c in catalog['cases'] if c.get('entrypoint')=='qos2_flow')['scenario_version']='2.0.0'
        with self.assertRaises(InputError):mapping.validate_mapping(self.value,catalog)

    def test_fanout_supplement_cannot_omit_or_change_role_case_phases_or_review(self):
        for changes in [dict(case_id='MQTT311-QOS2-FLOW-001'),dict(scenario_version='2.0.0'),
                        dict(server_role='sender'),dict(required_phases=['health-fanout-completed']),
                        dict(oracle='qos2_flow_evidence.check_evidence'),dict(review_status='approved')]:
            value=copy.deepcopy(self.value)
            value['compound_requirements'][1]['atoms'][0]['supplemental_evidence'][-1].update(changes)
            with self.subTest(changes=changes),self.assertRaises(InputError):self.validate(value)
        for mutation in ['omit','duplicate','wrong-catalog-version']:
            value=copy.deepcopy(self.value);catalog=copy.deepcopy(self.catalog)
            evidence=value['compound_requirements'][1]['atoms'][0]['supplemental_evidence']
            if mutation=='omit':evidence.pop()
            elif mutation=='duplicate':evidence.append(evidence[-1])
            else:next(c for c in catalog['cases'] if c.get('entrypoint')=='qos2_fanout')['scenario_version']='2.0.0'
            with self.subTest(mutation=mutation),self.assertRaises(InputError):mapping.validate_mapping(value,catalog)

    def test_duplex_supplement_requires_both_completion_orders_roles_and_current_case(self):
        records=[s for g in self.value['compound_requirements'] for a in g['atoms'] for s in a['supplemental_evidence'] if s['case_id']=='MQTT311-QOS2-DUPLEX-001']
        self.assertEqual(len(records),3)
        for changes in [dict(case_id='MQTT311-QOS2-FLOW-001'),dict(scenario_version='2.0.0'),
                        dict(server_role='receiver'),dict(required_phases=['overlap-established']),
                        dict(required_branches=['incoming-first']),dict(oracle='qos2_flow_evidence.check_evidence'),
                        dict(review_status='approved')]:
            value=copy.deepcopy(self.value)
            atom=next(a for a in value['compound_requirements'][0]['atoms'] if a['key']=='pubrel-original-id')
            atom['supplemental_evidence'][-1].update(changes)
            with self.subTest(changes=changes),self.assertRaises(InputError):self.validate(value)
        for mutation in ['omit','duplicate','wrong-case-version']:
            value=copy.deepcopy(self.value);catalog=copy.deepcopy(self.catalog)
            atom=next(a for a in value['compound_requirements'][0]['atoms'] if a['key']=='pubrel-original-id')
            if mutation=='omit':atom['supplemental_evidence'].pop()
            elif mutation=='duplicate':atom['supplemental_evidence'].append(atom['supplemental_evidence'][-1])
            else:next(c for c in catalog['cases'] if c.get('entrypoint')=='qos2_duplex')['scenario_version']='2.0.0'
            with self.subTest(mutation=mutation),self.assertRaises(InputError):mapping.validate_mapping(value,catalog)

    def test_source_full_list_order_boundaries_context_and_appendix_are_verified(self):
        value,path,body=self.source_fixture()
        self.assertEqual(mapping.verify_source(value,path),path)
        for changed in [body.replace(b'<li>',b'<p>').replace(b'</li>',b'</p>'),
                        body.replace(b'MUST NOT re-send the',b'MUST re-send the'),
                        body.replace(b'PUBCOMP Packet.',b'PUBACK Packet.'),
                        body.replace(b'<ul>',b'<ol>').replace(b'</ul>',b'</ol>'),
                        body.replace(b'In the QoS 2 delivery protocol, the Sender',b'Other sender')]:
            candidate=copy.deepcopy(value);candidate['source']['sha256']=hashlib.sha256(changed).hexdigest()
            path.write_bytes(changed)
            with self.assertRaises(InputError):mapping.verify_source(candidate,path)
        # Complete list appearing only after the appendix must not be imported.
        changed=b'<p class="AppendixHeading1">Appendix</p>'+body
        candidate=copy.deepcopy(value);candidate['source']['sha256']=hashlib.sha256(changed).hexdigest()
        path.write_bytes(changed)
        with self.assertRaises(InputError):mapping.verify_source(candidate,path)

    def test_source_symlink_and_missing_file_are_rejected(self):
        value, path, _ = self.source_fixture()
        link = self.cache / 'linked.html'
        link.symlink_to(path)
        for candidate in [link, self.cache / 'missing.html']:
            with self.assertRaises(InputError):
                mapping.verify_source(value, candidate)

    def test_explicit_fetch_verifies_bytes_and_reuses_cache_without_network(self):
        value, _, body = self.source_fixture()
        with patch.object(mapping.urllib.request, 'urlopen', return_value=io.BytesIO(body)) as network:
            path = mapping.fetch_source(value, self.cache)
            network.assert_called_once()
        self.assertEqual(path.read_bytes(), body)
        with patch.object(mapping.urllib.request, 'urlopen') as network:
            self.assertEqual(mapping.fetch_source(value, self.cache), path)
            network.assert_not_called()

    def test_fetch_preserves_invalid_existing_cache_and_rejects_bad_download(self):
        value, _, _ = self.source_fixture()
        path = self.cache / value['source']['cache_file']
        path.parent.mkdir()
        path.write_bytes(b'old')
        with patch.object(mapping.urllib.request, 'urlopen') as network, self.assertRaises(InputError):
            mapping.fetch_source(value, self.cache)
        network.assert_not_called()
        self.assertEqual(path.read_bytes(), b'old')
        path.unlink()
        with patch.object(mapping.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong')), self.assertRaises(InputError):
            mapping.fetch_source(value, self.cache)
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.iterdir()), [])

    def test_fetch_rejects_symlink_cache_parent_before_network_or_writes(self):
        outside = self.cache / 'outside'
        outside.mkdir()
        link = self.cache / 'linked'
        link.symlink_to(outside, target_is_directory=True)
        with patch.object(mapping.urllib.request, 'urlopen') as network, self.assertRaises(InputError):
            mapping.fetch_source(self.value, link / 'nested')
        network.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_runner_rejects_bad_mapping_before_dependency_or_resource_operations(self):
        import run
        with patch.object(run, 'load_mapping', side_effect=InputError('bad mapping')), \
                patch.object(run, 'dependencies') as dependencies, \
                patch.object(run, 'allocate_resources') as allocate, patch.object(run, 'supervise') as supervise:
            with self.assertRaises(InputError):
                run.execute(run.parser().parse_args(['--check']))
            for operation in [dependencies, allocate, supervise]:
                operation.assert_not_called()


if __name__ == '__main__':
    unittest.main()
