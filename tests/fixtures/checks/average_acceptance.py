"""Independent requirement oracle; run with the sample project as cwd."""

import unittest

from sample_stats.statistics import average
from sample_stats.summary import summarize


class AverageAcceptanceTests(unittest.TestCase):
    def test_empty_input_returns_none(self):
        self.assertIsNone(average([]))

    def test_single_zero(self):
        self.assertEqual(average([0]), 0.0)

    def test_existing_numeric_examples(self):
        for values, expected in [([7], 7.0), ([2, 4], 3.0), ([-2, 2], 0.0)]:
            with self.subTest(values=values):
                self.assertEqual(average(values), expected)

    def test_input_is_not_modified(self):
        values = [4, 2]
        average(values)
        self.assertEqual(values, [4, 2])

    def test_caller_still_formats_result(self):
        self.assertEqual(summarize([2, 4]), "average=3.00")


if __name__ == "__main__":
    unittest.main()
