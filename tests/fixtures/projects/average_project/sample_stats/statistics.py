"""Numeric statistics with one intentionally missing requirement."""


def average(values):
    """Return the arithmetic mean of a nonempty sequence."""
    return sum(values) / len(values)
