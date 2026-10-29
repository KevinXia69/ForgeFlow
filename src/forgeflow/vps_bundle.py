"""Explicit root-controlled bundle activation without rewriting historical bindings.

Only the original instance's bound provenance may use the historical validator.
The execution bundle must still satisfy the current recovery source contract.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from forgeflow.planning.bundle import ERP_BENCH_COMMIT, OWN_FILES, UPSTREAM_FILES
from forgeflow.recovery.storage import atomic_json, digest, read_json

_lstat = os.lstat


def configured_bundle(value: object) -> Path:
    if (not isinstance(value, str) or not Path(value).is_absolute()
            or str(Path(value)) != value or ".." in Path(value).parts):
        raise ValueError("invalid_active_bundle_path")
    return Path(value)


def _trusted_metadata(path: Path, *, directory: bool) -> None:
    info = _lstat(path)
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected_type(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or (not directory and info.st_nlink != 1)):
        raise RuntimeError("unsafe_bundle_path")


def trusted_directory(path: Path) -> None:
    """Every ancestor must protect the directory from unprivileged replacement."""
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise RuntimeError("unsafe_bundle_path")
        for parent in (*reversed(path.parents), path):
            _trusted_metadata(parent, directory=True)
    except OSError:
        raise RuntimeError("unsafe_bundle_path") from None


def _trusted_tree(path: Path) -> None:
    trusted_directory(path)
    try:
        for entry in path.rglob("*"):
            _trusted_metadata(entry, directory=entry.is_dir())
    except OSError:
        raise RuntimeError("unsafe_bundle_path") from None


def _planner_profile(provenance: dict) -> None:
    files = provenance.get("files_sha256")
    required = {"plan.py", "requirements.txt", "forgeflow/__init__.py", "recover.py",
                "forgeflow/recovery/service.py"}
    required |= {f"forgeflow/planning/{name}" for name in OWN_FILES}
    required |= set(UPSTREAM_FILES.values())
    if (provenance.get("profile") != "cp-sat-recovery-v1"
            or provenance.get("upstream_commit") != ERP_BENCH_COMMIT
            or provenance.get("recovery_enabled") is not True
            or not isinstance(files, dict) or not required <= files.keys()):
        raise RuntimeError("invalid_bundle_profile")


def verify_historical_bundle(bundle: Path, expected_provenance: str) -> dict:
    """Verify original hashes, without imposing new files on an immutable old bundle."""
    _trusted_tree(bundle)
    provenance = read_json(bundle / "provenance.json")
    if not isinstance(provenance, dict) or digest(provenance) != expected_provenance:
        raise RuntimeError("historical_bundle_binding_changed")
    _planner_profile(provenance)
    for name, expected in provenance["files_sha256"].items():
        if (not isinstance(name, str) or not name or Path(name).is_absolute()
                or str(Path(name)) != name or ".." in Path(name).parts
                or not isinstance(expected, str) or re.fullmatch(r"[a-f0-9]{64}", expected) is None):
            raise RuntimeError("invalid_historical_source_manifest")
        path = bundle / name
        try:
            _trusted_metadata(path, directory=False)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            raise RuntimeError("historical_source_missing") from None
        if actual != expected:
            raise RuntimeError("historical_source_bytes_changed")
    return provenance


def activate_bundle(record: dict, sandbox: Path, selected: object) -> Path:
    """Validate both bundles and append a durable, immutable activation record.

    The caller has already authenticated the instance and all original artifact
    bindings. Root config is the explicit authorization; no business approval or
    execution journal is consumed or changed here.
    """
    from forgeflow.recovery.service import verify_sources

    bundle = configured_bundle(selected)
    historical = sandbox / "planner"
    if bundle == historical or bundle.is_relative_to(historical) or historical.is_relative_to(bundle):
        raise RuntimeError("active_bundle_must_be_separate")
    verify_historical_bundle(historical, record["bindings"]["planner/provenance.json"])
    _trusted_tree(bundle)
    provenance = verify_sources(bundle)
    _planner_profile(provenance)
    # A path is immutable once selected. A later release gets a new directory,
    # even if an operator also rewrites its self-reported provenance hashes.
    activation = {
        "schema": 1, "instance_id": record["instance_id"], "generation": record["generation"],
        "original_bindings_sha256": digest(record["bindings"]),
        "active_bundle": str(bundle), "active_provenance_sha256": digest(provenance),
    }
    directory = sandbox / "bundle-activations"
    directory.mkdir(mode=0o700, exist_ok=True)
    trusted_directory(directory)
    target = directory / (hashlib.sha256(str(bundle).encode()).hexdigest() + ".json")
    if not target.exists() and not target.is_symlink():
        atomic_json(target, activation)
    _trusted_metadata(target, directory=False)
    if read_json(target) != activation:
        raise RuntimeError("bundle_activation_binding_changed")
    return bundle
