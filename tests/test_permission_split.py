"""Staging tests for the permission-split-v1 instruction derivation."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from forgeflow.config import validate_security_profile
from forgeflow.permission_profile import (
    PROFILE,
    READ_KEY_PATH,
    WRITE_KEY_PATH,
    profile_instruction,
)
from forgeflow.runner import run_task, stage_task

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_HOST = "api.tokenfactory.nebius.com"
WRITE_PASSWORD_OPEN = f"password=open('{WRITE_KEY_PATH}')"
DIFF_RELATIVE = f"task-derivation/{PROFILE}.diff"

# Recorded for tasks A and B in docs/P1_权限拆分与政策合同.md §8.5. The document
# abbreviates the hashes with an ellipsis; these are the full projected values.
EXPECTED = {
    "A": {
        "task": "2280_medium_repair_plan_medium",
        "changed_lines": [58, 62, 70],
        "source_sha256": "77defcf6ef0cac63683abe3025d45f47df160926cd6602103d2c9366eeb3c1bd",
        "execution_sha256": "608aeacdd30a75acd80e3ba6c6276580811eb2e6dc2f18a0851bd72667201cbe",
        "diff_sha256": "8047bf46fca60085fd6bf9253d6148773b3d1e8e8203fd7c4799aa721f4b46f7",
    },
    "B": {
        "task": "2299_hard_repair_plan_hard",
        "changed_lines": [65, 69, 77],
        "source_sha256": "c760020d5e9261783458b7d814a910de3e6ebc67b30633d07942796838ecc8e2",
        "execution_sha256": "8b51d27bd568cbadaaa86328f445e6018c915ceebe512a31af40a2d02e573480",
        "diff_sha256": "5c69c2b0a6a544b0ea78d3ec5cba489f5ae19b3dcef9845412fb0d1edd4bb64a",
    },
}
DECLARED = {
    "environment/Dockerfile",
    "environment/entrypoint.sh",
    "environment/forgeflow_provision.py",
    "task.toml",
    "instruction.md",
}


def _stage(tmp_path: Path, task: str, name: str = "run") -> tuple[Path, dict, Path]:
    run_dir = tmp_path / name
    run_dir.mkdir()
    staged, derivation = stage_task(
        PROJECT_ROOT, task, run_dir, agent="pi",
        allowed_host=ALLOWED_HOST, security_profile=PROFILE,
    )
    return staged, derivation, run_dir


@pytest.mark.parametrize("task", ["A", "B"])
def test_stage_task_projects_the_pinned_instruction(tmp_path: Path, task: str) -> None:
    expected = EXPECTED[task]
    staged, derivation, run_dir = _stage(tmp_path, task)
    instruction = (staged / "instruction.md").read_text()
    assert READ_KEY_PATH in instruction
    assert WRITE_PASSWORD_OPEN not in instruction
    assert hashlib.sha256(instruction.encode("utf-8")).hexdigest() == (
        expected["execution_sha256"]
    )
    record = derivation["permission_profile"]
    assert record["profile"] == PROFILE
    assert record["changed_lines"] == expected["changed_lines"]
    assert record["source_sha256"] == expected["source_sha256"]
    assert record["execution_sha256"] == expected["execution_sha256"]
    assert record["diff_sha256"] == expected["diff_sha256"]
    # The raw diff stays on disk rather than in the manifest-sized record.
    assert "diff" not in record
    diff_path = run_dir / DIFF_RELATIVE
    assert diff_path.is_file()
    diff = diff_path.read_text()
    assert hashlib.sha256(diff.encode("utf-8")).hexdigest() == expected["diff_sha256"]
    patch = next(
        entry for entry in derivation["execution_patches"]
        if entry["path"] == "instruction.md"
    )
    for key in ("changed_lines", "source_sha256", "execution_sha256", "diff_sha256"):
        assert patch[key] == expected[key]
    # The permission profile implies the hardened verifier context: without it
    # a planted module could read the write key during grading.
    assert derivation["security_profile"]["name"] == PROFILE
    assert set(derivation["security_profile"]["verifier_env_added"]) == {
        "PYTHONSAFEPATH", "PYTHONPATH", "UV_NO_CONFIG", "BASH_ENV",
    }


@pytest.mark.parametrize("task", ["A", "B"])
def test_staging_preserves_every_other_vendor_artifact(tmp_path: Path, task: str) -> None:
    expected = EXPECTED[task]
    vendor = PROJECT_ROOT / "vendor/erp-bench/tasks" / expected["task"]
    staged, derivation, _ = _stage(tmp_path, task)
    assert set(derivation["expected_modified_files"]) == DECLARED
    vendor_files = {
        path.relative_to(vendor).as_posix(): path.read_bytes()
        for path in vendor.rglob("*") if path.is_file()
    }
    staged_files = {
        path.relative_to(staged).as_posix(): path.read_bytes()
        for path in staged.rglob("*") if path.is_file()
    }
    assert staged_files.keys() == vendor_files.keys() | {"environment/forgeflow_provision.py"}
    for relative, content in vendor_files.items():
        if relative in DECLARED:
            continue
        assert staged_files[relative] == content
        assert (
            derivation["original_file_sha256"][relative]
            == derivation["execution_file_sha256"][relative]
        )
    # The two non-instruction edits are exactly the declared compatibility and
    # network-policy patches, so instruction.md is the only content change.
    dockerfile = vendor_files["environment/Dockerfile"].decode().replace(
        "RUN uv pip install --system --no-cache",
        "RUN uv pip install --system --break-system-packages --no-cache",
        1,
    )
    dockerfile = dockerfile.replace(
        "COPY setup_scenario.py /setup/setup_scenario.py\n",
        "COPY setup_scenario.py /setup/setup_scenario.py\n"
        "COPY forgeflow_provision.py /setup/forgeflow_provision.py\n",
        1,
    )
    assert staged_files["environment/Dockerfile"].decode() == dockerfile
    entrypoint = staged_files["environment/entrypoint.sh"].decode()
    assert "/etc/odoo/api_key.read" in entrypoint
    assert entrypoint.index("forgeflow_provision.py") < entrypoint.index(
        "touch /tmp/saas_setup_complete"
    )
    provision = staged_files["environment/forgeflow_provision.py"].decode()
    compile(provision, "<provision>", "exec")
    assert "SELFCHECK:" in provision
    task_config = tomllib.loads(staged_files["task.toml"].decode())
    assert task_config["agent"]["network_mode"] == "allowlist"
    assert task_config["agent"]["allowed_hosts"] == [ALLOWED_HOST]
    assert task_config["verifier"]["env"]["PYTHONSAFEPATH"] == "1"
    assert task_config["verifier"]["env"]["UV_NO_CONFIG"] == "1"
    assert derivation["original_file_sha256"]["instruction.md"] == expected["source_sha256"]
    assert derivation["execution_file_sha256"]["instruction.md"] == expected["execution_sha256"]


def test_staging_the_same_source_twice_keeps_the_instruction_hash(tmp_path: Path) -> None:
    first, first_record, first_run = _stage(tmp_path, "A", "run-first")
    second, second_record, second_run = _stage(tmp_path, "A", "run-second")
    first_hash = hashlib.sha256((first / "instruction.md").read_bytes()).hexdigest()
    second_hash = hashlib.sha256((second / "instruction.md").read_bytes()).hexdigest()
    assert first_hash == second_hash == EXPECTED["A"]["execution_sha256"]
    assert first_record["permission_profile"] == second_record["permission_profile"]
    assert (first_run / DIFF_RELATIVE).read_bytes() == (second_run / DIFF_RELATIVE).read_bytes()


def test_an_already_projected_source_is_a_recorded_noop(tmp_path: Path) -> None:
    vendor_relative = Path("vendor/erp-bench/tasks") / EXPECTED["A"]["task"]
    source_root = tmp_path / "source-root"
    shutil.copytree(PROJECT_ROOT / vendor_relative, source_root / vendor_relative)
    instruction_path = source_root / vendor_relative / "instruction.md"
    projected, _ = profile_instruction(instruction_path.read_text())
    instruction_path.write_text(projected)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    staged, derivation = stage_task(
        source_root, "A", run_dir, agent="pi",
        allowed_host=ALLOWED_HOST, security_profile=PROFILE,
    )
    assert derivation["permission_profile"] == {
        "profile": PROFILE,
        "already_projected": True,
    }
    assert "instruction.md" not in derivation["expected_modified_files"]
    assert (staged / "instruction.md").read_text() == projected
    assert not (run_dir / DIFF_RELATIVE).exists()


@pytest.mark.parametrize(("agent", "planner_profile"), [("pi", "none"), ("pi", "cp-sat-live-v1")])
def test_run_task_rejects_the_wrong_agent_or_planner(
    tmp_path: Path, agent: str, planner_profile: str,
) -> None:
    with pytest.raises(ValueError, match="requires cp-sat-recovery-v1"):
        run_task(
            tmp_path, "A", agent,
            planner_profile=planner_profile,
            security_profile=PROFILE,
            run_id="permission-split",
        )
    assert not (tmp_path / "runs").exists()


def test_validate_security_profile_accepts_the_new_profile() -> None:
    assert validate_security_profile(PROFILE) == PROFILE


def test_derived_entrypoint_is_valid_bash(tmp_path: Path) -> None:
    staged, _, _ = _stage(tmp_path, "A")
    script = staged / "environment/entrypoint.sh"
    checked = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
