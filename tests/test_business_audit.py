"""Database no-write evidence is independent, complete and trusted on export."""
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.agents.pi_nebius import (
    EXECUTOR_STORE,
    _build_business_audit_command,
    _build_log_export_command,
)
from forgeflow.budget import BUSINESS_AUDIT_TABLES
from forgeflow.runner import _business_table_audit_result


@pytest.mark.parametrize("failure", [False, True])
def test_snapshot_uses_one_read_only_transaction_and_persists_no_raw_rows(tmp_path, monkeypatch, failure):
    command = _build_business_audit_command("before")
    program = command.split("\n", 1)[1].rsplit("\nFORGEFLOW_BUSINESS_AUDIT", 1)[0]
    program = program.replace(repr(EXECUTOR_STORE), repr(str(tmp_path)))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    original = Path.lstat

    def root_directory(path, *args, **kwargs):
        if path == tmp_path / "business-audit":
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", root_directory)
    calls = []

    def query(argv, **kwargs):
        calls.append((argv, kwargs))
        if failure:
            raise RuntimeError("private database error")
        return SimpleNamespace(stdout="\n".join(f"{name}\t3\t{'a' * 32}" for name in BUSINESS_AUDIT_TABLES))

    monkeypatch.setattr(subprocess, "run", query)
    exec(compile(program, "business-audit", "exec"), {})
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:6] == ["runuser", "-u", "postgres", "--", "psql", "-d"]
    assert argv[-1].startswith("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;")
    assert argv[-1].count("SELECT '") == 16
    assert kwargs["timeout"] == 8
    target = tmp_path / "business-audit/before.json"
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    result = json.loads(target.read_text())
    assert result["status"] == ("failed" if failure else "captured")
    assert "private database error" not in target.read_text()
    assert result["tables"] is None if failure else set(result["tables"]) == set(BUSINESS_AUDIT_TABLES)


def audit_trial(tmp_path, *, changed=False, captured=True):
    agent = tmp_path / "agent"
    target = agent / "recovery/business-audit"
    target.mkdir(parents=True)
    rows = []
    for phase in ("before", "after"):
        data = {"schema_version": 1, "phase": phase, "status": "captured" if captured else "failed",
                "read_only_transaction": True,
                "tables": {name: {"count": 3, "digest": "a" * 32} for name in BUSINESS_AUDIT_TABLES}}
        if changed and phase == "after":
            data["tables"]["purchase_order"]["digest"] = "b" * 32
        path = target / f"{phase}.json"
        path.write_text(json.dumps(data))
        raw = path.read_bytes()
        rows.append({"path": f"business-audit/{phase}.json", "bytes": len(raw),
                     "sha256": hashlib.sha256(raw).hexdigest(), "root_owned_read_only": True})
    (agent / "recovery-export.json").write_text(json.dumps({"copied_files": rows}))
    return tmp_path


@pytest.mark.parametrize(("changed", "captured", "expected"), [
    (False, True, "unchanged"), (True, True, "changed"), (False, False, "failed"),
])
def test_audit_summary_distinguishes_unchanged_changed_and_failed(tmp_path, changed, captured, expected):
    result = _business_table_audit_result(audit_trial(tmp_path, changed=changed, captured=captured))
    assert result["status"] == expected
    assert result["business_tables_unchanged"] is ((not changed) if captured else None)
    assert result["changed_tables"] == (["purchase_order"] if changed else [])


@pytest.mark.parametrize("corrupt", ["missing", "content", "trust", "symlink"])
def test_unknown_evidence_never_becomes_a_zero_write_claim(tmp_path, corrupt):
    trial = audit_trial(tmp_path)
    path = trial / "agent/recovery/business-audit/after.json"
    if corrupt == "missing":
        path.unlink()
    elif corrupt == "content":
        path.write_text('{}')
    elif corrupt == "trust":
        report = trial / "agent/recovery-export.json"
        data = json.loads(report.read_text())
        data["copied_files"][1].pop("root_owned_read_only")
        report.write_text(json.dumps(data))
    else:
        path.unlink()
        path.symlink_to(path.with_name("before.json"))
    result = _business_table_audit_result(trial)
    assert result["status"] == "unknown" and result["business_tables_unchanged"] is None


def test_export_rejects_agent_writable_business_audit(tmp_path):
    source = tmp_path / "source"
    audit = source / "recovery/business-audit"
    audit.mkdir(parents=True, mode=0o700)
    (audit / "before.json").write_text('{"forged":true}')
    (audit / "before.json").chmod(0o666)
    destination = tmp_path / "logs"
    destination.mkdir()
    result = subprocess.run(["bash", "-c", _build_log_export_command(str(source), str(destination))],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    report = json.loads((destination / "recovery-export.json").read_text())
    assert report["complete"] is False
    assert not (destination / "recovery/business-audit/before.json").exists()
    assert any(row["reason"] == "untrusted_business_audit" for row in report["skipped"])
