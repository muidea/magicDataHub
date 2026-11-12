"""Cleanup must preserve resources when ownership cannot be proven."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lifecycle
from common import write_json


class Ownership(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='mqtt-broker-instance-')
        self.addCleanup(self.directory.cleanup)
        self.resources = dict(name='unit-owned-name', volume='unit-owned-volume',
                              token='unit-token', directory=self.directory.name)
        write_json(Path(self.directory.name) / '.owner.json', {'token': 'unit-token'})

    def test_foreign_container_is_preserved(self):
        foreign = {'Config': {'Labels': {'mqtt-validation.owner': 'someone-else'}}}
        with patch.object(lifecycle, 'inspect_optional', return_value=foreign), patch.object(lifecycle, 'command') as mutation:
            result = lifecycle.cleanup(self.resources)
        self.assertFalse(result['complete'])
        mutation.assert_not_called()
        self.assertTrue(Path(self.directory.name).exists())

    def test_foreign_volume_is_preserved(self):
        with patch.object(lifecycle, 'inspect_optional', side_effect=[None, {'Labels': {}}]), patch.object(lifecycle, 'command') as mutation:
            result = lifecycle.cleanup(self.resources)
        self.assertFalse(result['complete'])
        mutation.assert_not_called()
        self.assertTrue(Path(self.directory.name).exists())

    def test_unproven_temporary_directory_is_preserved(self):
        write_json(Path(self.directory.name) / '.owner.json', {'token': 'someone-else'})
        with patch.object(lifecycle, 'inspect_optional', return_value=None):
            result = lifecycle.cleanup(self.resources)
        self.assertFalse(result['complete'])
        self.assertTrue(Path(self.directory.name).exists())

    def test_empty_owned_journal_can_be_cleaned_idempotently(self):
        with patch.object(lifecycle, 'inspect_optional', return_value=None):
            self.assertTrue(lifecycle.cleanup(self.resources)['complete'])
            self.assertTrue(lifecycle.cleanup(self.resources)['complete'])
        self.assertFalse(Path(self.directory.name).exists())


if __name__ == '__main__':
    unittest.main()
