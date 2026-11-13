from __future__ import annotations

from pathlib import Path

import pytest

from scripts.critical_regression_gate import evaluate_junit


@pytest.mark.parametrize(("body", "returncode", "error"), [
    ("<testsuites/>", 0, "no_tests_executed"),
    ('<testsuite><testcase><skipped message="Odoo unavailable"/></testcase></testsuite>',
     0, "critical_tests_unverified"),
    ('<testsuite><testcase><failure/></testcase></testsuite>', 0, "critical_tests_failed"),
    ('<testsuite><testcase><error/></testcase></testsuite>', 0, "critical_tests_failed"),
    ("<testsuite><testcase/></testsuite>", 1, "test_command_failed"),
    ("not XML", 0, "test_report_unreadable"),
])
def test_incomplete_or_failed_evidence_blocks_release(tmp_path: Path, body, returncode, error):
    report = tmp_path / "result.xml"
    report.write_text(body)
    result = evaluate_junit(report, returncode)
    assert result["ok"] is False
    assert error in result["errors"]


def test_successful_tests_and_zero_exit_status_are_both_required(tmp_path: Path):
    report = tmp_path / "result.xml"
    report.write_text('<testsuites><testsuite><testcase/><testcase/></testsuite></testsuites>')
    result = evaluate_junit(report, 0)
    assert result == {"ok": True, "counts": {
        "tests": 2, "failures": 0, "errors": 0, "skipped": 0}, "errors": []}


def test_missing_report_is_not_a_pass(tmp_path: Path):
    assert evaluate_junit(tmp_path / "missing.xml", 0)["errors"] == ["test_report_unreadable"]
