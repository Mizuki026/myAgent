"""Produce deliberately misleading command/report outcomes for acceptance."""

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=["zero", "skipped", "missing", "corrupt", "failing"])
parser.add_argument("--report", required=True)
parser.add_argument("--suite", choices=["regression", "acceptance"], default="regression")
args = parser.parse_args()
report = Path(args.report)
report.parent.mkdir(parents=True, exist_ok=True)
if args.mode == "zero":
    report.write_text('<testsuite tests="0" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
elif args.mode == "corrupt":
    report.write_text("this is not XML", encoding="utf-8")

if args.mode in {"skipped", "failing"}:
    names = [
        "test_mean_of_positive_numbers", "test_single_value",
        "test_negative_and_positive_values", "test_input_is_not_modified",
        "test_existing_caller", "test_unrelated_same_named_function",
    ]
    classname = "test_statistics.OriginalBehaviorTests"
    if args.suite == "acceptance":
        names = ["test_empty_input_returns_none", "test_single_zero", "test_existing_numeric_examples",
                 "test_input_is_not_modified", "test_caller_still_formats_result"]
        classname = "test_requirement.AverageAcceptanceTests"
    root = ET.Element("testsuite", tests=str(len(names)), errors="0",
                      failures="1" if args.mode == "failing" else "0",
                      skipped="1" if args.mode == "skipped" else "0")
    for index, name in enumerate(names):
        case = ET.SubElement(root, "testcase", classname=classname, name=name)
        if index == 0:
            ET.SubElement(case, "skipped" if args.mode == "skipped" else "failure").text = "fixture fault"
    ET.ElementTree(root).write(report, encoding="utf-8", xml_declaration=True)
    if args.mode == "failing":
        raise SystemExit(1)
