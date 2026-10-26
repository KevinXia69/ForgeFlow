#!/usr/bin/env python3
"""Offline, journaled creation of a fresh ERP environment for the next reviewer.

Requires root, an inactive ForgeFlow runtime, and its old Odoo container already
stopped. Preserves every old directory/container and never starts the API.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
from contextlib import ExitStack
from pathlib import Path

from forgeflow.product.lease import EnvironmentLease
from forgeflow.vps_runtime import configuration, lifecycle_root, trusted_json


class RotationError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def canonical(path):
    value = Path(path)
    if not value.is_absolute() or value.resolve() != value or value.is_symlink():
        raise RotationError("canonical_absolute_path_required")
    return value


def save(path, value):
    if path.is_symlink():
        raise RotationError("symlink_state_refused")
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", closefd=False) as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(fd)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)


def load_script(repo, name):
    path = repo / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location("forgeflow_rotation_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record_for(config):
    root = canonical(config["sandbox_root"])
    record = trusted_json(root / "vps-instance.json")
    if (record.get("schema") != 1 or record.get("phase") != "ready"
            or record.get("container") != config["container"]
            or record.get("volume") != str(root / "odoo")
            or record.get("odoo_port") != config["odoo_port"]
            or re.fullmatch(r"[0-9a-f]{32}", str(record.get("generation"))) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(record.get("image_id"))) is None):
        raise RotationError("ready_instance_binding_required")
    return record


def case_directory(config, *, create=False):
    parent = canonical(config["case_root"])
    if not parent.is_dir():
        raise RotationError("existing_api_case_directory_required")
    path = canonical(parent / "product-cases")
    if create and not path.exists():
        path.mkdir(mode=0o700)
        info = parent.stat()
        os.chown(path, info.st_uid, info.st_gid)
    if path.exists() and (not path.is_dir() or path.stat().st_uid != parent.stat().st_uid):
        raise RotationError("api_case_directory_ownership_mismatch")
    return path


def assert_stopped(backup, config):
    unit = config.get("service_unit", "forgeflow-runtime.service")
    if backup.run(["systemctl", "show", unit, "--property=ActiveState", "--value"]) != "inactive":
        raise RotationError("runtime_must_be_inactive")
    # Also verifies MainPID=0, an empty cgroup, no live managed PID, the stopped
    # Odoo state, its bind mount, and an immutable image ID.
    image = backup.assert_writers_stopped({
        "maintenance_units": [unit], "runtime_root": Path(config["sandbox_root"]),
        "odoo_container": config["container"],
        "odoo_state": Path(config["sandbox_root"]) / "odoo",
    })
    if image != record_for(config)["image_id"]:
        raise RotationError("old_image_binding_changed")


def summary(journal):
    result = {key: journal.get(key) for key in (
        "operation_id", "phase", "runtime_config", "old_generation", "new_generation",
        "old_sandbox", "new_sandbox", "old_container", "new_container", "last_error",
    )}
    result["backup_configuration_review_required"] = journal.get("phase") == "committed"
    return result


def rotate(config_path, *, journal_path=None, backup=None, provisioner=None):
    if os.geteuid() != 0:
        raise RotationError("rotation_requires_root")
    config_path = canonical(config_path)
    current_raw = trusted_json(config_path)
    current = configuration(current_raw)
    repo = canonical(current["repo_root"])
    backup = backup or load_script(repo, "vps_backup")
    provisioner = provisioner or load_script(repo, "vps_provision")
    with ExitStack() as locks:
        for name in ("workbench.lock", "heavy-operation.lock"):
            locks.enter_context(backup.lease(lifecycle_root(current) / name))
        if digest(trusted_json(config_path)) != digest(current_raw):
            raise RotationError("runtime_configuration_changed_before_lock")
        if journal_path is None:
            old_config, old_raw = current, current_raw
            assert_stopped(backup, old_config)
            old_record = record_for(old_config)
            operation = secrets.token_hex(16)
            new_root = canonical(Path(old_config["sandbox_root"]).parent / ("sandbox-" + operation))
            if new_root.exists():
                raise RotationError("new_sandbox_already_exists")
            new_config = {**old_raw, "sandbox_root": str(new_root),
                          "container": "forgeflow-odoo-" + operation[:16],
                          "odoo_image": old_record["image_id"], "allow_initialize": False}
            case_root = case_directory(old_config)
            old_lease_generation = EnvironmentLease(case_root, old_record["generation"]).stored_generation()
            if old_lease_generation not in (None, old_record["generation"]):
                raise RotationError("old_lease_generation_mismatch")
            directory = config_path.parent / "rotations"
            directory.mkdir(mode=0o700, exist_ok=True)
            canonical(directory)
            if directory.stat().st_uid != 0 or directory.stat().st_mode & 0o077:
                raise RotationError("private_rotation_directory_required")
            journal_path = directory / (operation + ".json")
            journal = {"schema": 1, "operation_id": operation, "phase": "planned",
                       "runtime_config": str(config_path), "old_config": old_raw,
                       "new_config": new_config, "old_generation": old_record["generation"],
                       "new_generation": None, "old_lease_generation": old_lease_generation,
                       "old_sandbox": old_config["sandbox_root"], "new_sandbox": str(new_root),
                       "old_container": old_config["container"], "new_container": new_config["container"],
                       "last_error": None}
            save(journal_path, journal)
        else:
            journal_path = canonical(journal_path)
            journal = trusted_json(journal_path)
            if journal.get("schema") != 1 or journal.get("runtime_config") != str(config_path):
                raise RotationError("rotation_journal_mismatch")
            old_raw, new_config = journal["old_config"], journal["new_config"]
            old_config = configuration(old_raw)
            if canonical(old_config["repo_root"]) != repo:
                raise RotationError("rotation_repository_changed")
            if digest(current_raw) not in (digest(old_raw), digest(new_config)):
                raise RotationError("runtime_configuration_changed")
            if journal["phase"] == "committed":
                if digest(current_raw) != digest(new_config):
                    raise RotationError("committed_runtime_configuration_changed")
                return journal_path, summary(journal)
            assert_stopped(backup, old_config)
            old_record = record_for(old_config)
            if old_record["generation"] != journal["old_generation"]:
                raise RotationError("old_environment_generation_changed")
            case_root = case_directory(old_config)
        try:
            # These are the OLD sandbox locks. The new provisioner acquires its
            # own distinct .provision.lock, so there is no nested lock deadlock.
            for path in (Path(old_config["sandbox_root"]) / ".provision.lock",
                         Path(old_config["sandbox_root"]) / "store/scope.lock",
                         Path(old_config["sandbox_root"]) / "odoo/.lock"):
                locks.enter_context(backup.lease(path))
            journal["phase"] = "provisioning"
            journal["last_error"] = None
            save(journal_path, journal)
            provisioner.provision({**new_config, "allow_initialize": True})
            new_record = record_for(new_config)
            generation = new_record["generation"]
            if generation == journal["old_generation"] or new_record["image_id"] != old_record["image_id"]:
                raise RotationError("new_generation_or_image_invalid")
            if journal["new_generation"] not in (None, generation):
                raise RotationError("new_environment_generation_changed")
            journal.update(phase="new_ready", new_generation=generation)
            save(journal_path, journal)
            # Recheck the operator-facing config before releasing any lease.
            current_raw = trusted_json(config_path)
            if digest(current_raw) not in (digest(old_raw), digest(new_config)):
                raise RotationError("runtime_configuration_changed")
            case_root = case_directory(old_config, create=True)
            lease = EnvironmentLease(case_root, generation)
            stored_generation = lease.stored_generation()
            if stored_generation == generation:
                if digest(current_raw) == digest(old_raw) and not lease.is_unclaimed():
                    raise RotationError("new_generation_already_claimed")
            elif digest(current_raw) == digest(new_config):
                raise RotationError("published_generation_lease_mismatch")
            else:
                lease.initialize_empty(previous_generation=journal["old_lease_generation"])
            journal["phase"] = "lease_switched"
            save(journal_path, journal)
            if digest(trusted_json(config_path)) not in (digest(old_raw), digest(new_config)):
                raise RotationError("runtime_configuration_changed_after_lease_switch")
            save(config_path, new_config)
            journal["phase"] = "committed"
            save(journal_path, journal)
            return journal_path, summary(journal)
        except Exception as error:
            # Record the interrupted stage. Never start either service or
            # restore/release a lease on failure; --status is always read-only.
            journal["last_error"] = {"type": type(error).__name__, "phase": journal["phase"]}
            save(journal_path, journal)
            raise RotationError("rotation_incomplete_inspect_journal:" + str(journal_path)) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/forgeflow/runtime.json"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--resume", type=Path, help="Explicitly resume a failed journal while offline")
    modes.add_argument("--status", type=Path, help="Read journal status; makes no changes")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise RotationError("rotation_requires_root")
        if args.status:
            result = summary(trusted_json(canonical(args.status)))
        else:
            journal, result = rotate(args.config, journal_path=args.resume)
            result["journal"] = str(journal)
        print(json.dumps(result, sort_keys=True))
    except (RotationError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
