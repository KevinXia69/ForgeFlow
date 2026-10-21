"""Host-only ownership and bounded quiescence for guarded Harbor Docker runs.

The ledger is written before Harbor can create containers, outside mounted log
directories. Stopping owned containers does not roll back ERP writes or prove
that a remote provider stopped generating or billing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

LEDGER = "host-containers.json"
MECHANISM = "compose-owner-quiescence-v1"
_ID = re.compile(r"[0-9a-f]{64}")
_PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,199}")
_INSPECT = '{{json .Id}} {{json .State.Running}} {{json .State.Paused}} {{json (index .Config.Labels "com.docker.compose.project")}} {{json (index .Config.Labels "com.docker.compose.project.working_dir")}}'


def docker_environment(environment: dict | None = None) -> dict:
    source = os.environ if environment is None else environment
    allowed = {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
               "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "TMPDIR"}
    return {name: value for name, value in source.items() if name in allowed}


def host_group_quiescent(pid: int) -> bool:
    """Check live members after signals; a reaped leader alone is insufficient."""
    from forgeflow.runtime_lifecycle import process_identity

    table = subprocess.run(["ps", "-axo", "pid=,pgid="], capture_output=True,
                           text=True, timeout=5, check=True)
    for row in table.stdout.splitlines():
        child, group = map(int, row.split())
        if group == pid:
            identity = process_identity(child)
            if identity is not None and identity["pgid"] == pid:
                return False
    return True


def wait_host_group(pid: int, timeout: float = 3) -> bool:
    end = time.monotonic() + timeout
    while True:
        if host_group_quiescent(pid):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)


def _command(arguments: list[str], environment: dict, *, timeout: int = 8) -> str:
    result = subprocess.run(
        ["docker", *arguments], env=docker_environment(environment), capture_output=True,
        text=True, timeout=timeout, check=False,
    )
    if result.returncode:
        # Never forward raw Docker output: diagnostics can include configuration.
        raise RuntimeError("Docker ownership or quiescence command failed.")
    if len(result.stdout) > 128 * 1024:
        raise ValueError("Docker ownership response exceeds its bound.")
    return result.stdout.strip()


def _engine(environment: dict) -> str:
    value = _command(["info", "--format", "{{.ID}}"], environment)
    if not value or len(value) > 128 or any(c.isspace() for c in value):
        raise ValueError("Missing Docker engine identity.")
    return value


def _containers(project: str, environment: dict) -> list[str]:
    values = _command([
        "ps", "-aq", "--no-trunc", "--filter", f"label=com.docker.compose.project={project}",
    ], environment).split()
    if len(values) > 8 or len(set(values)) != len(values) or any(not _ID.fullmatch(x) for x in values):
        raise ValueError("Invalid or excessive project containers.")
    return sorted(values)


def _plain(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Ownership paths must not traverse symlinks.")


def _validate(record: dict, run_dir: Path) -> None:
    if record.get("schema") != 1 or record.get("mechanism") != MECHANISM:
        raise ValueError("Unsupported container ownership ledger.")
    if not _PROJECT.fullmatch(record.get("project", "")) or record.get("preexisting_ids") != []:
        raise ValueError("Container ownership was not established before launch.")
    directory = Path(record["directory"])
    _plain(directory)
    if not directory.is_absolute() or not directory.is_relative_to(run_dir / "task"):
        raise ValueError("Container project directory is outside this run's staged task.")
    if str(directory.resolve()) != str(directory):
        raise ValueError("Noncanonical container project directory.")


def register_environment(trial_dir: Path, directory: Path, project: str, environment: dict) -> dict:
    _plain(trial_dir)
    run_dir = trial_dir.parents[2]
    if trial_dir.parent != run_dir / "harbor/job":
        raise ValueError("Unexpected Harbor trial ownership location.")
    record = {"schema": 1, "mechanism": MECHANISM, "project": project,
              "directory": str(directory), "preexisting_ids": []}
    _validate(record, run_dir)
    record["engine_id"] = _engine(environment)
    if _containers(project, environment):
        raise ValueError("Refusing a preexisting Compose project; nothing was stopped.")
    trial_dir.mkdir(parents=True, exist_ok=True)
    path = trial_dir / LEDGER
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(trial_dir, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return record


def _inspect(container_id: str, record: dict, environment: dict) -> dict:
    raw = _command(["inspect", "--format", _INSPECT, container_id], environment)
    decoder = json.JSONDecoder()
    values = []
    while raw:
        value, consumed = decoder.raw_decode(raw)
        values.append(value)
        raw = raw[consumed:].lstrip()
    if len(values) != 5:
        raise ValueError("Invalid container identity projection.")
    actual, running, paused, project, directory = values
    if actual != container_id or project != record["project"] or directory != record["directory"]:
        raise ValueError("Container ownership changed; refusing to stop it.")
    if not isinstance(running, bool) or not isinstance(paused, bool):
        raise ValueError("Invalid container running state.")
    return {"id": actual, "running": running, "paused": paused}


def quiesce_run(run_dir: Path, environment: dict) -> dict:
    """Call only after the Harbor host group is stopped; never removes containers."""
    report = {"mechanism": MECHANISM, "status": "unknown", "quiescent": False,
              "containers": [], "erp_rollback_confirmed": False,
              "provider_cancellation_confirmed": False}
    try:
        _plain(run_dir)
        _plain(run_dir / "harbor/job")
        paths = list((run_dir / "harbor/job").glob(f"*/{LEDGER}"))
        if not paths:
            # GuardedDockerEnvironment cannot create anything before this ledger.
            return {**report, "status": "not_registered", "quiescent": True}
        if len(paths) != 1:
            raise ValueError("Ambiguous container ownership ledger.")
        path = paths[0]
        _plain(path)
        if not path.is_file() or path.stat().st_size > 16384:
            raise ValueError("Invalid container ownership ledger.")
        record = json.loads(path.read_text())
        _validate(record, run_dir)
        if _engine(environment) != record["engine_id"]:
            raise ValueError("Docker engine identity changed; refusing cleanup.")
        ids = _containers(record["project"], environment)
        before = [_inspect(cid, record, environment) for cid in ids]
        report["containers"] = before
        # Validate every container before mutating any; foreign labels fail closed.
        paused = [row["id"] for row in before if row["paused"]]
        if paused:
            _command(["unpause", *paused], environment)
        running = [row["id"] for row in before if row["running"] or row["paused"]]
        if running:
            _command(["stop", "--time", "5", *running], environment, timeout=18)
        remaining = _containers(record["project"], environment)
        if not set(remaining) <= set(ids):
            raise ValueError("A new container appeared after host shutdown.")
        after = [_inspect(cid, record, environment) for cid in remaining]
        if any(row["running"] or row["paused"] for row in after):
            raise ValueError("A trial container is still active.")
        report.update(status="stopped" if ids else "absent", quiescent=True, containers=after)
    except Exception as error:
        report["error_type"] = type(error).__name__
    return report
