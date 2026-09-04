#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


class ReportingResult(unittest.TextTestResult):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.records: list[dict[str, str]] = []
        self._status: dict[str, str] = {}

    def startTest(self, test: unittest.TestCase) -> None:
        self._status[test.id()] = "passed"
        super().startTest(test)

    def addFailure(
        self,
        test: unittest.TestCase,
        err: tuple[type[BaseException], BaseException, object],
    ) -> None:
        self._status[test.id()] = "failed"
        super().addFailure(test, err)

    def addError(
        self,
        test: unittest.TestCase,
        err: tuple[type[BaseException], BaseException, object],
    ) -> None:
        self._status[test.id()] = "error"
        super().addError(test, err)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        self._status[test.id()] = "skipped"
        super().addSkip(test, reason)

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        self._status[test.id()] = "failed"
        super().addUnexpectedSuccess(test)

    def addExpectedFailure(
        self,
        test: unittest.TestCase,
        err: tuple[type[BaseException], BaseException, object],
    ) -> None:
        self._status[test.id()] = "failed"
        super().addExpectedFailure(test, err)

    def addSubTest(
        self,
        test: unittest.TestCase,
        subtest: unittest.TestCase,
        err: tuple[type[BaseException], BaseException, object] | None,
    ) -> None:
        if err is not None:
            self._status[test.id()] = (
                "failed" if issubclass(err[0], test.failureException) else "error"
            )
        super().addSubTest(test, subtest, err)

    def stopTest(self, test: unittest.TestCase) -> None:
        self.records.append({"id": test.id(), "status": self._status[test.id()]})
        super().stopTest(test)


def _write_report(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _contract_test_ids(repository: Path) -> set[str]:
    manifest = json.loads(
        (repository / "tests" / "bdd_manifest.json").read_text(encoding="utf-8")
    )
    return {
        test_id
        for entry in manifest["scenarios"].values()
        if entry.get("status") == "deterministic"
        for test_id in entry["tests"]
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    options = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    os.chdir(repository)
    sys.path.insert(0, str(repository))
    os.environ["CODEXBAR_TEST_RUNNER_CHILD"] = "1"

    suite = unittest.defaultTestLoader.discover(
        str(repository / "tests"), pattern="test_*.py", top_level_dir=str(repository)
    )
    runner = unittest.TextTestRunner(verbosity=2, resultclass=ReportingResult)
    result = runner.run(suite)
    assert isinstance(result, ReportingResult)
    contract_ids = _contract_test_ids(repository)
    records = [
        record
        for record in result.records
        if record["id"] in contract_ids
        or record["id"].rsplit(".", 1)[-1].startswith("test_bdd_")
    ]
    summary = {
        "passed": sum(record["status"] == "passed" for record in records),
        "failed": sum(record["status"] == "failed" for record in records),
        "errors": sum(record["status"] == "error" for record in records),
        "skipped": sum(record["status"] == "skipped" for record in records),
    }
    report = {
        "schemaVersion": 1,
        "tests": sorted(records, key=lambda record: record["id"]),
        "summary": summary,
    }
    _write_report(options.report.resolve(), report)
    return (
        0
        if result.wasSuccessful()
        and summary
        == {
            "passed": len(records),
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        }
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
