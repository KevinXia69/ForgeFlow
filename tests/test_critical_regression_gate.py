from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.critical_regression_gate import evaluate_junit, pytest_command


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


def test_interleaved_suite_keeps_directory_fixtures_and_every_test(tmp_path: Path):
    """Real pytest subprocess: returning to a directory lost fixtures on 9.1.1.

    Parent and child fixtures deliberately use the same name, so registering the
    child globally or dropping a selected test cannot make this contract pass.
    """
    files = {
        "pytest.ini": "[pytest]\n",
        "conftest.py": "import pytest\n@pytest.fixture\ndef scope(): return 'root'\n",
        "api/conftest.py": "import pytest\n@pytest.fixture\ndef scope(): return 'api'\n"
                           "@pytest.fixture\ndef client(): return 'scoped client'\n",
        "api/test_first.py": "def test_first(scope, client):\n"
                             "    assert (scope, client) == ('api', 'scoped client')\n",
        "test_middle.py": "def test_middle(scope): assert scope == 'root'\n",
        "integration/test_probe.py": "def test_probe(scope): assert scope == 'root'\n",
        "api/test_last.py": "def test_last(scope, client):\n"
                            "    assert (scope, client) == ('api', 'scoped client')\n",
    }
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    selected = ("api/test_first.py", "test_middle.py", "integration/test_probe.py",
                "api/test_last.py")
    report = tmp_path / "pytest.xml"
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(name, None)
    result = subprocess.run(pytest_command(report, selected), cwd=tmp_path,
                            env=env, text=True, capture_output=True, check=False)
    assert evaluate_junit(report, result.returncode) == {
        "ok": True, "counts": {"tests": 4, "failures": 0, "errors": 0, "skipped": 0},
        "errors": [],
    }, result.stdout + result.stderr
