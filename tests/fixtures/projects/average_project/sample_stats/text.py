"""A same-named function that is unrelated to numeric statistics."""


def average(words):
    """Return the average word length for a nonempty sequence."""
    return sum(len(word) for word in words) / len(words)
