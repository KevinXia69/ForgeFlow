"""Tests for the curated demo-runs builder.

The builder is loaded by path so the tests exercise the same script the CLI
runs. Synthetic source trees keep every artifact local to ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_demo_runs.py"
PLAN_ID = "a" * 64
TRIAL = "trial-1"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_demo_runs", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()
RUN_IDS = builder.RUN_IDS

ALLOWED = [
    "manifest.json",
    "summary.json",
    "attestation/verifier.json",
    "attestation/verifier.sig",
    f"harbor/job/{TRIAL}/result.json",
    f"harbor/job/{TRIAL}/verifier/reward.json",
    f"harbor/job/{TRIAL}/verifier/reward.txt",
    f"harbor/job/{TRIAL}/agent/recovery/active-plan.json",
    f"harbor/job/{TRIAL}/agent/recovery/{PLAN_ID}/prepared.json",
    f"harbor/job/{TRIAL}/agent/recovery/{PLAN_ID}/success.json",
    f"harbor/job/{TRIAL}/agent/recovery/{PLAN_ID}/manifest.json",
    f"harbor/job/{TRIAL}/agent/recovery/{PLAN_ID}/journal.jsonl",
    f"harbor/job/{TRIAL}/agent/recovery/{PLAN_ID}/snapshot.json",
]

FORBIDDEN = [
    "harbor/job/job.log",
    "harbor/job/result.json",
    "harbor-config.json",
    "harbor-output.log",
    "source-verification.log",
    "implementation/index.json",
    "task/2280_medium_repair_plan_medium/instruction.md",
    "task-derivation/planning-metadata-v1.diff",
    "supplemental-evidence/execution-analysis.json",
    f"harbor/job/{TRIAL}/config.json",
    f"harbor/job/{TRIAL}/trial.log",
    f"harbor/job/{TRIAL}/artifacts/manifest.json",
    f"harbor/job/{TRIAL}/verifier/checks.log",
    f"harbor/job/{TRIAL}/verifier/rule_results.tsv",
    f"harbor/job/{TRIAL}/agent/pi.txt",
    f"harbor/job/{TRIAL}/agent/provider-request-policy.jsonl",
    f"harbor/job/{TRIAL}/agent/entrypoint.log",
    f"harbor/job/{TRIAL}/agent/executor.log",
    f"harbor/job/{TRIAL}/agent/executor.json",
    f"harbor/job/{TRIAL}/agent/recovery-export.json",
]


def _write(path: Path, data: bytes = b"{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _make_run(source: Path, run_id: str, *, recovery: bool = True) -> Path:
    run = source / run_id
    _write(run / "manifest.json", b'{"run_id": "synthetic"}')
    _write(run / "summary.json", b'{"score": 100.0}')
    _write(run / "attestation/verifier.json", b'{"signature": {}}')
    _write(run / "attestation/verifier.sig", b"signature")
    trial = run / "harbor" / "job" / TRIAL
    _write(trial / "result.json", b'{"task_name": "agentic-labs/synthetic"}')
    _write(trial / "verifier/reward.json", b'{"overall_score": 100.0}')
    _write(trial / "verifier/reward.txt", b"100.0")
    for relative in FORBIDDEN:
        _write(run / relative, b"must not be copied")
    if recovery:
        plan = trial / "agent" / "recovery" / PLAN_ID
        _write(trial / "agent/recovery/active-plan.json", b'{"plan_id": "active"}')
        _write(plan / "prepared.json", b'{"step_count": 1}')
        _write(plan / "success.json", b'{"execution_validated": true}')
        _write(plan / "manifest.json", b'{"artifacts": {}}')
        _write(plan / "journal.jsonl", b'{"event": "prepared"}\n')
        _write(plan / "snapshot.json", b'{"data": {}}')
    return run


def _digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _copied_paths(out: Path) -> set[str]:
    return {
        path.relative_to(out).as_posix()
        for path in out.rglob("*")
        if path.is_file()
    }


def test_only_allowlisted_files_are_copied(tmp_path):
    source = tmp_path / "runs"
    out = tmp_path / "demo-runs"
    run = _make_run(source, RUN_IDS[0])

    stats = builder.build_demo_runs(source, out)

    assert stats[0].files == len(ALLOWED)
    expected_bytes = sum((run / relative).stat().st_size for relative in ALLOWED)
    assert stats[0].bytes == expected_bytes
    assert _copied_paths(out) == {f"{RUN_IDS[0]}/{relative}" for relative in ALLOWED} | {
        "README.md"
    }
    for relative in ALLOWED:
        assert (out / RUN_IDS[0] / relative).read_bytes() == (run / relative).read_bytes()
    for relative in FORBIDDEN:
        assert not (out / RUN_IDS[0] / relative).exists(), (
            f"copied forbidden artifact {relative}"
        )
    assert not any(path.is_symlink() for path in out.rglob("*"))


def test_missing_recovery_files_reduce_the_count(tmp_path):
    source = tmp_path / "runs"
    out = tmp_path / "demo-runs"
    _make_run(source, RUN_IDS[2], recovery=False)

    stats = builder.build_demo_runs(source, out)

    assert stats[0].files == 7
    assert stats[0].bytes == sum(
        (source / RUN_IDS[2] / relative).stat().st_size for relative in ALLOWED[:7]
    )


def test_symlinked_files_and_directories_are_skipped(tmp_path):
    source = tmp_path / "runs"
    out = tmp_path / "demo-runs"
    run = _make_run(source, RUN_IDS[0])
    trial = run / "harbor" / "job" / TRIAL
    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    outside_dir = tmp_path / "outside-dir"
    _write(outside_dir / "stolen.json", b"stolen")

    os.symlink(outside, trial / "agent/recovery/linked.json")
    os.symlink(outside, trial / "agent/recovery" / PLAN_ID / "linked.json")
    os.symlink(outside_dir, trial / "agent/recovery/escape-dir", target_is_directory=True)
    os.symlink(outside_dir, run / "harbor/job/symlink-trial", target_is_directory=True)

    stats = builder.build_demo_runs(source, out)

    assert stats[0].files == len(ALLOWED)
    prefix = out / RUN_IDS[0] / "harbor" / "job" / TRIAL
    assert not (prefix / "agent/recovery/linked.json").exists()
    assert not (prefix / "agent/recovery" / PLAN_ID / "linked.json").exists()
    assert "escape-dir" not in _copied_paths(out)
    assert "symlink-trial" not in " ".join(_copied_paths(out))
    assert not any(path.is_symlink() for path in out.rglob("*"))


def test_per_file_cap_skips_oversize_file(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "MAX_FILE_BYTES", 200)
    monkeypatch.setattr(builder, "MAX_TOTAL_BYTES", 10_000)
    source = tmp_path / "runs"
    out = tmp_path / "demo-runs"
    run = _make_run(source, RUN_IDS[0])
    _write(run / f"harbor/job/{TRIAL}/agent/recovery/{PLAN_ID}/large.bin", b"x" * 201)

    stats = builder.build_demo_runs(source, out)

    assert stats[0].files == len(ALLOWED)
    large = out / RUN_IDS[0] / "harbor" / "job" / TRIAL / "agent/recovery" / PLAN_ID
    assert not (large / "large.bin").exists()
    assert stats[0].bytes <= builder.MAX_TOTAL_BYTES


def test_total_cap_is_global_across_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(builder, "MAX_FILE_BYTES", 10_000)
    monkeypatch.setattr(builder, "MAX_TOTAL_BYTES", 100)
    source = tmp_path / "runs"
    out = tmp_path / "demo-runs"
    _make_run(source, RUN_IDS[0])
    _make_run(source, RUN_IDS[1])

    stats = builder.build_demo_runs(source, out)

    assert [item.run_id for item in stats] == list(RUN_IDS[:2])
    assert sum(item.bytes for item in stats) <= 100
    assert len(_copied_paths(out)) < 2 * len(ALLOWED)
    assert "total would exceed the 100-byte cap" in capsys.readouterr().err


def test_caps_match_the_deployment_contract():
    assert builder.MAX_FILE_BYTES == 2 * 1024 * 1024
    assert builder.MAX_TOTAL_BYTES == 20 * 1024 * 1024


def test_rerun_is_byte_deterministic_and_removes_stale_files(tmp_path):
    source = tmp_path / "runs"
    out = tmp_path / "demo-runs"
    _make_run(source, RUN_IDS[0])
    _make_run(source, RUN_IDS[1], recovery=False)

    builder.build_demo_runs(source, out)
    first = _digest(out)
    source_before = _digest(source)

    _write(out / "stale.txt", b"stale")
    builder.build_demo_runs(source, out)

    assert _digest(out) == first
    assert not (out / "stale.txt").exists()
    assert _digest(source) == source_before


def test_missing_runs_are_tolerated_and_absent_from_readme(tmp_path):
    source = tmp_path / "runs"
    out = tmp_path / "demo-runs"
    _make_run(source, RUN_IDS[1])
    _make_run(source, RUN_IDS[3], recovery=False)

    stats = builder.build_demo_runs(source, out)

    assert [item.run_id for item in stats] == [RUN_IDS[1], RUN_IDS[3]]
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert RUN_IDS[1] in readme
    assert RUN_IDS[3] in readme
    assert RUN_IDS[0] not in readme
    assert RUN_IDS[2] not in readme


def test_readme_describes_the_curated_subset(tmp_path):
    source = tmp_path / "runs"
    out = tmp_path / "demo-runs"
    _make_run(source, RUN_IDS[0])

    builder.build_demo_runs(source, out)

    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "curated, read-only subset" in readme
    assert "no\nagent traces" in readme or "no agent traces" in readme
    assert "scripts/build_demo_runs.py" in readme
    for run_id in RUN_IDS[:1]:
        assert f"- `{run_id}`" in readme


def test_cli_prints_per_run_counts_and_bytes(tmp_path, capsys):
    source = tmp_path / "runs"
    out = tmp_path / "demo-runs"
    run = _make_run(source, RUN_IDS[0])
    total = sum((run / relative).stat().st_size for relative in ALLOWED)

    assert builder.main(["--source", str(source), "--out", str(out)]) == 0

    output = capsys.readouterr().out
    assert f"{RUN_IDS[0]}: {len(ALLOWED)} files, {total} bytes" in output
    assert f"total: {len(ALLOWED)} files, {total} bytes" in output


def test_cli_defaults_to_runs_and_demo_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_run(tmp_path / "runs", RUN_IDS[0])

    assert builder.main([]) == 0

    assert (tmp_path / "demo-runs" / RUN_IDS[0] / "manifest.json").is_file()
    assert (tmp_path / "demo-runs" / "README.md").is_file()


def test_no_selected_run_exits_2_and_leaves_output_untouched(tmp_path, capsys):
    (tmp_path / "runs").mkdir()
    out = tmp_path / "demo-runs"

    assert builder.main(["--source", str(tmp_path / "runs"), "--out", str(out)]) == 2

    assert "none of the selected runs exist" in capsys.readouterr().err
    assert not out.exists()


def test_all_selected_runs_are_built_in_selection_order(tmp_path):
    source = tmp_path / "runs"
    out = tmp_path / "demo-runs"
    for run_id in RUN_IDS:
        _make_run(source, run_id)

    stats = builder.build_demo_runs(source, out)

    assert [item.run_id for item in stats] == list(RUN_IDS)
    assert all(item.files == len(ALLOWED) for item in stats)
    assert (out / "README.md").is_file()
