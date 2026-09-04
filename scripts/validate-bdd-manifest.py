#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from pathlib import Path

DETERMINISTIC = {
    "BDD-S01",
    "BDD-S02",
    "BDD-S03",
    "BDD-S04",
    "BDD-S05",
    "BDD-S05B",
    "BDD-S06",
    "BDD-S07",
    "BDD-S08",
    "BDD-S09",
    "BDD-P01",
    "BDD-P02",
    "BDD-P03",
    "BDD-P04",
    "BDD-P05",
    "BDD-P06",
    "BDD-P07",
    "BDD-P08",
    "BDD-P09",
    "BDD-P10",
    "BDD-P11",
    "BDD-P12",
    "BDD-Z01",
    "BDD-Z02",
    "BDD-Z03",
    "BDD-Z04",
    "BDD-Z05",
    "BDD-Z06",
    "BDD-Z07",
    "BDD-Z08",
    "BDD-Z09",
    "BDD-Z09B",
    "BDD-Z10",
    "BDD-R01",
    "BDD-R02",
    "BDD-R03",
    "BDD-R04",
    "BDD-R05",
    "BDD-R06",
    "BDD-R07",
    "BDD-R08",
    "BDD-R09",
    "BDD-R10",
    "BDD-R11",
    "BDD-R12",
    "BDD-V01",
    "BDD-V02",
    "BDD-V03",
    "BDD-V04",
    "BDD-V05",
    "BDD-V06",
    "BDD-L01",
    "BDD-L02",
    "BDD-L03",
    "BDD-L04",
    "BDD-L05",
    "BDD-I01",
    "BDD-I02",
    "BDD-I03",
    "BDD-I04",
    "BDD-I04B",
    "BDD-I05",
    "BDD-I06",
    "BDD-I07",
    "BDD-I08",
    "BDD-I08B",
    "BDD-I08C",
    "BDD-I09",
    "BDD-E01",
    "BDD-E02",
    "BDD-E04",
    "BDD-E05",
    "BDD-Q01",
    "BDD-Q01B",
    "BDD-Q02",
    "BDD-Q04A",
}
EXTERNAL = {"BDD-E03", "BDD-E06", "BDD-Q03", "BDD-Q04B", "BDD-Q05"}


class ContractError(RuntimeError):
    pass


def _load_unique(path: Path) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ContractError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ContractError(f"unsafe JSON document: {path}")
    if metadata.st_size > 1024 * 1024:
        raise ContractError(f"oversized JSON document: {path}")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=unique_object
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"invalid JSON: {path}") from error


