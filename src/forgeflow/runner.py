"""Reproducible local Harbor runs with separate raw and strict task outcomes."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tomllib
import uuid
from datetime import datetime, timezone
from difflib import unified_diff
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

from forgeflow.agents.pi_usage import accounting_view
from forgeflow.budget import BUDGET_MECHANISM_VERSION, CLEANUP_ALLOWANCE_SEC, SETUP_BUDGET_SEC
from forgeflow.config import (
    VERIFIER_HARDENING,
    load_config,
    resolve_execution_guidance,
    validate_execution_guidance,
    validate_security_profile,
    validate_thinking_mode,
)
from forgeflow.permission_entrypoint import (
    DOCKERFILE_FILE,
    ENTRYPOINT_FILE,
    PROVISION_FILE,
    patch_dockerfile,
    patch_entrypoint,
)
from forgeflow.permission_profile import PROFILE, READ_KEY_PATH, profile_instruction
from forgeflow.permissions import provision_script
from forgeflow.runtime import docker_environment

TASKS = {
    "A": "2280_medium_repair_plan_medium",
    "B": "2299_hard_repair_plan_hard",
}
AGENTS = ("oracle", "nop", "pi")
DATA_PROFILES = ("original", "planning-metadata-v1")
PLANNER_PROFILES = ("none", "cp-sat-live-v1", "cp-sat-native-v1", "cp-sat-native-v2",
                    "cp-sat-recovery-v1")
PLANNING_METADATA_MARKER = "[ForgeFlow planning-metadata-v1]"
PI_IMPORT_PATH = "forgeflow.agents.pi_nebius:PiNebius"
VERIFIER_IMPORT_PATH = "forgeflow.verifier:ERPBenchVerifier"
IMPLEMENTATION_INPUTS = (
    "src/forgeflow", "scripts", "pyproject.toml", "uv.lock", "upstream.lock.json",
    "build-inputs/pi-0.85.1-model-data", "build-inputs/planner",
)
SNAPSHOT_EXCLUDED_NAMES = {
    "__pycache__", ".cache", ".pytest_cache", ".ruff_cache", ".venv",
    ".runtime", ".git", "node_modules", "vendor", "runs", ".DS_Store",
}


HELD_OUT_TASK = re.compile(r"[0-9]{4}_[a-z0-9_]{1,80}")


def resolve_task(task: str) -> str:
    if task in TASKS:
        return TASKS[task]
    if task in TASKS.values():
        return task
    # A held-out ERP-Bench task is addressed by its exact installed directory
    # name; the pattern excludes separators and the directory must exist.
    if (
        isinstance(task, str) and HELD_OUT_TASK.fullmatch(task)
        and (Path(__file__).resolve().parents[2] / "vendor/erp-bench/tasks" / task).is_dir()
    ):
        return task
    raise ValueError("Task must be A, B, a selected ERP-Bench task ID, or an installed held-out task ID.")


def build_run_command(
    root: str | Path,
    task: str,
    agent: str,
    run_dir: str | Path,
    *,
    model: str | None = None,
    agent_timeout_sec: int = 3600,
    task_path: str | Path | None = None,
    max_output_tokens: int | None = None,
    thinking_mode: str = "provider-default",
    execution_guidance: str = "none",
    planner_profile: str = "none",
    security_profile: str = "none",
    execution_root: str | Path | None = None,
    host_runtime: dict | None = None,
) -> list[str]:
    root = Path(root).resolve()
    task_name = resolve_task(task)
    if agent not in AGENTS:
        raise ValueError("Agent must be oracle, nop, or pi.")
    if isinstance(agent_timeout_sec, bool) or not isinstance(agent_timeout_sec, int) or agent_timeout_sec <= 0:
        raise ValueError("Agent timeout must be a positive integer in seconds.")
    _validate_output_budget_argument(agent, max_output_tokens)
    _validate_thinking_mode_argument(agent, thinking_mode)
    validate_execution_guidance(
        execution_guidance, agent=agent, planner_profile=planner_profile,
    )
    _planner_record(agent, planner_profile)
    outer_agent_sec = agent_timeout_sec + (CLEANUP_ALLOWANCE_SEC if agent == "pi" else 0)
    if host_runtime is not None:
        from forgeflow.host_runtime import harbor_command

        if execution_root is None:
            raise ValueError("Frozen host execution requires a frozen source root.")
        entrypoint = harbor_command(host_runtime, Path(execution_root))
    else:
        entrypoint = [str(root / ".venv/bin/harbor")]
    command = [
        *entrypoint,
        "run",
        "-p", str(Path(task_path).resolve() if task_path else root / "vendor/erp-bench/tasks" / task_name),
        "-a", PI_IMPORT_PATH if agent == "pi" else agent,
        "--env", "forgeflow.environment:GuardedDockerEnvironment" if agent == "pi" else "docker",
        "--jobs-dir", str(Path(run_dir).resolve() / "harbor"),
        "--job-name", "job",
        "-n", "1",
        "-k", "1",
        "--max-retries", "0",
        "--timeout-multiplier", "1",
        "--agent-timeout-multiplier", str(outer_agent_sec / 3600),
        "--verifier", VERIFIER_IMPORT_PATH,
        "--delete",
        "--quiet",
    ]
    if agent == "pi":
        if not model or any(character.isspace() for character in model):
            raise ValueError("Pi requires an explicit Nebius model identifier.")
        source_root = Path(execution_root).resolve() if execution_root else root
        command.extend(["-m", f"nebius/{model}", "--ak", f"workspace_root={source_root}"])
        command.extend(["--ak", f"agent_budget_sec={agent_timeout_sec}"])
        if max_output_tokens is not None:
            command.extend(["--ak", f"max_output_tokens={max_output_tokens}"])
        if thinking_mode != "provider-default":
            command.extend(["--ak", f"thinking_mode={thinking_mode}"])
        if execution_guidance != "none":
            command.extend(["--ak", f"execution_guidance={execution_guidance}"])
        if planner_profile != "none":
            command.extend(["--ak", f"planner_profile={planner_profile}"])
        if security_profile != "none":
            command.extend(["--ak", f"security_profile={security_profile}"])
    return command


def _validate_output_budget_argument(agent: str, value: int | None) -> None:
    if value is None:
        return
    if agent != "pi":
        raise ValueError("max_output_tokens is supported only for the Pi agent.")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("max_output_tokens must be a positive integer.")


def _validate_thinking_mode_argument(agent: str, mode: str) -> None:
    validate_thinking_mode(mode)
    if agent != "pi" and mode != "provider-default":
        raise ValueError("thinking_mode is supported only for the Pi agent.")


def _data_profile_record(profile: str) -> dict:
    if profile not in DATA_PROFILES:
        raise ValueError("Unknown data profile; expected original or planning-metadata-v1.")
    return {
        "name": profile,
        "visibility_only": profile != "original",
        "is_original_benchmark_data": profile == "original",
    }


def _planner_record(agent: str, profile: str) -> dict:
    if profile not in PLANNER_PROFILES:
        raise ValueError("Unknown planner profile; expected " + ", ".join(PLANNER_PROFILES) + ".")
    if agent != "pi" and profile != "none":
        raise ValueError("Planner tools are supported only for the Pi agent.")
    record = {
        "profile": profile,
        "enabled": profile != "none",
        "read_only": True,
        "experimental_factor": "solver_tool_availability",
    }
    if profile in {"cp-sat-native-v1", "cp-sat-native-v2"}:
        record.update(interface="pi_native_tool", experimental_factor="solver_tool_interface")
    if profile == "cp-sat-native-v2":
        record.update(
            error_feedback="structured-v1",
            experimental_factor="planner_error_feedback",
        )
    if profile == "cp-sat-recovery-v1":
        record.update(read_only=False, interface="pi_native_tool",
                      experimental_factor="validated_change_set_execution",
                      execution="planned_documents_with_journal_and_readback")
    return record


def _planning_metadata_projection(source_text: str) -> tuple[str, dict, str]:
    """Expose existing planning facts in notes; never derive replacement values."""
    payload = json.loads(source_text)
    rows = payload.get("workcenters") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("Planning metadata requires a nonempty workcenters array.")
    groups: dict[str, list[dict]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("Planning metadata requires workcenter objects.")
        if not isinstance(row.get("code"), str) or not row["code"].strip():
            raise ValueError("Planning metadata requires a workcenter code.")
        if not isinstance(row.get("product_code"), str) or not row["product_code"].strip():
            raise ValueError("Planning metadata requires a product code.")
        note = row.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError("Planning metadata requires a string or null workcenter note.")
        if isinstance(note, str) and PLANNING_METADATA_MARKER in note:
            raise ValueError("Planning metadata must be derived from an unmodified source note.")
        for field in ("time_per_unit_minutes", "unit_cost", "lead_days"):
            value = row.get(field)
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0
                or (field == "time_per_unit_minutes" and value == 0)
            ):
                raise ValueError(f"Planning metadata requires a valid existing {field}.")
        values = {
            field: row[field]
            for field in ("product_code", "time_per_unit_minutes", "unit_cost", "lead_days")
        }
        groups.setdefault(row["code"], []).append({
            "row_pointer": f"/workcenters/{index}",
            "field_pointers": {
                field: f"/workcenters/{index}/{field}" for field in ("code", *values)
            },
            "values": values,
        })
    changed_fields = []
    for index, row in enumerate(rows):
        entries = groups[row["code"]]
        block = "\n".join([
            PLANNING_METADATA_MARKER,
            "Benchmark horizon planning values for this workcenter, by product.",
            "time_per_unit_minutes = standard processing minutes per unit; "
            "unit_cost = standard manufacturing cost per unit in dollars; lead_days = lead time in days.",
            "Use these values with the benchmark horizon-wide capacity rule. "
            "Do not substitute calendar-recomputed workcenter_load.",
            *[json.dumps(entry["values"], ensure_ascii=False, allow_nan=False) for entry in entries],
        ])
        original_note = row.get("note")
        row["note"] = (original_note + "\n\n" if original_note else "") + block
        changed_fields.append(f"/workcenters/{index}/note")
    # Prove that note fields are the only semantic edits, including original values and types.
    restored = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    original = json.loads(source_text)
    for index, row in enumerate(original["workcenters"]):
        if "note" in row:
            restored["workcenters"][index]["note"] = row["note"]
        else:
            restored["workcenters"][index].pop("note", None)
    if restored != original:
        raise RuntimeError("Planning metadata changed fields outside workcenter notes.")
    derived_text = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    diff = "".join(unified_diff(
        source_text.splitlines(keepends=True), derived_text.splitlines(keepends=True),
        fromfile="original/environment/scenario_data.json",
        tofile="planning-metadata-v1/environment/scenario_data.json",
    ))
    record = {
        **_data_profile_record("planning-metadata-v1"),
        "source_file": "environment/scenario_data.json",
        "source_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
        "execution_sha256": hashlib.sha256(derived_text.encode()).hexdigest(),
        "changed_fields": changed_fields,
        "preserved_non_note_fields_verified": True,
        "planning_basis": "benchmark horizon standard minutes/cost/lead time; not calendar workcenter_load",
        "field_sources": [
            {"workcenter_code": code, "rows": entries} for code, entries in groups.items()
        ],
        "diff_path": "task-derivation/planning-metadata-v1.diff",
        "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
    }
    return derived_text, record, diff


def _task_file_hashes(directory: Path) -> dict[str, str]:
    hashes = {}
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Task input must be a regular directory.")
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Task input must not contain symbolic links.")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Task input must contain only regular files.")
        hashes[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _write_derivation_diff(run_dir: Path, relative: str, text: str) -> None:
    """Write derivation evidence without following a symlinked evidence directory."""
    path = run_dir / relative
    if path.parent.is_symlink():
        raise ValueError("The task derivation evidence directory must not be a symlink.")
    path.parent.mkdir(exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


def _harden_verifier_env(text: str) -> tuple[str, dict[str, str]]:
    """Add the verifier hardening variables to [verifier.env], preserving all else.

    The verifier runs as root with the task's working directory on sys.path, so
    a module planted by the agent can be imported and can write the reward. The
    variables are added only when absent; a conflicting task value is an error
    rather than a silent override.
    """
    original = tomllib.loads(text)
    verifier = original.get("verifier")
    if verifier is not None and not isinstance(verifier, dict):
        raise ValueError("[verifier] must be a table, not a scalar or array.")
    env = verifier.get("env") if isinstance(verifier, dict) else None
    if env is not None and not isinstance(env, dict):
        raise ValueError("[verifier].env must be a table.")
    env = env if isinstance(env, dict) else {}
    added = {}
    for key, value in sorted(VERIFIER_HARDENING.items()):
        if key in env:
            if env[key] != value:
                raise ValueError(
                    f"The task already sets a different {key} in [verifier.env]; review it first."
                )
            continue
        added[key] = value
    if not added:
        return text, {}
    lines = "".join(f'{key} = "{value}"\n' for key, value in added.items())
    header = re.compile(r"(?m)^\[verifier\.env\][ \t]*(?:#.*)?$")
    matches = list(header.finditer(text))
    if len(matches) > 1:
        raise ValueError("Review the changed task config before hardening the verifier.")
    if not matches and isinstance(verifier, dict) and "env" in verifier:
        raise ValueError(
            "The task defines [verifier].env as an inline table or dotted keys; "
            "hardening needs an explicit [verifier.env] table. Edit the task first."
        )
    if matches:
        position = matches[0].end()
        derived = text[:position] + "\n" + lines + text[position:]
    else:
        derived = text.rstrip("\n") + "\n\n[verifier.env]\n" + lines
    expected = json.loads(json.dumps(original))
    expected_verifier = expected.setdefault("verifier", {})
    expected_env = expected_verifier.setdefault("env", {})
    expected_env.update(added)
    if tomllib.loads(derived) != expected:
        raise RuntimeError("Derived task changed fields outside [verifier.env].")
    return derived, added


def stage_task(
    root: str | Path,
    task: str,
    run_dir: str | Path,
    *,
    agent: str,
    allowed_host: str | None = None,
    data_profile: str = "original",
    security_profile: str = "none",
) -> tuple[Path, dict]:
    """Apply declared compatibility fixes and optional planning-data visibility."""
    validate_security_profile(security_profile)
    profile_record = _data_profile_record(data_profile)
    if agent not in AGENTS:
        raise ValueError("Agent must be oracle, nop, or pi.")
    if agent == "pi":
        if not allowed_host or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", allowed_host):
            raise ValueError("The model allowlist must contain one exact hostname.")
        allowed_host = allowed_host.lower()
    elif allowed_host is not None:
        raise ValueError("Only Pi may have a model allowlist.")
    root = Path(root).resolve()
    run_dir = Path(run_dir).resolve()
    task_name = resolve_task(task)
    source = root / "vendor/erp-bench/tasks" / task_name
    destination = run_dir / "task" / task_name
    original_hashes = _task_file_hashes(source)
    dockerfile_path = "environment/Dockerfile"
    dockerfile = (source / dockerfile_path).read_text()
    original_line = "RUN uv pip install --system --no-cache \\\n"
    execution_line = "RUN uv pip install --system --break-system-packages --no-cache \\\n"
    if dockerfile.count(original_line) != 1:
        raise ValueError("Review the changed upstream uv install command before deriving a task.")
    derived_files = {dockerfile_path: dockerfile.replace(original_line, execution_line, 1)}
    overrides = {}
    patches = [{
        "path": dockerfile_path,
        "reason": "The Odoo image marks system Python externally managed (PEP 668); uv otherwise rejects the pinned build-time dependency install.",
        "before": original_line,
        "after": execution_line,
    }]
    if agent == "pi":
        config_text = (source / "task.toml").read_text()
        original_config = tomllib.loads(config_text)
        original_agent = original_config.get("agent")
        if not isinstance(original_agent, dict):
            raise ValueError("The original task must contain an [agent] section.")
        if "network_mode" in original_agent or "allowed_hosts" in original_agent:
            raise ValueError("Review the changed upstream Agent network policy before deriving a task.")
        matches = list(re.finditer(r"(?m)^\[agent\][ \t]*$", config_text))
        if len(matches) != 1:
            raise ValueError("The original task must contain one plain [agent] header.")
        override_lines = '\nnetwork_mode = "allowlist"\nallowed_hosts = ' + json.dumps([allowed_host])
        match = matches[0]
        derived_text = config_text[:match.end()] + override_lines + config_text[match.end():]
        expected_config = tomllib.loads(config_text)
        expected_config["agent"].update(network_mode="allowlist", allowed_hosts=[allowed_host])
        if tomllib.loads(derived_text) != expected_config:
            raise RuntimeError("Derived task changed fields outside the Agent network policy.")
        derived_files["task.toml"] = derived_text
        overrides = {"agent.network_mode": "allowlist", "agent.allowed_hosts": [allowed_host]}
        patches.append({
            "path": "task.toml",
            "reason": "Restrict Agent-phase network access to the model endpoint to exclude public reference answers.",
            "before": match.group(),
            "after": match.group() + override_lines,
        })
    verifier_env_added = {}
    if security_profile in ("verifier-hardened-v1", "permission-split-v1"):
        config_text = derived_files.get("task.toml", (source / "task.toml").read_text())
        derived_text, verifier_env_added = _harden_verifier_env(config_text)
        if verifier_env_added:
            derived_files["task.toml"] = derived_text
            patches.append({
                "path": "task.toml",
                "reason": (
                    "Harden the root verifier context: keep the task working directory off "
                    "sys.path and stop uv from reading a task-planted config."
                ),
                "before": [f"{key} absent" for key in verifier_env_added],
                "after": [f'{key} = "{value}"' for key, value in verifier_env_added.items()],
            })
    permission_profile_record = None
    permission_profile_diff = None
    if security_profile == PROFILE:
        instruction_path = "instruction.md"
        instruction_text = derived_files.get(
            instruction_path, (source / instruction_path).read_text()
        )
        try:
            projected, permission_record = profile_instruction(instruction_text)
        except ValueError as error:
            if str(error) != "already_projected":
                raise
            # A source that already cites the read-only credential needs no
            # second rewrite; record the no-op instead of failing the run.
            permission_profile_record = {
                "profile": PROFILE,
                "already_projected": True,
            }
        else:
            derived_files[instruction_path] = projected
            permission_profile_diff = str(permission_record["diff"])
            permission_profile_record = {
                key: value for key, value in permission_record.items() if key != "diff"
            }
            permission_profile_record["already_projected"] = False
            patches.append({
                "path": instruction_path,
                "reason": (
                    "Permission-split-v1: cite the read-only Odoo credential at "
                    f"{READ_KEY_PATH} and require writes through execute_recovery "
                    "after prepare_recovery."
                ),
                "changed_lines": permission_record["changed_lines"],
                "source_sha256": permission_record["source_sha256"],
                "execution_sha256": permission_record["execution_sha256"],
                "diff_sha256": permission_record["diff_sha256"],
            })
        entrypoint_text = (source / ENTRYPOINT_FILE).read_text()
        derived_files[ENTRYPOINT_FILE] = patch_entrypoint(entrypoint_text)
        patches.append({
            "path": ENTRYPOINT_FILE,
            "reason": (
                "Permission-split-v1: after scenario setup, provision the read-only "
                "Odoo user, restart Odoo so group caches rebuild, write the read key, "
                "and make the admin key and odoo.conf root-only."
            ),
        })
        derived_files[PROVISION_FILE] = provision_script()
        patches.append({
            "path": PROVISION_FILE,
            "reason": "Generated read-only credential provisioning and self-check.",
        })
        dockerfile_text = derived_files.get(DOCKERFILE_FILE, (source / DOCKERFILE_FILE).read_text())
        derived_files[DOCKERFILE_FILE] = patch_dockerfile(dockerfile_text)
        patches.append({
            "path": DOCKERFILE_FILE,
            "reason": "Copy the generated provisioning script into /setup.",
        })
    profile_diff = None
    if data_profile == "planning-metadata-v1":
        scenario_path = "environment/scenario_data.json"
        derived_text, profile_record, profile_diff = _planning_metadata_projection(
            (source / scenario_path).read_bytes().decode("utf-8")
        )
        derived_files[scenario_path] = derived_text
        patches.append({
            "path": scenario_path,
            "reason": "Visibility-only planning metadata profile: append existing workcenter planning fields to public notes; preserve numeric business inputs and original verifier.",
            "diff_path": profile_record["diff_path"],
            "diff_sha256": profile_record["diff_sha256"],
        })
    if destination.parent.is_symlink():
        raise ValueError("The derived task directory must not be a symlink.")
    destination.parent.mkdir(exist_ok=True)
    shutil.copytree(source, destination)
    for relative, content in derived_files.items():
        (destination / relative).write_text(content)
    execution_hashes = _task_file_hashes(destination)
    differences = {
        name for name in original_hashes.keys() | execution_hashes.keys()
        if original_hashes.get(name) != execution_hashes.get(name)
    }
    if differences != set(derived_files) or _task_file_hashes(source) != original_hashes:
        raise RuntimeError("Task derivation did not preserve the original business artifacts.")
    if profile_diff is not None:
        _write_derivation_diff(run_dir, str(profile_record["diff_path"]), profile_diff)
    if permission_profile_diff is not None:
        _write_derivation_diff(
            run_dir,
            f"task-derivation/{PROFILE}.diff",
            permission_profile_diff,
        )
    return destination, {
        "path": str(destination.relative_to(run_dir)),
        "original_task_path": str(source.relative_to(root)),
        "original_files_modified": False,
        "derived_config_modified": "task.toml" in derived_files,
        "unchanged_artifacts_verified": True,
        "execution_config_overrides": overrides,
        "expected_modified_files": sorted(derived_files),
        "execution_patches": patches,
        "data_profile": profile_record,
        "permission_profile": permission_profile_record,
        "security_profile": {
            "name": security_profile,
            "verifier_env_added": verifier_env_added,
        },
        "original_file_sha256": original_hashes,
        "execution_file_sha256": execution_hashes,
        "derived_task_integrity_after": None,
    }


def stage_pi_task(
    root: str | Path, task: str, run_dir: str | Path, *, allowed_host: str,
    data_profile: str = "original", security_profile: str = "none",
) -> tuple[Path, dict]:
    """Compatibility entry point for the Pi-specific execution task."""
    return stage_task(
        root, task, run_dir, agent="pi", allowed_host=allowed_host,
        data_profile=data_profile, security_profile=security_profile,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
            text = text.replace(json.dumps(secret)[1:-1], "[REDACTED]")
    return text


def _write_json(path: Path, value: dict, secrets: tuple[str, ...] = ()) -> None:
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(_redact(text, secrets))
    temporary.replace(path)


def _snapshot_excluded(path: Path) -> bool:
    return (
        path.name in SNAPSHOT_EXCLUDED_NAMES
        or path.name.startswith(".env")
        or path.suffix in {".pyc", ".pyo"}
    )


def _implementation_files(root: Path) -> list[Path]:
    """Select source/build inputs without traversing caches or symbolic links."""
    files = []
    for relative in IMPLEMENTATION_INPUTS:
        top = root / relative
        parts = Path(relative).parts
        if any(root.joinpath(*parts[:length]).is_symlink() for length in range(1, len(parts) + 1)):
            raise ValueError("Implementation snapshot must not contain symbolic links.")
        if not top.exists():
            raise ValueError("Implementation snapshot requires regular source/build inputs.")
        if top.is_file():
            files.append(top)
            continue
        if not top.is_dir():
            raise ValueError("Implementation snapshot input is not a regular file or directory.")
        for directory, children, names in os.walk(top, followlinks=False):
            parent = Path(directory)
            children[:] = sorted(name for name in children if not _snapshot_excluded(Path(name)))
            for name in children:
                if (parent / name).is_symlink():
                    raise ValueError("Implementation snapshot must not contain symbolic links.")
            for name in sorted(names):
                path = parent / name
                if _snapshot_excluded(path):
                    continue
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Implementation snapshot must contain regular files only.")
                files.append(path)
    return sorted(files)


def _implementation_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _implementation_files(root)
    }


def capture_implementation_snapshot(
    root: Path, run_dir: Path, *, secrets: tuple[str, ...] = (),
) -> dict:
    """Capture pre-launch disk contents; do not claim execution is forcibly frozen."""
    captured_at = _now()
    contents = {}
    files = {}
    for path in _implementation_files(root):
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        if any(secret and secret.encode() in content for secret in secrets):
            raise ValueError("Implementation source contains a configured credential; snapshot refused.")
        contents[relative] = content
        files[relative] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "mode": path.stat().st_mode & 0o777,
        }
    expected = {name: item["sha256"] for name, item in files.items()}
    matches_capture = _implementation_hashes(root) == expected
    destination = run_dir / "implementation"
    destination.mkdir(mode=0o700, exist_ok=False)
    archive_path = destination / "source.tar.gz"
    with archive_path.open("wb") as archive_file:
        with gzip.GzipFile(filename="", mode="wb", fileobj=archive_file, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, content in contents.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    member.mode = files[name]["mode"]
                    archive.addfile(member, io.BytesIO(content))
    archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    index = {
        "schema_version": 1,
        "capture_kind": "pre_launch_workspace_snapshot",
        "captured_at_utc": captured_at,
        "capture_finished_at_utc": _now(),
        "execution_uses_snapshot": False,
        "execution_source": "Workspace files; this archive records disk contents before launch.",
        "workspace_matches_at_capture": matches_capture,
        "included_inputs": list(IMPLEMENTATION_INPUTS),
        "excluded": "Credentials, caches, dependencies, vendor sources, runtime/run directories and symlinks.",
        "archive": "source.tar.gz",
        "archive_sha256": archive_sha256,
        "file_count": len(files),
        "uncompressed_bytes": sum(item["size_bytes"] for item in files.values()),
        "files": files,
    }
    index_path = destination / "index.json"
    _write_json(index_path, index)
    archive_path.chmod(0o400)
    index_path.chmod(0o400)
    return {
        "path": "implementation/source.tar.gz",
        "index_path": "implementation/index.json",
        "captured_at_utc": captured_at,
        "archive_sha256": archive_sha256,
        "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "file_count": len(files),
        "execution_uses_snapshot": False,
        "workspace_matches_at_capture": matches_capture,
        "workspace_after": None,
    }


def compare_implementation_snapshot(root: Path, run_dir: Path, snapshot: dict) -> dict:
    """Report later disk changes as evidence, without overriding business outcomes."""
    index_path = run_dir / snapshot["index_path"]
    index_bytes = index_path.read_bytes()
    index = json.loads(index_bytes)
    before = {name: item["sha256"] for name, item in index["files"].items()}
    after = _implementation_hashes(root)
    changes = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    ui_changes = [
        name for name in changes
        if name == "src/forgeflow/ui.py" or name.startswith("src/forgeflow/ui_assets/")
    ]
    return {
        "checked_at_utc": _now(),
        "changed": bool(changes),
        "changed_files": changes,
        "ui_changed_files": ui_changes,
        "other_changed_files": [name for name in changes if name not in ui_changes],
        "snapshot_archive_unchanged": hashlib.sha256(
            (run_dir / snapshot["path"]).read_bytes()
        ).hexdigest() == snapshot["archive_sha256"],
        "snapshot_index_unchanged": hashlib.sha256(index_bytes).hexdigest() == snapshot["index_sha256"],
        "used_to_override_business_result": False,
    }


def _child_environment(root: Path) -> dict[str, str]:
    # Host runtime and Docker settings are needed; unrelated provider credentials
    # are deliberately not inherited by the Harbor process.
    allowed = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP",
        "LANG", "LC_ALL", "LC_CTYPE", "DOCKER_HOST", "DOCKER_CONTEXT",
        "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
        "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    }
    effective_environment = docker_environment(root)
    environment = {name: value for name, value in effective_environment.items() if name in allowed}
    environment.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
        PYTHONPATH=str(root / "src"),
        NO_COLOR="1",
        TERM="dumb",
    )
    return environment


def _verify_sources(root: Path, environment: dict[str, str], log: Path, secrets: tuple[str, ...]) -> None:
    result = subprocess.run(
        [sys.executable, str(root / "scripts/fetch_upstream.py"), "--verify-only"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    with log.open("a") as handle:
        handle.write(_redact(result.stdout + result.stderr, secrets))
    if result.returncode != 0:
        raise RuntimeError("Pinned upstream source verification failed; see source-verification.log.")


def _sanitize_harbor_artifacts(run_dir: Path, secrets: tuple[str, ...]) -> list[str]:
    changed = []
    if not any(secrets):
        return changed
    replacements = []
    for secret in secrets:
        if secret:
            replacements.extend([secret.encode(), json.dumps(secret)[1:-1].encode()])
    for path in run_dir.rglob("*"):
        # Preserve pinned task bytes and the source snapshot (credential-checked
        # at capture) rather than treating these immutable inputs as runtime logs.
        if path.is_relative_to(run_dir / "task") or path.is_relative_to(run_dir / "implementation"):
            continue
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(run_dir):
            continue
        original = path.read_bytes()
        redacted = original
        for value in replacements:
            redacted = redacted.replace(value, b"[REDACTED]")
        if redacted != original:
            path.write_bytes(redacted)
            changed.append(str(path.relative_to(run_dir)))
    return changed


def _stop_process_group(process: subprocess.Popen) -> list[str]:
    """Bound cleanup and tolerate a process exiting before its signal arrives."""
    errors = []

    def send(termination_signal):
        identity = getattr(process, "_forgeflow_identity", None)
        if identity is not None:
            from forgeflow.research_containers import host_group_quiescent
            from forgeflow.runtime_lifecycle import process_identity, same_process

            observed = process_identity(process.pid)
            if observed is not None and not same_process(identity, observed):
                raise ValueError("Recorded Harbor PID has been reused; refusing to signal.")
            if observed is None and host_group_quiescent(process.pid):
                return
        os.killpg(process.pid, termination_signal)

    for termination_signal, timeout in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
        try:
            # Signal the group even if its leader exited while output was still
            # held open by a child. A prior poll() would introduce a race.
            send(termination_signal)
        except ProcessLookupError:
            pass
        except Exception as error:
            errors.append(f"Could not signal Harbor process group: {type(error).__name__}")
            return errors
        try:
            process.wait(timeout=timeout)
            if termination_signal == signal.SIGTERM:
                # The leader may exit while a child ignores TERM and still owns
                # stdout or launches remote work. Reaping only the leader is not
                # process-group quiescence.
                try:
                    send(signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception as error:
                    errors.append(f"Could not stop Harbor descendants: {type(error).__name__}")
            return errors
        except subprocess.TimeoutExpired:
            continue
        except OSError as error:
            errors.append(f"Could not reap Harbor process: {type(error).__name__}")
            return errors
    errors.append("Harbor process did not exit within the bounded cleanup interval.")
    return errors


ATTESTATION_KEY_ENV = "FORGEFLOW_ATTESTATION_KEY"


def attest_run(run_dir: str | Path, key_path: str | Path) -> dict:
    """Sign the verifier artifacts of a finished run; never changes its result."""
    from forgeflow import attestation

    run_dir = Path(run_dir)
    trials = [
        path for path in sorted((run_dir / "harbor/job").glob("*"))
        if path.is_dir() and not path.is_symlink()
    ]
    if len(trials) != 1:
        return {"status": "skipped", "reason": "trial_missing"}
    trial = trials[0]
    manifest = _read_json(run_dir / "manifest.json", []) or {}
    result = _read_json(trial / "result.json", []) or {}
    verifier_result = result.get("verifier_result")
    verifier = {
        "command": "/tests/test.sh",
        "effective_uid": 0,
        "cwd": "/workspace",
        "started_at_utc": (result.get("verifier") or {}).get("started_at"),
        "finished_at_utc": (result.get("verifier") or {}).get("finished_at"),
        "exit_code": 0 if verifier_result else 1,
    }
    record = attestation.build_verifier_record(
        run_id=run_dir.name,
        task=manifest.get("task"),
        agent=manifest.get("agent"),
        model=(manifest.get("model") or {}).get("model"),
        verifier=verifier,
        artifacts=attestation.read_artifacts(trial / "verifier"),
    )
    key = attestation.load_key(Path(key_path))
    sealed, signature = attestation.seal_record(record, key)
    attestation.write_record(run_dir, sealed, signature)
    return {
        "status": "verified",
        "key_id": key["key_id"],
        "artifact_count": len(record["artifacts"]),
    }


def run_task(
    root: str | Path,
    task: str,
    agent: str,
    *,
    agent_timeout_sec: int = 3600,
    run_id: str | None = None,
    max_output_tokens: int | None = None,
    thinking_mode: str = "provider-default",
    execution_guidance: str = "none",
    data_profile: str = "original",
    planner_profile: str = "none",
    security_profile: str = "none",
    attest_key: str | Path | None = None,
    contract_path: str | Path | None = None,
    require_contract: bool = False,
    contract_guidance: str = "none",
) -> Path:
    """Run one trial; preserve preflight failures, process failures and exceptions.

    Invalid task/agent/run identifiers raise before creating a run. Once a run
    exists, its terminal state is written and the path is returned even on failure.
    """
    root = Path(root).resolve()
    task_name = resolve_task(task)
    contract_payload = None
    contract = None
    if require_contract and contract_path is None:
        raise ValueError("This run requires a reviewed task contract.")
    if contract_path is not None:
        from forgeflow.contract import contract_from_payload

        try:
            contract_payload = json.loads(Path(contract_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError("Contract must be a readable JSON object.") from error
        if not isinstance(contract_payload, dict):
            raise ValueError("Contract must be a JSON object.")
        contract = contract_from_payload(contract_payload)
        if contract.task_id != task_name:
            raise ValueError("Contract task_id does not match the requested task.")
    if agent not in AGENTS:
        raise ValueError("Agent must be oracle, nop, or pi.")
    from forgeflow.contract_guidance import resolve_contract_guidance
    contract_guidance_record = resolve_contract_guidance(contract_guidance, contract_payload)
    if contract_guidance != "none" and (agent != "pi" or planner_profile != "cp-sat-recovery-v1"):
        raise ValueError("Contract guidance requires Pi with cp-sat-recovery-v1.")
    _validate_output_budget_argument(agent, max_output_tokens)
    _validate_thinking_mode_argument(agent, thinking_mode)
    validate_execution_guidance(
        execution_guidance, agent=agent, planner_profile=planner_profile,
    )
    validate_security_profile(security_profile)
    if security_profile == PROFILE and agent == "pi" and planner_profile != "cp-sat-recovery-v1":
        # Environment controls (nop/oracle) may run under the profile to prove
        # the container boots; only a Pi benchmark run needs the write path.
        raise ValueError("permission-split-v1 requires cp-sat-recovery-v1 for the Pi agent")
    profile_record = _data_profile_record(data_profile)
    planner_record = _planner_record(agent, planner_profile)
    if run_id is None:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"-{agent}-{uuid.uuid4().hex[:6]}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", run_id):
        raise ValueError("Run ID must be a simple directory name.")
    runs_root = root / "runs"
    if runs_root.is_symlink():
        raise ValueError("The runs directory must not be a symlink.")
    runs_root.mkdir(mode=0o700, exist_ok=True)
    run_dir = runs_root / run_id
    run_dir.mkdir(mode=0o700, exist_ok=False)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "task": task_name,
        "agent": agent,
        "thinking_mode": thinking_mode if agent == "pi" else None,
        "mode": "benchmark" if agent == "pi" else "environment_control",
        "data_profile": profile_record,
        "planner": planner_record,
        "state": "preflight",
        "started_at_utc": _now(),
        "finished_at_utc": None,
        "harbor_version": version("harbor"),
        "runtime": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "host_system": platform.system(),
            "host_architecture": platform.machine(),
        },
        "harbor_returncode": None,
        "budget": {"agent_timeout_sec": agent_timeout_sec, "attempts": 1, "concurrency": 1, "max_retries": 0},
        "verifier_adapter": VERIFIER_IMPORT_PATH,
        "source_integrity_before": None,
        "source_integrity_after": None,
        "task_input_policy": "Pinned original task; no ForgeFlow edits or extra bind mounts",
        "execution_config_overrides": {},
        "task_derivation": None,
        "implementation_snapshot": None,
        "frozen_execution": None,
        "execution_guidance": resolve_execution_guidance(execution_guidance) if agent == "pi" else None,
        "contract_guidance": contract_guidance_record,
        "redacted_artifacts": [],
    }
    if data_profile != "original":
        manifest["mode"] += "_visibility_experiment"
    secrets: tuple[str, ...] = ()
    process = None
    watchdog = None
    heavy_lease = None
    residue_path = root / ".runtime/lifecycle/research-residue.json"
    owns_residue = False
    environment = {}
    if contract is not None and contract_payload is not None:
        manifest["contract"] = {
            "id": contract.contract_id,
            "sha256": contract.digest(),
            "task_id": contract.task_id,
        }
        _write_json(run_dir / "contract.json", contract_payload)
    _write_json(run_dir / "manifest.json", manifest)
    try:
        from forgeflow.runtime_lifecycle import exclusive_lease

        if (root / ".runtime").is_symlink() or (root / ".runtime/lifecycle").is_symlink():
            raise ValueError("Heavy-operation state must not traverse a symlink.")
        lease = exclusive_lease(root / ".runtime/lifecycle/heavy-operation.lock")
        lease.__enter__()
        heavy_lease = lease
        manifest["resource_policy"] = {"heavy_operation_concurrency": 1, "lease_acquired": True}
        environment = _child_environment(root)
        if residue_path.exists() or residue_path.is_symlink():
            raise RuntimeError(
                "An earlier research run has unconfirmed cleanup; reconcile its recorded resources first."
            )
        if contract_payload is not None:
            environment["FORGEFLOW_CONTRACT_JSON"] = json.dumps(contract_payload)
        if contract_guidance != "none":
            environment["FORGEFLOW_CONTRACT_GUIDANCE"] = contract_guidance
        model = None
        if agent == "pi":
            config = load_config(root, require_model=True)
            secrets = (config.api_key,)
            model = config.model
            environment.update(
                NEBIUS_API_KEY=config.api_key,
                NEBIUS_BASE_URL=config.base_url,
                NEBIUS_MODEL=config.model,
            )
            manifest["model"] = config.public_dict()
            from forgeflow.agents.pi_nebius import (
                PiNebius,
                build_models_config,
            )

            effective_output_tokens = max_output_tokens
            model_settings = build_models_config(
                config, model, max_output_tokens=effective_output_tokens,
                thinking_mode=thinking_mode,
            )["providers"]["nebius"]["models"][0]
            manifest["budget"].update(
                max_output_tokens=effective_output_tokens,
                max_output_tokens_source=(
                    "provider_default" if max_output_tokens is None else "explicit_run_argument"
                ),
                output_limit_request_fields="omitted" if max_output_tokens is None else "included",
                output_policy_version="all-requests-v2" if max_output_tokens is None else "explicit",
                context_window_tokens=model_settings["contextWindow"],
                sampling_params=model_settings["samplingParams"],
                output_budget_scope="Per model response, shared by reasoning and answer tokens.",
            )

            setup_timeout = PiNebius.__dict__.get("DEFAULT_SETUP_TIMEOUT_SEC")
            if setup_timeout is not None:
                if (
                    isinstance(setup_timeout, bool) or not isinstance(setup_timeout, (int, float))
                    or not math.isfinite(setup_timeout) or setup_timeout <= 0
                ):
                    raise ValueError("Pi declares an invalid setup timeout.")
                manifest["budget"].update(
                    mechanism=BUDGET_MECHANISM_VERSION,
                    cleanup_allowance_sec=CLEANUP_ALLOWANCE_SEC,
                    agent_outer_timeout_sec=agent_timeout_sec + CLEANUP_ALLOWANCE_SEC,
                    agent_setup_timeout_sec=SETUP_BUDGET_SEC,
                    agent_setup_outer_timeout_sec=setup_timeout,
                    agent_setup_timeout_source="forgeflow.budget.SETUP_BUDGET_SEC",
                )
        manifest["source_integrity_before"] = False
        _verify_sources(root, environment, run_dir / "source-verification.log", secrets)
        manifest["source_integrity_before"] = True
        manifest["upstream"] = json.loads((root / "upstream.lock.json").read_text())["sources"]
        task_path, derivation = stage_task(
            root, task_name, run_dir, agent=agent,
            allowed_host=(urlsplit(config.base_url).hostname or "") if agent == "pi" else None,
            data_profile=data_profile,
            security_profile=security_profile,
        )
        manifest["task_derivation"] = derivation
        manifest["data_profile"] = derivation["data_profile"]
        manifest["security_profile"] = derivation["security_profile"]
        manifest["execution_config_overrides"] = derivation["execution_config_overrides"]
        manifest["task_input_policy"] = (
            "Original vendor task preserved; execution copy applies only declared Docker build "
            "compatibility and Pi Agent network policy patches; no host task directory is mounted into the Agent"
        )
        if data_profile != "original":
            manifest["task_input_policy"] += (
                "; planning-metadata-v1 also appends existing workcenter fields to public notes "
                "as a separate visibility experiment, not the original benchmark condition"
            )
        manifest["implementation_snapshot"] = capture_implementation_snapshot(
            root, run_dir, secrets=secrets,
        )
        from forgeflow.frozen_execution import IMPORT_PROBE, materialize_execution, verify_execution

        frozen = materialize_execution(
            root, run_dir, manifest["implementation_snapshot"],
            agent=agent, planner_profile=planner_profile, secrets=secrets,
        )
        manifest["frozen_execution"] = frozen
        execution_root = run_dir / frozen["root"]
        from forgeflow.host_runtime import PATH_BOOTSTRAP, ensure_host_runtime, verify_host_runtime

        runtime = ensure_host_runtime(
            execution_root, root / ".runtime/frozen-host", root / ".venv/bin/python",
        )
        frozen["host_runtime"] = runtime
        frozen["host_dependencies_frozen"] = True
        distributions = dict(runtime.get("import_probe", {}).get("distributions", []))
        if "harbor" in distributions:
            manifest["harbor_version"] = distributions["harbor"]
            manifest["harbor_version_source"] = "frozen_host_runtime_actual_import"
        environment["PYTHONPATH"] = os.pathsep.join((str(execution_root / "src"), runtime["packages"]))
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONSAFEPATH"] = "1"
        environment["PATH"] = os.pathsep.join((
            str(Path(runtime["python"]).parent), str(Path(runtime["packages"]) / "bin"),
            environment.get("PATH", ""),
        ))
        modules = ["forgeflow", "forgeflow.runner", "forgeflow.verifier"]
        if agent == "pi":
            modules.append("forgeflow.agents.pi_nebius")
        probe = subprocess.run(
            [runtime["python"], "-I", "-B", "-S", "-c", PATH_BOOTSTRAP + IMPORT_PROBE,
             str(execution_root / "src"), runtime["packages"], str(execution_root), *modules],
            cwd=execution_root, env=environment, capture_output=True, text=True, timeout=30, check=False,
        )
        if probe.returncode:
            raise RuntimeError("Frozen execution import probe failed; no trial was launched.")
        frozen["import_probe"] = json.loads(probe.stdout)
        frozen["integrity_before"] = verify_execution(run_dir, frozen)
        runtime["integrity_before"] = verify_host_runtime(runtime)
        command = build_run_command(
            root, task_name, agent, run_dir, model=model,
            agent_timeout_sec=agent_timeout_sec, task_path=task_path,
            max_output_tokens=effective_output_tokens if agent == "pi" else None,
            thinking_mode=thinking_mode,
            execution_guidance=execution_guidance,
            planner_profile=planner_profile,
            security_profile=security_profile,
            execution_root=execution_root,
            host_runtime=runtime,
        )
        manifest["command"] = command
        resolved = subprocess.run(
            command + ["--print-config"],
            cwd=execution_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        (run_dir / "harbor-config.stderr.log").write_text(_redact(resolved.stderr, secrets))
        if resolved.returncode:
            (run_dir / "harbor-config.stdout.log").write_text(_redact(resolved.stdout, secrets))
            raise RuntimeError("Harbor rejected the run configuration; see harbor-config logs.")
        _write_json(run_dir / "harbor-config.json", json.loads(resolved.stdout), secrets)
        frozen["integrity_at_launch"] = verify_execution(run_dir, frozen)
        runtime["integrity_at_launch"] = verify_host_runtime(runtime)
        manifest["state"] = "running"
        _write_json(run_dir / "manifest.json", manifest, secrets)
        if agent == "pi":
            from forgeflow.run_watchdog import RunWatchdog

            task_settings = tomllib.loads((task_path / "task.toml").read_text())
            watchdog = RunWatchdog(
                run_dir, agent_sec=agent_timeout_sec,
                verifier_sec=task_settings["verifier"]["timeout_sec"],
            )
        with (run_dir / "harbor-output.log").open("w") as output:
            if agent == "pi":
                from forgeflow.research_containers import MECHANISM
                from forgeflow.runtime_lifecycle import write_record

                write_record(residue_path, {"schema": 1, "mechanism": MECHANISM,
                                            "run_dir": str(run_dir)})
                owns_residue = True
                manifest["container_cleanup_mechanism"] = MECHANISM
            launch_options = dict(cwd=execution_root, env=environment, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors="replace")
            if agent == "pi":
                from forgeflow.runtime_lifecycle import spawn_managed

                process = spawn_managed(run_dir / "host-harbor.json", command,
                                        "research-harbor", **launch_options)
            else:
                process = subprocess.Popen(command, start_new_session=True, **launch_options)
            if watchdog is not None:
                watchdog.start(process, _stop_process_group)
            assert process.stdout is not None
            for line in process.stdout:
                output.write(_redact(line, secrets))
                output.flush()
            manifest["harbor_returncode"] = process.wait()
        if watchdog is not None:
            manifest["host_watchdog"] = watchdog.stop()
            if manifest["host_watchdog"]["status"] == "terminated":
                raise RuntimeError("Host watchdog terminated the trial; execution may be partial.")
        manifest["state"] = "completed" if process.returncode == 0 else "failed"
        frozen["integrity_after"] = verify_execution(run_dir, frozen)
        runtime["integrity_after"] = verify_host_runtime(runtime)
        if manifest["task_derivation"] is not None:
            manifest["task_derivation"]["derived_task_integrity_after"] = False
            if _task_file_hashes(task_path) != manifest["task_derivation"]["execution_file_sha256"]:
                raise RuntimeError("The derived task files changed during the run.")
            manifest["task_derivation"]["derived_task_integrity_after"] = True
        manifest["source_integrity_after"] = False
        _verify_sources(root, environment, run_dir / "source-verification.log", secrets)
        manifest["source_integrity_after"] = True
    except (Exception, KeyboardInterrupt) as error:
        manifest["state"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        manifest["error"] = {"type": type(error).__name__, "message": _redact(str(error), secrets)}
        if process is not None:
            cleanup_errors = _stop_process_group(process)
            if cleanup_errors:
                manifest["process_cleanup_errors"] = cleanup_errors
            manifest["harbor_returncode"] = process.returncode
    finally:
        if watchdog is not None:
            manifest["host_watchdog"] = watchdog.stop()
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if owns_residue:
            from forgeflow.research_containers import quiesce_run, wait_host_group

            host_errors = _stop_process_group(process) if process is not None else []
            if process is not None:
                try:
                    if not wait_host_group(process.pid):
                        host_errors.append("Harbor process group still has live members.")
                except Exception as error:
                    host_errors.append(f"Could not verify host quiescence: {type(error).__name__}")
            if host_errors:
                manifest["process_cleanup_errors"] = host_errors
            cleanup = (quiesce_run(run_dir, environment) if not host_errors else
                       {"status": "host_shutdown_unconfirmed", "quiescent": False})
            manifest["container_cleanup"] = cleanup
            if cleanup["quiescent"] and not host_errors:
                residue_path.unlink()
            else:
                manifest["state"] = "failed"
                manifest["resource_cleanup_unconfirmed"] = True
        if heavy_lease is not None:
            heavy_lease.__exit__(None, None, None)
        manifest["finished_at_utc"] = _now()
        if manifest["implementation_snapshot"] is not None:
            try:
                manifest["implementation_snapshot"]["workspace_after"] = compare_implementation_snapshot(
                    root, run_dir, manifest["implementation_snapshot"],
                )
            except Exception as error:
                manifest["implementation_snapshot"]["workspace_after"] = {
                    "checked_at_utc": _now(), "status": "unavailable",
                    "error_type": type(error).__name__,
                    "used_to_override_business_result": False,
                }
        manifest["redacted_artifacts"] = _sanitize_harbor_artifacts(run_dir, secrets)
        _write_json(run_dir / "manifest.json", manifest, secrets)
        summary = summarize_run(run_dir)
        if summary["environment_chain_ok"] is False and manifest["state"] == "completed":
            manifest["state"] = "failed"
            _write_json(run_dir / "manifest.json", manifest, secrets)
            summary = summarize_run(run_dir)
        _write_json(run_dir / "summary.json", summary, secrets)
        key_path = attest_key or os.environ.get(ATTESTATION_KEY_ENV)
        if key_path:
            try:
                summary["attestation"] = attest_run(run_dir, key_path)
            except Exception as error:  # signing must never change the result
                summary["attestation"] = {
                    "status": "failed", "reason": type(error).__name__,
                }
            _write_json(run_dir / "summary.json", summary, secrets)
    return run_dir


def _read_json(path: Path, issues: list[str]) -> dict | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict):
            raise ValueError("expected an object")
        return payload
    except (ValueError, OSError) as error:
        issues.append(f"Invalid {path.name}: {type(error).__name__}")
        return None


def rule_projection(reward: dict | None) -> dict | None:
    """Summarize original rule rows without inventing applicability.

    Returns ``None`` when the reward carries no parseable rule rows, so callers
    keep "unknown" instead of treating missing rules as a pass. Skipped (NA)
    rules stay out of the applicable count, and a failed rule is never dropped.
    """
    rules = reward.get("rules") if isinstance(reward, dict) else None
    if not isinstance(rules, dict) or not isinstance(rules.get("by_dimension"), dict):
        return None
    groups = list(rules["by_dimension"].values())
    if not all(isinstance(group, list) for group in groups):
        return None
    rows = [row for group in groups for row in group]
    if not rows or not all(
        isinstance(row, dict) and row.get("status") in ("PASS", "FAIL", "NA") for row in rows
    ):
        return None
    issue = None
    if any(
        "applicable" in row and row["applicable"] != (row["status"] != "NA")
        for row in rows
    ):
        issue = "Rule applicability flags disagree with rule statuses."
    applicable = [row for row in rows if row.get("applicable", row["status"] != "NA")]
    return {
        "applicable": len(applicable),
        "passed": sum(row["status"] == "PASS" for row in applicable),
        "failed": sum(row["status"] == "FAIL" for row in applicable) if applicable else None,
        "all_pass": all(row["status"] == "PASS" for row in applicable) if applicable else None,
        "issue": issue,
    }


def summarize_run(run_dir: str | Path) -> dict:
    """Read original results without normalizing 0–100 reward to a binary score."""
    run_dir = Path(run_dir).resolve()
    issues: list[str] = []
    manifest = _read_json(run_dir / "manifest.json", issues) or {}
    trial_dirs = [
        path for path in sorted((run_dir / "harbor/job").glob("*"))
        if path.is_dir() and not path.is_symlink()
    ]
    trial_dirs = [
        path for path in trial_dirs
        if (path / "config.json").exists() or (path / "result.json").exists() or (path / "verifier").exists()
    ]
    if len(trial_dirs) > 1:
        issues.append("Expected one trial but found multiple; no aggregate success is inferred.")
    trial = trial_dirs[0] if len(trial_dirs) == 1 else None
    reward_path = trial / "verifier/reward.json" if trial else None
    reward = _read_json(reward_path, issues) if reward_path else None
    result_path = trial / "result.json" if trial else None
    result = _read_json(result_path, issues) if result_path else None
    score = reward.get("overall_score") if reward else None
    if score is not None and (
        isinstance(score, bool) or not isinstance(score, (float, int))
        or not math.isfinite(score) or not 0 <= score <= 100
    ):
        issues.append("Original reward score is outside the finite 0–100 scale.")
        score = None
    original_passed = reward.get("passed") if reward else None
    if not isinstance(original_passed, bool):
        original_passed = None
    if score is not None and original_passed is not None and original_passed != (score >= 99):
        issues.append("Original passed flag disagrees with the original score.")
    rule_result = rule_projection(reward)
    all_rules_pass = None
    failed_rules = None
    if rule_result is not None:
        all_rules_pass = rule_result["all_pass"]
        failed_rules = rule_result["failed"]
        if rule_result["issue"]:
            issues.append(rule_result["issue"])
    raw_strict_success = None
    if score is not None and not issues:
        if original_passed is False:
            raw_strict_success = False
        elif original_passed is True and all_rules_pass is not None:
            raw_strict_success = all_rules_pass
    exception = result.get("exception_info") if result else None
    if result is not None and result.get("task_name") != f"agentic-labs/{manifest.get('task')}":
        issues.append("Harbor trial task identity does not match the selected original task.")
    if result is not None and (reward is not None or not exception):
        verifier_result = result.get("verifier_result")
        harbor_rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
        harbor_score = harbor_rewards.get("reward") if isinstance(harbor_rewards, dict) else None
        if (
            isinstance(harbor_score, bool)
            or not isinstance(harbor_score, (int, float))
            or not math.isfinite(harbor_score)
            or not 0 <= harbor_score <= 100
        ):
            issues.append("Harbor verifier_result.rewards.reward is missing or invalid.")
        elif score is not None and not math.isclose(score, harbor_score, rel_tol=0, abs_tol=1e-9):
            issues.append("Harbor reward disagrees with the original overall_score.")
    chain_ok = None
    if manifest.get("state") in ("failed", "interrupted") or exception:
        chain_ok = False
    elif (
        manifest.get("state") == "completed"
        and manifest.get("harbor_returncode") == 0 and result is not None
        and result.get("verifier_result") is not None and reward is not None
    ):
        chain_ok = not issues and score is not None and original_passed is not None
    elif manifest.get("state") == "completed":
        chain_ok = False
        issues.append("Run completed without a complete original verifier result.")
    strict_success = raw_strict_success if chain_ok is True else None
    expected = None
    if chain_ok is True and manifest.get("agent") == "nop":
        expected = score == 0 and original_passed is False
    elif chain_ok is True and manifest.get("agent") == "oracle" and strict_success is not None:
        expected = score == 100 and strict_success is True
    agent_result = result.get("agent_result") if result else None
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    metadata = agent_result.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    data_profile = manifest.get("data_profile")
    planner = manifest.get("planner")
    return {
        "schema_version": 1,
        "run_id": manifest.get("run_id", run_dir.name),
        "task": manifest.get("task"),
        "agent": manifest.get("agent"),
        "mode": manifest.get("mode"),
        "data_profile": data_profile.get("name") if isinstance(data_profile, dict) else None,
        "planner_profile": planner.get("profile") if isinstance(planner, dict) else None,
        "state": manifest.get("state", "unknown"),
        "score": score,
        "score_scale": [0, 100],
        "original_passed": original_passed,
        "raw_task_strict_success": raw_strict_success,
        "strict_success": strict_success,
        "all_applicable_rules_pass": all_rules_pass,
        "failed_rule_count": failed_rules,
        "environment_chain_ok": chain_ok,
        "evaluation_valid": chain_ok,
        "expected_control_outcome": expected,
        "harbor_returncode": manifest.get("harbor_returncode"),
        "trial_exception": exception,
        "runner_error": manifest.get("error"),
        "raw_reward_path": str(reward_path.relative_to(run_dir)) if reward_path and reward_path.exists() else None,
        "raw_result_path": str(result_path.relative_to(run_dir)) if result_path and result_path.exists() else None,
        "metrics": {
            **{key: agent_result.get(key) for key in
               ("n_input_tokens", "n_output_tokens", "n_cache_tokens", "cost_usd")},
            "usage_accounting": accounting_view(metadata.get("usage_accounting")),
        },
        "issues": issues,
    }
