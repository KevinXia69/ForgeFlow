#!/usr/bin/env python3
"""Prepare one persistent VPS sandbox without starting the public API.

Only an explicitly enabled empty instance may initialize. Subsequent invocations
validate the same image, volume, generation and policy and preserve ERP data.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import math
import os
import re
import stat
import subprocess
import time
import uuid
from pathlib import Path


def run(*args, check=True, timeout=30):
    try:
        result = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("provision_command_timeout") from None
    if check and result.returncode:
        raise RuntimeError("provision_command_failed:" + args[0])
    return result


def save(path, value):
    if path.is_symlink():
        raise RuntimeError("unsafe_provision_record")
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary.unlink(missing_ok=True)


def trusted_bytes(path, limit=1024 * 1024):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or info.st_mode & 0o022 or info.st_size > limit):
                raise RuntimeError("unsafe_provision_file")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(limit + 1)
            if len(data) != info.st_size:
                raise RuntimeError("unsafe_provision_file")
            return data
        finally:
            os.close(fd)
    except OSError:
        raise RuntimeError("unsafe_provision_file") from None


def trusted_json(path):
    try:
        value = json.loads(trusted_bytes(path))
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("invalid_provision_record") from None
    if not isinstance(value, dict):
        raise RuntimeError("invalid_provision_record")
    return value


def artifact_bindings(sandbox):
    from forgeflow.recovery.storage import digest

    return {name: digest(trusted_json(sandbox / name))
            for name in ("task-policy.json", "request.json", "planner/provenance.json")}


def require_preserved_artifacts(record, sandbox, workbench):
    if not workbench._valid_planner(sandbox / "planner"):
        raise RuntimeError("preserved_planner_invalid")
    if record.get("bindings") != artifact_bindings(sandbox):
        raise RuntimeError("vps_policy_or_planner_binding_changed")


def inspect_container(container):
    result = run("docker", "container", "inspect", container, check=False)
    if result.returncode:
        # An unavailable Docker daemon or an uninspectable existing container is
        # not proof that it is safe to create a new one.
        listed = run("docker", "ps", "-aq", "--filter", "name=^/" + re.escape(container) + "$")
        if listed.stdout.strip():
            raise RuntimeError("existing_container_uninspectable")
        return None
    try:
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError
        return rows[0]
    except (ValueError, TypeError):
        raise RuntimeError("invalid_container_inspection") from None


def wait_ready(container, *, initializing):
    from forgeflow import local_entrypoint

    deadline = time.monotonic() + (1800 if initializing else 120)
    boot = None
    while time.monotonic() < deadline:
        info = inspect_container(container)
        state = (info or {}).get("State", {})
        if (state.get("Running") is not True or state.get("Restarting") is True
                or state.get("OOMKilled") is True):
            raise RuntimeError("odoo_stopped_or_restarting_before_ready")
        current_boot = (state.get("StartedAt"), info.get("RestartCount"))
        if boot is None:
            boot = current_boot
        elif current_boot != boot:
            raise RuntimeError("odoo_restarted_during_readiness")
        ready = run("docker", "exec", container, "sh", "-c", local_entrypoint.READY_CHECK,
                    check=False, timeout=5)
        if ready.returncode == 0:
            return
        time.sleep(2)
    raise RuntimeError("odoo_readiness_timeout")


def load_workbench(repo, config):
    os.environ.update({
        "FORGEFLOW_LOCAL_SCENARIO": "A",
        "FORGEFLOW_LOCAL_SANDBOX_ROOT": config["sandbox_root"],
        "FORGEFLOW_LOCAL_CONTAINER_NAME": config["container"],
        "FORGEFLOW_LOCAL_ODOO_PORT": str(config["odoo_port"]),
        "FORGEFLOW_LOCAL_SOCKET_PATH": config["socket"],
    })
    spec = importlib.util.spec_from_file_location("forgeflow_vps_workbench", repo / "scripts/local_workbench.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resource_limits(config):
    """Optional immutable Docker budgets; zero preserves the original runtime."""
    memory = config.get("odoo_memory_mb", 0)
    cpus = config.get("odoo_cpus", 0)
    if type(memory) is not int or memory < 0 or memory > 1024 * 1024 or (memory and memory < 256):
        raise RuntimeError("invalid_odoo_memory_limit")
    if (type(cpus) not in (int, float) or not math.isfinite(cpus)
            or cpus < 0 or cpus > 64 or (cpus and cpus < 0.1)):
        raise RuntimeError("invalid_odoo_cpu_limit")
    return {"Memory": memory * 1024 * 1024, "NanoCpus": round(cpus * 1_000_000_000)}


def provision(config):
    if os.geteuid() != 0:
        raise RuntimeError("provision_requires_root")
    if config.get("scenario", "A") != "A":
        raise RuntimeError("vps_image_only_supports_scenario_A")
    repo, sandbox = Path(config["repo_root"]), Path(config["sandbox_root"])
    if (not repo.is_absolute() or not sandbox.is_absolute() or sandbox.is_symlink()
            or sandbox.resolve() != sandbox.absolute()):
        raise RuntimeError("invalid_runtime_paths")
    container = config["container"]
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}", container) is None:
        raise RuntimeError("invalid_container_name")
    port = config["odoo_port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise RuntimeError("invalid_odoo_port")
    sandbox.mkdir(parents=True, mode=0o700, exist_ok=True)
    sandbox.chmod(0o700)
    lock = os.open(sandbox / ".provision.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("unsafe_provision_lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _locked_provision(config, repo, sandbox, container, port)
    finally:
        os.close(lock)


def _locked_provision(config, repo, sandbox, container, port):
    limits = resource_limits(config)
    record_path = sandbox / "vps-instance.json"
    image_name = config.get("odoo_image", "forgeflow-odoo:vps1")
    image_id = json.loads(run("docker", "image", "inspect", image_name).stdout)[0]["Id"]
    volume = sandbox / "odoo"
    if volume.is_symlink():
        raise RuntimeError("unsafe_odoo_volume")
    if record_path.exists() or record_path.is_symlink():
        record = trusted_json(record_path)
        if (record.get("schema") != 1 or record.get("container") != container
                or record.get("image_id") != image_id or record.get("odoo_port") != port
                or record.get("volume") != str(volume)
                or record.get("phase") not in {"created", "ready"}
                or not isinstance(record.get("instance_id"), str)
                or re.fullmatch(r"ff-[a-f0-9]{32}", record["instance_id"]) is None
                or not isinstance(record.get("generation"), str)
                or re.fullmatch(r"[a-f0-9]{32}", record["generation"]) is None):
            raise RuntimeError("vps_instance_binding_changed")
        if record.get("phase") == "ready" or record.get("initialization_started") is True:
            # Validate the volume before Docker can execute its initialization
            # entrypoint, even if that container retained its first-boot flag.
            from forgeflow.vps_odoo import expected_binding, load_state

            load_state(volume, expected_binding(record["instance_id"], record["generation"]), False)
    else:
        leftovers = [item for item in sandbox.iterdir()
                     if item.name not in {".provision.lock", "odoo"}]
        if (config.get("allow_initialize") is not True or leftovers
                or (volume.exists() and any(volume.iterdir()))):
            raise RuntimeError("explicit_new_instance_initialization_required")
        record = {
            "schema": 1, "instance_id": "ff-" + uuid.uuid4().hex,
            "generation": uuid.uuid4().hex, "container": container,
            "image_id": image_id, "volume": str(volume), "odoo_port": port,
            "phase": "created",
        }
        save(record_path, record)
    volume.mkdir(mode=0o700, exist_ok=True)
    workbench = load_workbench(repo, config)
    if record["phase"] == "ready":
        require_preserved_artifacts(record, sandbox, workbench)
    info = inspect_container(container)
    if info is None:
        args = ["docker", "create", "--name", container, "--restart", "no",
                "--stop-timeout", "120", "--mount", f"type=bind,src={volume},dst=/var/lib/forgeflow",
                "--label", "io.forgeflow.instance=" + record["instance_id"],
                "--label", "io.forgeflow.generation=" + record["generation"],
                "-e", "FORGEFLOW_INSTANCE_ID=" + record["instance_id"],
                "-e", "FORGEFLOW_GENERATION=" + record["generation"],
                "-p", f"127.0.0.1:{port}:8069"]
        if limits["Memory"]:
            args += ["--memory", str(limits["Memory"]), "--memory-swap", str(limits["Memory"])]
        if limits["NanoCpus"]:
            args += ["--cpus", format(limits["NanoCpus"] / 1_000_000_000, ".9g")]
        if record["phase"] == "created":
            args += ["-e", "FORGEFLOW_ALLOW_INITIALIZE=1"]
        run(*args, image_id)
        info = inspect_container(container)
        if info is None:
            raise RuntimeError("created_container_missing")
    labels = info["Config"].get("Labels") or {}
    ports = info["HostConfig"].get("PortBindings") or {}
    volumes = [row for row in info["Mounts"] if row["Destination"] == "/var/lib/forgeflow"]
    if (info["Image"] != image_id or labels.get("io.forgeflow.instance") != record["instance_id"]
            or labels.get("io.forgeflow.generation") != record["generation"]
            or ports != {"8069/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]}
            or len(volumes) != 1 or volumes[0]["Source"] != str(volume) or volumes[0]["Type"] != "bind"
            or volumes[0].get("RW") is not True
            or any(row.get("Type") == "bind" and row.get("Destination") != "/var/lib/forgeflow"
                   for row in info["Mounts"])
            or info["HostConfig"].get("Privileged") is True
            or info["HostConfig"].get("NetworkMode") == "host"
            or info["HostConfig"].get("Memory", 0) != limits["Memory"]
            or info["HostConfig"].get("NanoCpus", 0) != limits["NanoCpus"]
            or (limits["Memory"] and info["HostConfig"].get("MemorySwap") != limits["Memory"])
            or (info["HostConfig"].get("RestartPolicy") or {}).get("Name") != "no"):
        raise RuntimeError("odoo_container_binding_changed")
    if not info["State"]["Running"]:
        if record["phase"] == "created" and not record.get("initialization_started"):
            # Persist the one-shot startup intent before Docker can run setup.
            # If this invocation is interrupted, the next one requires a fully
            # ready volume; it never tries initialization a second time.
            record.update(initialization_started=True, container_id=info["Id"])
            save(record_path, record)
        run("docker", "start", container)
    wait_ready(container, initializing=record["phase"] == "created")
    key = sandbox / "api_key"
    persisted_key = volume / "api_key"
    data = trusted_bytes(persisted_key, 4096)
    if not 1 <= len(data) <= 4096:
        raise RuntimeError("invalid_persisted_odoo_key")
    if key.exists():
        if trusted_bytes(key, 4096) != data:
            raise RuntimeError("executor_odoo_key_changed")
    else:
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    os.chmod(key, 0o400)
    workbench._planner()
    policy, request = sandbox / "task-policy.json", sandbox / "request.json"
    if not policy.exists():
        if record["phase"] == "ready":
            raise RuntimeError("preserved_policy_missing")
        workbench._prepare_local_policy()
    if not request.exists():
        if record["phase"] == "ready":
            raise RuntimeError("preserved_request_missing")
        workbench._write_request(policy)
    bindings = artifact_bindings(sandbox)
    if record.get("phase") == "ready" and record.get("bindings") != bindings:
        raise RuntimeError("vps_policy_binding_changed")
    for name in ("store", "approvals"):
        directory = sandbox / name
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise RuntimeError("executor_store_symlink")
        os.chmod(directory, 0o700)
    record.update(phase="ready", bindings=bindings, container_id=info["Id"])
    save(record_path, record)
    print("ForgeFlow persistent sandbox ready", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    provision(trusted_json(args.config))


if __name__ == "__main__":
    main()
