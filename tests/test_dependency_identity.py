"""Reject source drift and normal edges; dev edges must not pollute service checks."""
import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('dependency', Path(__file__).resolve().parents[1] / 'scripts/verify-dependency-identity.py')
dependency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dependency)


class DependencyIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = dependency.metadata()

    def test_real_graph_and_source_rejection(self):
        dependency.verify(self.graph)
        graph = copy.deepcopy(self.graph)
        p = next(p for p in graph['packages'] if p['name'] == 'modorch')
        p['source'] = None
        with self.assertRaises(ValueError):
            dependency.verify(graph)

    def test_duplicate_business_package_is_rejected(self):
        graph = copy.deepcopy(self.graph)
        p = copy.deepcopy(next(p for p in graph['packages'] if p['name'] == 'modorch-fedengine'))
        p['id'] += '-other-source'
        graph['packages'].append(p)
        with self.assertRaises(ValueError):
            dependency.verify(graph)

    def test_dev_edges_are_excluded_but_normal_leak_is_rejected(self):
        graph = copy.deepcopy(self.graph)
        packages = {p['name']: p for p in graph['packages']}
        node = next(n for n in graph['resolve']['nodes'] if n['id'] == packages['mqtt-server']['id'])
        edge = {'pkg': packages['modorch-fedengine']['id'], 'dep_kinds': [{'kind': 'dev'}]}
        node['deps'].append(edge)
        dependency.verify(graph)
        edge['dep_kinds'][0]['kind'] = None
        with self.assertRaises(ValueError):
            dependency.verify(graph)


if __name__ == '__main__':
    unittest.main()
