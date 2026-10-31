"""A caller whose existing behavior must survive changes to average."""

from sample_stats.statistics import average


def summarize(values):
    return f"average={average(values):.2f}"
