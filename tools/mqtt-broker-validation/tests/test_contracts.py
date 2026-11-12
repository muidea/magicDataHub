"""Data contracts reject ambiguous input; concurrent trace order stays observable."""
import copy
from pathlib import Path
import tempfile
import threading
import unittest

from common import InputError, ROOT, decode_json, read_json
from schema_check import check, load_schema, schema_definition
from wire import Trace


class Contracts(unittest.TestCase):
    def test_concurrent_trace_events_keep_contiguous_sequence_and_monotonic_time(self):
        with tempfile.TemporaryDirectory(prefix='mqtt-contract-check-') as directory:
            trace = Trace(Path(directory) / 'trace.jsonl')
            errors = []
            def emit():
                try:
                    for _ in range(20):
                        trace.check(True, 'concurrent-control')
                except Exception as error:
                    errors.append(error)
            threads = [threading.Thread(target=emit) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not x.is_alive() for x in threads))
            self.assertFalse(errors, errors)
            events = [decode_json(line) for line in trace.path.read_text().splitlines()]
            self.assertEqual([x['sequence'] for x in events], list(range(80)))
            self.assertEqual([x['seconds'] for x in events], sorted(x['seconds'] for x in events))
            for event in events:
                check('event', event)
            for metadata in [dict(sequence=0), dict(seconds=0), dict(schema_version=1)]:
                with self.assertRaises(InputError):
                    trace.event('assertion', assertion='override', passed=True, **metadata)

    def test_strict_json_rejects_nested_duplicates_and_nonfinite_values(self):
        for body in ['{"nested":{"pass":false,"pass":true}}', '{"seconds":NaN}',
                     '[Infinity]', '[-Infinity]', '{"seconds":1e999}']:
            with self.assertRaises(InputError):
                decode_json(body)
        self.assertEqual(decode_json('{"seconds":0.125,"passed":false}'), {'seconds': 0.125, 'passed': False})

    def test_schema_definition_rejects_malformed_or_unsupported_validation_rules(self):
        for schema in [{'allOf': []}, {'type': 'bogus'}, {'uniqueItems': 'true'},
                       {'additionalProperties': 'false'}, {'minimum': True}, {'required': 'passed'},
                       {'prefixItems': {}}, {'oneOf': []}, {'pattern': '['}]:
            with self.assertRaises(InputError):
                schema_definition(schema)

    def test_tuple_and_event_discriminator_are_enforced(self):
        base = dict(schema_version=1, sequence=0, seconds=0, kind='connection', connection='unit', peer=['127.0.0.1', 1883])
        check('event', base)
        for peer in [[1883, '127.0.0.1'], ['127.0.0.1', True], ['127.0.0.1', 65536], ['127.0.0.1'], ['127.0.0.1', 1883, 0]]:
            with self.assertRaises(InputError):
                check('event', dict(base, peer=peer))
        with self.assertRaises(InputError):
            check('event', dict(base, kind='bytes'))

    def test_oneof_requires_one_branch_and_unknown_keywords_fail_preflight(self):
        for path in (ROOT / 'schemas').glob('*.schema.json'):
            name = path.name.removesuffix('.schema.json')
            schema_definition(load_schema(name))
        schema = copy.deepcopy(load_schema('event'))
        schema['oneOf'].append(schema['oneOf'][0])
        event = dict(schema_version=1, sequence=0, seconds=0, kind='case-start',
                     run_id='unit', attempt_id='attempt', target='tool', case_id='TOOL-CAL-001', profile_binding=None)
        from schema_check import validate
        with self.assertRaises(InputError):
            validate(event, schema)

    def test_upstream_event_accepts_every_locked_parameter_set_and_rejects_extra_options(self):
        lock = read_json(ROOT / 'upstream.lock.json')
        base = dict(kind='upstream-selection', schema_version=1, sequence=0, seconds=0,
                    file='fixture.py', sha256='0' * 64, reuse='B', limitations=['bounded control'])
        for case in lock['mosquitto']['cases']:
            check('event', dict(base, parameters=case['parameters']))
        for parameters in [dict(proto_ver=4, clean_session='false'), dict(proto_ver=4, unrestricted=True)]:
            with self.assertRaises(InputError):
                check('event', dict(base, parameters=parameters))


if __name__ == '__main__':
    unittest.main()
