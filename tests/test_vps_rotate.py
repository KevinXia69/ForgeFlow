"""Offline rotation fault tests; synthetic ERP records, no real services/Docker.

Run on Linux as root so file ownership and the real maintenance flocks are tested.
"""
from __future__ import annotations

import importlib.util
import json
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.product.lease import EnvironmentLease, EnvironmentLeaseError

SPEC = importlib.util.spec_from_file_location(
    "forgeflow_vps_rotate_test", Path(__file__).resolve().parents[1] / "scripts/vps_rotate.py")
rotation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rotation)
backup_module = rotation.load_script(Path(__file__).resolve().parents[1], "vps_backup")
pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="requires Linux root file ownership")

OLD = "a" * 32
NEW = "b" * 32
IMAGE = "sha256:" + "c" * 64
CASE = "d" * 32


def read(path):
    return json.loads(path.read_text())


def write_record(config, generation):
    sandbox = Path(config["sandbox_root"])
    (sandbox / "odoo").mkdir(parents=True, exist_ok=True)
    rotation.save(sandbox / "vps-instance.json", {
        "schema": 1, "phase": "ready", "generation": generation,
        "instance_id": "ff-" + generation, "container": config["container"],
        "volume": str(sandbox / "odoo"), "odoo_port": config["odoo_port"], "image_id": IMAGE,
    })


class SyntheticBackup:
    def __init__(self):
        self.active = "inactive"
        self.error = None
        self.locks = []
        self.checked = []
        self.commands = []

    @contextmanager
    def lease(self, path):
        self.locks.append(path)
        with backup_module.lease(path):
            yield

    def run(self, argv):
        self.commands.append(argv)
        assert argv == ["systemctl", "show", "forgeflow-runtime.service", "--property=ActiveState", "--value"]
        return self.active

    def assert_writers_stopped(self, config):
        self.checked.append(config)
        if self.error:
            raise self.error
        return IMAGE


class SyntheticProvisioner:
    def __init__(self):
        self.calls = []
        self.after_ready = None
        self.error = None
        self.generation = NEW

    def provision(self, config):
        self.calls.append(config)
        if self.error:
            raise self.error
        write_record(config, self.generation)
        if self.after_ready:
            self.after_ready(config)


@pytest.fixture
def env(tmp_path):
    sandbox = tmp_path / "old-sandbox"
    cases = tmp_path / "api/cases"
    cases.mkdir(parents=True, mode=0o700)
    repo = tmp_path / "repo"
    repo.mkdir()
    config = {
        "repo_root": str(repo), "python": "/usr/bin/python3", "sandbox_root": str(sandbox),
        "case_root": str(cases), "data_root": str(tmp_path / "api/runs"),
        "socket": str(tmp_path / "rpc/executor.sock"), "container": "old-odoo",
        "odoo_port": 18069, "api_port": 8000, "scenario": "A", "allow_initialize": False,
    }
    config_path = tmp_path / "runtime.json"
    rotation.save(config_path, config)
    write_record(config, OLD)
    old_file = sandbox / "odoo/previous-data"
    old_file.write_text("preserve the original ERP data")
    lease = EnvironmentLease(cases / "product-cases", OLD)
    lease.claim("reviewer-one", CASE)
    return SimpleNamespace(config=config, path=config_path, lease=lease, old_file=old_file,
                           backup=SyntheticBackup(), provisioner=SyntheticProvisioner())


def rotate(env, **kwargs):
    return rotation.rotate(env.path, backup=env.backup, provisioner=env.provisioner, **kwargs)


def assert_unchanged(env):
    assert read(env.path) == env.config
    env.lease.require("reviewer-one", CASE)
    assert env.old_file.read_text() == "preserve the original ERP data"


def journal_for(env):
    return next((env.path.parent / "rotations").glob("*.json"))