def validate(manifest_path: Path, report_path: Path) -> None:
    manifest = _load_unique(manifest_path)
    report = _load_unique(report_path)
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schemaVersion", "scenarios"}
        or type(manifest.get("schemaVersion")) is not int
        or manifest.get("schemaVersion") != 1
    ):
        raise ContractError("invalid manifest schema")
    scenarios = manifest.get("scenarios")
    if not isinstance(scenarios, dict):
        raise ContractError("manifest scenarios must be an object")
    expected = DETERMINISTIC | EXTERNAL
    missing = sorted(expected - set(scenarios))
    unknown = sorted(set(scenarios) - expected)
    if missing:
        raise ContractError(f"missing scenarios: {', '.join(missing)}")
    if unknown:
        raise ContractError(f"unknown scenarios: {', '.join(unknown)}")

    scenario_tests: dict[str, list[str]] = {}
    mapped_tests: set[str] = set()
    for scenario_id in sorted(DETERMINISTIC):
        entry = scenarios[scenario_id]
        if (
            not isinstance(entry, dict)
            or set(entry) != {"status", "tests"}
            or entry.get("status") != "deterministic"
        ):
            raise ContractError(f"invalid deterministic scenario: {scenario_id}")
        tests = entry.get("tests")
        if (
            not isinstance(tests, list)
            or not tests
            or not all(isinstance(test_id, str) and test_id for test_id in tests)
        ):
            raise ContractError(f"missing tests for {scenario_id}")
        if len(tests) != len(set(tests)):
            raise ContractError(f"duplicate test mapping for {scenario_id}")
        scenario_tests[scenario_id] = tests
        mapped_tests.update(tests)

    receipts: list[str] = []
    for scenario_id in sorted(EXTERNAL):
        entry = scenarios[scenario_id]
        if (
            not isinstance(entry, dict)
            or set(entry) != {"status", "tests", "receipt"}
            or entry.get("status") != "external-gate"
        ):
            raise ContractError(f"invalid external scenario: {scenario_id}")
        if entry.get("tests") != []:
            raise ContractError(f"external scenario must not map tests: {scenario_id}")
        receipt = entry.get("receipt")
        if (
            not isinstance(receipt, str)
            or re.fullmatch(r"\.release/evidence/[A-Za-z0-9._-]+\.json", receipt)
            is None
        ):
            raise ContractError(f"invalid external receipt for {scenario_id}")
        receipts.append(receipt)
    if len(receipts) != len(set(receipts)):
        raise ContractError("duplicate external receipt mapping")

    if (
        not isinstance(report, dict)
        or set(report)
        not in (
            {"tests", "summary"},
            {"schemaVersion", "tests", "summary"},
        )
        or type(report.get("schemaVersion", 1)) is not int
        or report.get("schemaVersion", 1) != 1
    ):
        raise ContractError("invalid test report schema")
    results = report.get("tests")
    if not isinstance(results, list):
        raise ContractError("test report has no test list")
    statuses: dict[str, str] = {}
    for item in results:
        if not isinstance(item, dict) or set(item) != {"id", "status"}:
            raise ContractError("invalid test report entry")
        test_id = item.get("id")
        status = item.get("status")
        if not isinstance(test_id, str) or status not in {
            "passed",
            "failed",
            "error",
            "skipped",
        }:
            raise ContractError("invalid test report entry")
        if test_id in statuses:
            raise ContractError(f"duplicate test result: {test_id}")
        statuses[test_id] = status
    unknown_results = sorted(set(statuses) - mapped_tests)
    if unknown_results:
        raise ContractError(f"unknown test results: {', '.join(unknown_results)}")
    skipped = sorted(
        test_id for test_id, status in statuses.items() if status == "skipped"
    )
    if skipped:
        raise ContractError(f"skipped tests are not accepted: {', '.join(skipped)}")

    for scenario_id in sorted(DETERMINISTIC):
        for test_id in scenario_tests[scenario_id]:
            if test_id not in statuses:
                raise ContractError(f"missing test result for {scenario_id}: {test_id}")
            if statuses[test_id] != "passed":
                raise ContractError(
                    f"unpassed test for {scenario_id}: {test_id} ({statuses[test_id]})"
                )

    summary = report.get("summary")
    if (
        not isinstance(summary, dict)
        or set(summary) != {"passed", "failed", "errors", "skipped"}
        or any(type(summary[key]) is not int or summary[key] < 0 for key in summary)
        or summary.get("skipped") != 0
    ):
        raise ContractError("test report summary contains skipped tests")
    observed_summary = {
        "passed": sum(status == "passed" for status in statuses.values()),
        "failed": sum(status == "failed" for status in statuses.values()),
        "errors": sum(status == "error" for status in statuses.values()),
        "skipped": sum(status == "skipped" for status in statuses.values()),
    }
    if summary != observed_summary:
        raise ContractError("test report summary is inconsistent")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--test-report", type=Path, required=True)
    options = parser.parse_args()
    try:
        validate(options.manifest, options.test_report)
    except (OSError, ContractError) as error:
        print(f"bdd-manifest: {error}", file=sys.stderr)
        return 1
    print("BDD manifest is complete and all deterministic scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
