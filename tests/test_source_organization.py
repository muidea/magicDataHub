"""Reject the deployment mistakes that merge unrelated native databases."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('source_organization', SCRIPTS / 'verify-source-organization.py')
organization = importlib.util.module_from_spec(spec)
spec.loader.exec_module(organization)


class SourceOrganization(unittest.TestCase):
    def setUp(self):
        self.plan = {'sources': [
            {'id': 'first', 'kind': 'postgres', 'host': 'DB.EXAMPLE.', 'port': 5432,
             'database': 'native', 'schema': 'sales', 'virtual_database': 'business'},
            {'id': 'second', 'kind': 'postgres', 'host': 'db.example', 'port': 5432,
             'database': 'native', 'schema': 'public', 'virtual_database': 'business'},
        ]}
        self.sources = [
            {'id': record['id'], 'kind': record['kind'], 'status': 'Active', 'catalog_snapshot': 'snapshot',
             'desired': {'definition': {'allowed_schemas': [record['schema']], 'organization': {
                 'instance': {'host': record['host'], 'port': record['port']},
                 'database': record['database'], 'virtual_database': record['virtual_database']}}}}
            for record in self.plan['sources']]
        self.databases = [{'managed': True, 'config': {'name': 'business', 'schemas': [
            {'name': record['schema'], 'source': record['id'], 'remote_schema': record['schema']}
            for record in self.plan['sources']]}}]

    def test_same_native_database_merges_and_keeps_original_schemas(self):
        result = organization.verify(self.sources, self.databases, self.plan)
        self.assertEqual(result['native_databases'], 1)
        self.assertEqual(result['published_databases'], ['business'])

    def test_raw_source_registration_cannot_pass_as_automatic_organization(self):
        self.sources[0]['desired']['definition']['organization'] = None
        with self.assertRaisesRegex(ValueError, 'lacks native-database organization'):
            organization.verify(self.sources, self.databases, self.plan)

    def test_same_database_cannot_be_split_by_registration_alias(self):
        self.plan['sources'][1]['virtual_database'] = 'duplicate'
        with self.assertRaisesRegex(ValueError, 'split into multiple'):
            organization.verify(self.sources, self.databases, self.plan)

    def test_different_ports_or_database_case_cannot_be_collapsed(self):
        for field, value in [('port', 5433), ('database', 'Native')]:
            plan = deepcopy(self.plan)
            plan['sources'][1][field] = value
            with self.assertRaisesRegex(ValueError, 'collapsed into one'):
                organization.verify(self.sources, self.databases, plan)

    def test_renamed_schema_and_extra_aggregate_database_are_rejected(self):
        self.databases[0]['config']['schemas'][0]['name'] = 'first'
        with self.assertRaisesRegex(ValueError, 'renamed or incorrect'):
            organization.verify(self.sources, self.databases, self.plan)
        self.databases.append({'managed': False, 'config': {'name': 'e2e_live', 'schemas': []}})
        with self.assertRaisesRegex(ValueError, 'aggregate virtual database'):
            organization.verify(self.sources, self.databases, self.plan)

    def test_failed_activation_is_explicit_and_not_a_published_database(self):
        self.sources[0].update(status='Degraded', catalog_snapshot=None)
        self.databases[0]['config']['schemas'] = self.databases[0]['config']['schemas'][1:]
        result = organization.verify(self.sources, self.databases, self.plan)
        self.assertEqual(result['inactive_sources'], ['first'])


if __name__ == '__main__':
    unittest.main()