def test_rotation_preserves_old_data_and_requires_new_generation(env):
    journal, result = rotate(env)
    new = read(env.path)
    assert result["phase"] == "committed"
    assert result["backup_configuration_review_required"] is True
    assert new["container"] != env.config["container"]
    assert new["sandbox_root"] != env.config["sandbox_root"]
    assert new["case_root"] == env.config["case_root"]
    assert new["odoo_port"] == env.config["odoo_port"]
    assert new["odoo_image"] == IMAGE and new["allow_initialize"] is False
    assert env.provisioner.calls[0]["allow_initialize"] is True
    assert env.old_file.read_text() == "preserve the original ERP data"
    assert rotation.record_for(env.config)["generation"] == OLD
    current = EnvironmentLease(env.lease.root, NEW)
    assert current.is_unclaimed() and current.stored_generation() == NEW
    with pytest.raises(EnvironmentLeaseError, match="environment_generation_mismatch"):
        current.require("reviewer-one", CASE)
    assert [p.name for p in env.backup.locks] == [
        "workbench.lock", "heavy-operation.lock", ".provision.lock", "scope.lock", ".lock"]
    assert env.backup.checked[0]["odoo_container"] == "old-odoo"
    assert read(journal)["old_config"] == env.config
    assert os.stat(env.path).st_mode & 0o777 == 0o600
    assert not any("start" in command or "stop" in command for command in env.backup.commands)


@pytest.mark.parametrize("active", ["active", "activating", "failed"])
def test_runtime_must_be_strictly_inactive_before_any_mutation(env, active):
    env.backup.active = active
    with pytest.raises(rotation.RotationError, match="runtime_must_be_inactive"):
        rotate(env)
    assert not env.provisioner.calls
    assert not (env.path.parent / "rotations").exists()
    assert_unchanged(env)


def test_nonzero_pid_or_running_container_is_rejected_by_stopped_writer_check(env):
    env.backup.error = backup_module.BackupError("writer_service_main_pid_present")
    with pytest.raises(backup_module.BackupError, match="main_pid_present"):
        rotate(env)
    assert not env.provisioner.calls
    assert_unchanged(env)


def test_nonroot_cannot_rotate(env, monkeypatch):
    monkeypatch.setattr(rotation.os, "geteuid", lambda: 1000)
    with pytest.raises(rotation.RotationError, match="rotation_requires_root"):
        rotate(env)
    assert not env.backup.locks
    assert_unchanged(env)


def test_active_maintenance_lock_prevents_rotation(env):
    lock = Path(env.config["repo_root"]) / ".runtime/lifecycle/workbench.lock"
    with backup_module.lease(lock):
        with pytest.raises(backup_module.BackupError, match="already_active"):
            rotate(env)
    assert not env.provisioner.calls
    assert_unchanged(env)


def test_configuration_is_rechecked_after_acquiring_global_lock(env, monkeypatch):
    original = env.backup.lease

    @contextmanager
    def lease_with_changed_config(path):
        with original(path):
            if path.name == "workbench.lock":
                rotation.save(env.path, {**env.config, "api_port": 8001})
            yield

    monkeypatch.setattr(env.backup, "lease", lease_with_changed_config)
    with pytest.raises(rotation.RotationError, match="runtime_configuration_changed_before_lock"):
        rotate(env)
    assert not env.provisioner.calls
    env.lease.require("reviewer-one", CASE)


def test_failed_provision_keeps_lease_and_config_and_has_inspectable_journal(env):
    env.provisioner.error = RuntimeError("synthetic private diagnostic")
    with pytest.raises(rotation.RotationError, match="rotation_incomplete") as error:
        rotate(env)
    assert "private diagnostic" not in str(error.value)
    assert_unchanged(env)
    journal = read(journal_for(env))
    status = rotation.summary(journal)
    assert status["phase"] == "provisioning"
    assert status["last_error"] == {"type": "RuntimeError", "phase": "provisioning"}
    assert "reviewer-one" not in json.dumps(status)
    env.provisioner.error = None
    _, result = rotate(env, journal_path=journal_for(env))
    assert result["phase"] == "committed"


@pytest.mark.parametrize("fault", ["generation", "record", "config"])
def test_failed_ready_binding_or_changed_config_never_releases_old_lease(env, fault):
    if fault == "generation":
        env.provisioner.generation = OLD
    elif fault == "record":
        def damage(config):
            record = Path(config["sandbox_root"]) / "vps-instance.json"
            rotation.save(record, {**read(record), "phase": "failed"})
        env.provisioner.after_ready = damage
    else:
        env.provisioner.after_ready = lambda _: rotation.save(env.path, {**env.config, "api_port": 8001})
    with pytest.raises(rotation.RotationError, match="rotation_incomplete"):
        rotate(env)
    env.lease.require("reviewer-one", CASE)
    assert env.old_file.exists()
    assert read(journal_for(env))["phase"] != "committed"


