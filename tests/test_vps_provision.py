"""Host persistence guards with fake Docker; never starts a real container."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl")

SPEC = importlib.util.spec_from_file_location(
    "vps_provision_test", Path(__file__).resolve().parents[1] / "scripts/vps_provision.py")
provisioner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provisioner)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "sandbox"
    root.mkdir()
    config = {"repo_root": str(tmp_path), "sandbox_root": str(root),
              "container": "odoo-test", "odoo_port": 18069, "socket": "/run/test.sock",
              "odoo_image": "test-image", "allow_initialize": True}
    calls = []
    record = {"schema": 1, "instance_id": "ff-" + "a" * 32, "generation": "b" * 32,
              "container": "odoo-test", "image_id": "sha256:test", "volume": str(root / "odoo"),
              "odoo_port": 18069, "phase": "created"}
    container = {"Id": "container-test", "Image": "sha256:test", "Config": {"Labels": {}},
                 "HostConfig": {"PortBindings": {"8069/tcp": [
                     {"HostIp": "127.0.0.1", "HostPort": "18069"}]},
                     "RestartPolicy": {"Name": "no"}, "NetworkMode": "bridge"},
                 "Mounts": [{"Destination": "/var/lib/forgeflow", "Source": str(root / "odoo"),
                             "Type": "bind", "RW": True}],
                 "State": {"Running": False, "StartedAt": "boot1"}, "RestartCount": 0}
    holder = {"container": container}

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        stdout, code = "", 0
        if args[:3] == ("docker", "image", "inspect"):
            stdout = json.dumps([{"Id": "sha256:test"}])
        elif args[:3] == ("docker", "container", "inspect"):
            if holder["container"] is None:
                code = 1
            else:
                current = provisioner.trusted_json(root / "vps-instance.json")
                container["Config"]["Labels"] = {
                    "io.forgeflow.instance": current["instance_id"],
                    "io.forgeflow.generation": current["generation"]}
                stdout = json.dumps([container])
        elif args[:2] == ("docker", "create"):
            holder["container"] = container
        elif args[:2] == ("docker", "start"):
            current = provisioner.trusted_json(root / "vps-instance.json")
            assert current["initialization_started"] is True
            container["State"]["Running"] = True
        return subprocess.CompletedProcess(args, code, stdout, "")

    workbench = SimpleNamespace(
        _valid_planner=lambda _path: True,
        _planner=lambda: write_json(root / "planner/provenance.json", {"files_sha256": {}}),
        _prepare_local_policy=lambda: write_json(root / "task-policy.json", {"policy": 1}),
        _write_request=lambda _path: write_json(root / "request.json", {"request": 1}))
    monkeypatch.setattr(provisioner, "run", run)
    monkeypatch.setattr(provisioner, "load_workbench", lambda *_args: workbench)

    def ready(*_args, **_kwargs):
        key = root / "odoo/api_key"
        key.write_text("synthetic-test-key")
        key.chmod(0o600)

    monkeypatch.setattr(provisioner, "wait_ready", ready)
    return SimpleNamespace(root=root, config=config, calls=calls, record=record,
                           container=container, holder=holder, workbench=workbench)


def invoke(sandbox):
    provisioner._locked_provision(sandbox.config, sandbox.root.parent, sandbox.root,
                                 "odoo-test", 18069)


def test_new_empty_instance_uses_one_shot_initialization_and_no_docker_restart(sandbox):
    sandbox.holder["container"] = None
    invoke(sandbox)
    create = next(args for args, _kwargs in sandbox.calls if args[:2] == ("docker", "create"))
    assert create[create.index("--restart") + 1] == "no"
    assert "FORGEFLOW_ALLOW_INITIALIZE=1" in create
    saved = provisioner.trusted_json(sandbox.root / "vps-instance.json")
    assert saved["initialization_started"] is True
    assert saved["phase"] == "ready"
    assert set(saved["bindings"]) == {"task-policy.json", "request.json", "planner/provenance.json"}


@pytest.mark.parametrize("leftover", ["store", "approvals", "api_key", "task-policy.json"])
def test_missing_host_record_with_any_business_residue_never_initializes(sandbox, leftover):
    (sandbox.root / leftover).touch()
    with pytest.raises(RuntimeError, match="explicit_new_instance_initialization_required"):
        invoke(sandbox)
    assert not (sandbox.root / "vps-instance.json").exists()
    assert len(sandbox.calls) == 1  # only image inspect


def test_ready_volume_loss_fails_before_any_container_start(sandbox):
    sandbox.record["phase"] = "ready"
    write_json(sandbox.root / "vps-instance.json", sandbox.record)
    with pytest.raises(RuntimeError, match="state_directory_invalid"):
        invoke(sandbox)
    assert len(sandbox.calls) == 1


def test_interrupted_first_boot_does_not_repeat_setup(sandbox):
    sandbox.record["initialization_started"] = True
    write_json(sandbox.root / "vps-instance.json", sandbox.record)
    (sandbox.root / "odoo").mkdir()
    with pytest.raises(RuntimeError, match="explicit_initialization_required"):
        invoke(sandbox)
    assert len(sandbox.calls) == 1


@pytest.mark.parametrize("mutation", ["restart", "public_port", "extra_port", "extra_bind", "privileged"])
def test_changed_container_security_settings_fail_without_silent_mutation(sandbox, mutation):
    write_json(sandbox.root / "vps-instance.json", sandbox.record)
    host = sandbox.container["HostConfig"]
    if mutation == "restart":
        host["RestartPolicy"]["Name"] = "unless-stopped"
    elif mutation == "public_port":
        host["PortBindings"]["8069/tcp"][0]["HostIp"] = "0.0.0.0"
    elif mutation == "extra_port":
        host["PortBindings"]["5432/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "5432"}]
    elif mutation == "extra_bind":
        sandbox.container["Mounts"].append({"Type": "bind", "Destination": "/var/run/docker.sock"})
    else:
        host["Privileged"] = True
    with pytest.raises(RuntimeError, match="odoo_container_binding_changed"):
        invoke(sandbox)
    assert not any(args[:2] in (("docker", "start"), ("docker", "update"))
                   for args, _kwargs in sandbox.calls)


def test_ready_artifacts_are_bound_before_container_inspection(sandbox, monkeypatch):
    from forgeflow import vps_odoo

    sandbox.record.update(phase="ready", bindings={})
    write_json(sandbox.root / "vps-instance.json", sandbox.record)
    monkeypatch.setattr(vps_odoo, "load_state", lambda *_args: ({}, False))
    sandbox.workbench._planner()
    sandbox.workbench._prepare_local_policy()
    sandbox.workbench._write_request(None)
    with pytest.raises(RuntimeError, match="vps_policy_or_planner_binding_changed"):
        invoke(sandbox)
    assert len(sandbox.calls) == 1


def test_invalid_existing_planner_is_not_rebuilt(sandbox, monkeypatch):
    from forgeflow import vps_odoo

    sandbox.record["phase"] = "ready"
    write_json(sandbox.root / "vps-instance.json", sandbox.record)
    monkeypatch.setattr(vps_odoo, "load_state", lambda *_args: ({}, False))
    sandbox.workbench._valid_planner = lambda _path: False
    sandbox.workbench._planner = lambda: pytest.fail("must not rebuild")
    with pytest.raises(RuntimeError, match="preserved_planner_invalid"):
        invoke(sandbox)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "writable", "oversize"])
def test_secret_and_record_reader_rejects_unsafe_files(tmp_path, kind):
    path = tmp_path / "file"
    path.write_bytes(b"synthetic")
    path.chmod(0o600)
    if kind == "symlink":
        target = tmp_path / "alias"
        target.symlink_to(path)
        path = target
    elif kind == "hardlink":
        os.link(path, tmp_path / "alias")
    elif kind == "writable":
        path.chmod(0o622)
    with pytest.raises(RuntimeError, match="unsafe_provision_file"):
        provisioner.trusted_bytes(path, 1 if kind == "oversize" else 1024)


def test_readiness_probe_is_bounded_to_five_seconds(monkeypatch):
    calls = []
    monkeypatch.setattr(provisioner, "inspect_container", lambda _name: {
        "State": {"Running": True, "StartedAt": "first"}, "RestartCount": 0})
    monkeypatch.setattr(provisioner, "run", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or subprocess.CompletedProcess(args, 0)))
    provisioner.wait_ready("test", initializing=False)
    assert calls[0][1]["timeout"] == 5


@pytest.mark.parametrize("state", [{"Running": False}, {"Running": True, "Restarting": True},
                                  {"Running": True, "OOMKilled": True}])
def test_readiness_fails_immediately_when_container_dies(monkeypatch, state):
    monkeypatch.setattr(provisioner, "inspect_container", lambda _name: {"State": state})
    monkeypatch.setattr(provisioner, "run", lambda *_args, **_kwargs: pytest.fail("no exec"))
    with pytest.raises(RuntimeError, match="odoo_stopped_or_restarting_before_ready"):
        provisioner.wait_ready("test", initializing=True)


def test_readiness_rejects_a_restart_during_setup(monkeypatch):
    rows = iter([{"State": {"Running": True, "StartedAt": name}, "RestartCount": 0}
                 for name in ("first", "second")])
    monkeypatch.setattr(provisioner, "inspect_container", lambda _name: next(rows))
    monkeypatch.setattr(provisioner.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(provisioner, "run", lambda *args, **_kwargs: subprocess.CompletedProcess(args, 1))
    with pytest.raises(RuntimeError, match="odoo_restarted_during_readiness"):
        provisioner.wait_ready("test", initializing=True)


def test_judge_container_creation_applies_fixed_cpu_and_memory_limits(sandbox):
    sandbox.config.update(odoo_memory_mb=1024, odoo_cpus=1)
    sandbox.container["HostConfig"].update(Memory=1024 ** 3, MemorySwap=1024 ** 3,
                                            NanoCpus=1_000_000_000)
    sandbox.holder["container"] = None
    invoke(sandbox)
    create = next(args for args, _kwargs in sandbox.calls if args[:2] == ("docker", "create"))
    assert create[create.index("--memory") + 1] == str(1024 ** 3)
    assert create[create.index("--memory-swap") + 1] == str(1024 ** 3)
    assert create[create.index("--cpus") + 1] == "1"


@pytest.mark.parametrize("property", ["Memory", "MemorySwap", "NanoCpus"])
def test_changed_resource_limits_fail_before_start_without_updating_container(sandbox, property):
    sandbox.config.update(odoo_memory_mb=1024, odoo_cpus=1)
    sandbox.container["HostConfig"].update(Memory=1024 ** 3, MemorySwap=1024 ** 3,
                                            NanoCpus=1_000_000_000)
    sandbox.container["HostConfig"][property] = 0
    write_json(sandbox.root / "vps-instance.json", sandbox.record)
    with pytest.raises(RuntimeError, match="odoo_container_binding_changed"):
        invoke(sandbox)
    assert not any(args[:2] in (("docker", "start"), ("docker", "update"))
                   for args, _kwargs in sandbox.calls)


@pytest.mark.parametrize("limits", [
    {"odoo_memory_mb": True}, {"odoo_memory_mb": -1}, {"odoo_memory_mb": 1},
    {"odoo_cpus": True}, {"odoo_cpus": float("nan")}, {"odoo_cpus": -1},
])
def test_invalid_resource_budgets_are_rejected_before_docker_mutation(sandbox, limits):
    sandbox.config.update(limits)
    with pytest.raises(RuntimeError, match="invalid_odoo_"):
        invoke(sandbox)
    assert not sandbox.calls
