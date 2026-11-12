import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "overlap_diagnostic", Path(__file__).resolve().parents[1] / "overlap_diagnostic.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OverlapDiagnosticTests(unittest.TestCase):
    def test_orders_and_filters_are_distinct_and_overlapping(self):
        self.assertEqual([name for name, _ in module.ORDERS],
                         ["exact-first", "wildcard-first", "plus-first"])
        selected = module.filters("validation/abc/overlap")
        self.assertEqual(selected["exact"], "validation/abc/overlap/item")
        self.assertEqual(selected["+"], "validation/abc/overlap/+")
        self.assertEqual(selected["#"], "validation/abc/overlap/#")

    def test_summary_counts_copies_and_qos(self):
        deliveries = [{"qos": 2}, {"qos": 2}, {"qos": 0}]
        self.assertEqual(module.summarize(deliveries), {"0": 1, "2": 2})

    def test_invalid_bounds_and_requests_fail_before_network(self):
        for qos, window in [(3, 0.4), (-1, 0.4), (2, 0), (2, -1)]:
            with self.assertRaises(ValueError):
                module.run(("127.0.0.1", 1), qos=qos, window=window)
        for requests in [(2, 2), (2, 2, 2, 2), (0, 1, 3), (-1, 2, 2)]:
            with self.assertRaises(ValueError):
                module.run(("127.0.0.1", 1), requests=requests)


if __name__ == "__main__":
    unittest.main()