def test_explicit_resume_finishes_after_lease_switched_but_config_write_failed(env, monkeypatch):
    save = rotation.save

    def fail_configuration(path, value):
        if path == env.path:
            raise OSError("synthetic disk failure")
        save(path, value)

    monkeypatch.setattr(rotation, "save", fail_configuration)
    with pytest.raises(rotation.RotationError, match="rotation_incomplete"):
        rotate(env)
    journal = journal_for(env)
    assert read(journal)["phase"] == "lease_switched"
    assert read(env.path) == env.config
    assert EnvironmentLease(env.lease.root, NEW).is_unclaimed()
    with pytest.raises(EnvironmentLeaseError, match="environment_generation_mismatch"):
        env.lease.claim("reviewer-one", CASE)
    monkeypatch.setattr(rotation, "save", save)
    _, result = rotate(env, journal_path=journal)
    assert result["phase"] == "committed"
    assert read(env.path)["sandbox_root"] == result["new_sandbox"]


def test_committed_resume_is_read_only_and_preserves_new_owner(env):
    journal, _ = rotate(env)
    new_lease = EnvironmentLease(env.lease.root, NEW)
    new_lease.claim("reviewer-two", "e" * 32)
    count = len(env.provisioner.calls)
    _, result = rotate(env, journal_path=journal)
    assert result["phase"] == "committed"
    assert len(env.provisioner.calls) == count
    new_lease.require("reviewer-two", "e" * 32)


def test_resume_after_config_publication_does_not_reset_a_new_owner(env, monkeypatch):
    save = rotation.save

    def crash_before_commit_receipt(path, value):
        if value.get("phase") == "committed":
            raise KeyboardInterrupt
        save(path, value)

    monkeypatch.setattr(rotation, "save", crash_before_commit_receipt)
    with pytest.raises(KeyboardInterrupt):
        rotate(env)
    journal = journal_for(env)
    assert read(journal)["phase"] == "lease_switched"
    assert read(env.path)["sandbox_root"] == read(journal)["new_sandbox"]
    new_lease = EnvironmentLease(env.lease.root, NEW)
    new_lease.claim("reviewer-two", "e" * 32)
    monkeypatch.setattr(rotation, "save", save)
    _, result = rotate(env, journal_path=journal)
    assert result["phase"] == "committed"
    new_lease.require("reviewer-two", "e" * 32)


def test_resume_refuses_claimed_unpublished_generation(env, monkeypatch):
    save = rotation.save

    def fail_configuration(path, value):
        if path == env.path:
            raise OSError("synthetic disk failure")
        save(path, value)

    monkeypatch.setattr(rotation, "save", fail_configuration)
    with pytest.raises(rotation.RotationError, match="rotation_incomplete"):
        rotate(env)
    new_lease = EnvironmentLease(env.lease.root, NEW)
    new_lease.claim("unexpected-owner", "e" * 32)
    monkeypatch.setattr(rotation, "save", save)
    with pytest.raises(rotation.RotationError, match="rotation_incomplete"):
        rotate(env, journal_path=journal_for(env))
    assert read(env.path) == env.config
    new_lease.require("unexpected-owner", "e" * 32)


def test_resume_refuses_external_configuration_change(env):
    env.provisioner.error = RuntimeError("interruption")
    with pytest.raises(rotation.RotationError, match="rotation_incomplete"):
        rotate(env)
    journal = journal_for(env)
    rotation.save(env.path, {**env.config, "api_port": 8001})
    env.provisioner.error = None
    with pytest.raises(rotation.RotationError, match="runtime_configuration_changed"):
        rotate(env, journal_path=journal)
    env.lease.require("reviewer-one", CASE)


def test_first_rotation_before_any_case_preserves_api_directory_ownership(env):
    for path in env.lease.root.iterdir():
        path.unlink()
    env.lease.root.rmdir()
    os.chown(Path(env.config["case_root"]), 12345, 12345)
    rotate(env)
    assert env.lease.root.stat().st_uid == 12345
    assert env.lease.path.stat().st_uid == 12345
    assert (env.lease.root / ".environment-lease.lock").stat().st_uid == 12345
    assert EnvironmentLease(env.lease.root, NEW).is_unclaimed()


def test_symlink_case_directory_is_rejected_before_provision(env):
    actual = env.lease.root.with_name("old-product-cases")
    env.lease.root.rename(actual)
    env.lease.root.symlink_to(actual, target_is_directory=True)
    with pytest.raises(rotation.RotationError, match="canonical_absolute_path_required"):
        rotate(env)
    assert not env.provisioner.calls
