"""Run real unittest cases and emit a JUnit XML report."""

import argparse
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append((test.id(), None, ""))

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.cases.append((test.id(), "failure", self._exc_info_to_string(error, test)))

    def addError(self, test, error):
        super().addError(test, error)
        self.cases.append((test.id(), "error", self._exc_info_to_string(error, test)))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.cases.append((test.id(), "skipped", reason))

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error is not None:
            kind = "failure" if issubclass(error[0], test.failureException) else "error"
            self.cases.append((subtest.id(), kind, self._exc_info_to_string(error, test)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(Path.cwd()))
    suite = unittest.defaultTestLoader.discover(args.start, pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    root = ET.Element(
        "testsuite",
        tests=str(len(result.cases)),
        failures=str(len(result.failures)),
        errors=str(len(result.errors)),
        skipped=str(len(result.skipped)),
    )
    for identifier, outcome, details in result.cases:
        classname, _, name = identifier.rpartition(".")
        case = ET.SubElement(root, "testcase", classname=classname, name=name)
        if outcome:
            ET.SubElement(case, outcome).text = details
    destination = Path(args.report)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=True)
    return 0 if result.wasSuccessful() and result.testsRun else 1


if __name__ == "__main__":
    raise SystemExit(main())
