import unittest

from sample_stats.statistics import average
from sample_stats.summary import summarize
from sample_stats.text import average as average_word_length


class OriginalBehaviorTests(unittest.TestCase):
    def test_mean_of_positive_numbers(self):
        self.assertEqual(average([2, 4]), 3.0)

    def test_single_value(self):
        self.assertEqual(average([7]), 7.0)

    def test_negative_and_positive_values(self):
        self.assertEqual(average([-2, 2]), 0.0)

    def test_input_is_not_modified(self):
        values = [4, 2]
        average(values)
        self.assertEqual(values, [4, 2])

    def test_existing_caller(self):
        self.assertEqual(summarize([2, 4]), "average=3.00")

    def test_unrelated_same_named_function(self):
        self.assertEqual(average_word_length(["a", "abc"]), 2.0)


if __name__ == "__main__":
    unittest.main()
