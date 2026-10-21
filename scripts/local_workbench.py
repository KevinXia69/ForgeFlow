#!/usr/bin/env python3
"""One-command local ForgeFlow workbench.

Starts a configured Odoo A/B sandbox, host root executor and local API against an
already-built task image. The image is intentionally not rebuilt here: the
task image includes Odoo/Postgres and is large; build it once per host (see
docs/LOCAL_WORKBENCH.md). Every other step is automated.

Usage:
  uv run python scripts/local_workbench.py start       # initialize only if new
  uv run python scripts/local_workbench.py resume      # existing instance only
  uv run python scripts/local_workbench.py stop        # preserve data
  uv run python scripts/local_workbench.py doctor      # read-only
  uv run python scripts/local_workbench.py restart-api
  uv run python scripts/local_workbench.py delete --confirm-container-id <full-id>

up/down are preserving start/stop aliases. Reset is an explicit delete followed
by init in NEW sandbox/case roots; retained execution evidence is never erased.
Unlabelled legacy instances are diagnostic-only, never silently adopted.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import itertools
import json
import os
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path, PurePosixPath

from forgeflow import local_entrypoint
from forgeflow import runtime_lifecycle as lifecycle
from forgeflow.planning.objectives import LEGACY_CANDIDATE_PROFILE, profile_objectives

ROOT = Path(__file__).resolve().parent.parent
SCENARIO = os.environ.get("FORGEFLOW_LOCAL_SCENARIO", "A").upper()
SCENARIO_CONFIG = {
    "A": {"task": "2280_medium_repair_plan_medium", "image": "forgeflow-planner-live:a-20261020",
          "policy": "2280_medium_repair_plan_medium.json", "port": 8000, "odoo_port": 18069,
          "deadlines": (8, 8, 9, 10, 10)},
    "B": {"task": "2299_hard_repair_plan_hard", "image": "forgeflow-planner-live:b-20261020",
          "policy": "2299-workcenter-outage-v1.json", "port": 8010, "odoo_port": 18070,
          "deadlines": (8, 8, 9, 9, 9, 10, 10, 11, 11, 11, 12, 12)},
}
if SCENARIO not in SCENARIO_CONFIG:
    raise SystemExit("FORGEFLOW_LOCAL_SCENARIO must be A or B")
CONFIG = SCENARIO_CONFIG[SCENARIO]
SUFFIX = "" if SCENARIO == "A" else "-b"
TASK = CONFIG["task"]
SANDBOX = Path(os.environ.get("FORGEFLOW_LOCAL_SANDBOX_ROOT", str(ROOT / ".runtime" / f"local-sandbox{SUFFIX}")))
CONTAINER = os.environ.get("FORGEFLOW_LOCAL_CONTAINER_NAME", f"ff-local-workbench{SUFFIX}")
PORT = int(os.environ.get("FORGEFLOW_LOCAL_API_PORT", str(CONFIG["port"])))
ODOO_PORT = int(os.environ.get("FORGEFLOW_LOCAL_ODOO_PORT", str(CONFIG["odoo_port"])))
SOCKET = os.environ.get("FORGEFLOW_LOCAL_SOCKET_PATH", f"/tmp/ff-local{SUFFIX}/executor.sock")
IMAGE = os.environ.get("FORGEFLOW_LOCAL_IMAGE", CONFIG["image"])
POLICY = Path(os.environ.get("FORGEFLOW_LOCAL_POLICY", str(ROOT / "contracts" / CONFIG["policy"])))
RUNTIME_POLICY = SANDBOX / "task-policy.json"
INVITE = "demo-invite-code-0001"


def _docker_env() -> dict[str, str]:
    env = dict(os.environ)
    if "DOCKER_HOST" not in env:
        candidate = Path.home() / ".colima" / "forgeflow" / "docker.sock"
        if candidate.exists():
            env["DOCKER_HOST"] = f"unix://{candidate}"
    config = ROOT / ".runtime" / "docker-config"
    if config.is_dir():
        env.setdefault("DOCKER_CONFIG", str(config))
    return env


DOCKER_ENV = _docker_env()


def _run(argv: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, cwd=ROOT, env=DOCKER_ENV, check=check,
            text=True, capture_output=capture, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise lifecycle.LifecycleError("lifecycle_command_timeout") from None


def _docker(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return _run(["docker", *args], check=check, capture=capture)


def _pid_file(name: str) -> Path:
    return SANDBOX / f"{name}.pid"


def _read_pid(name: str) -> int | None:
    record, state = lifecycle.managed_process(_pid_file(name))
    if state == "pid_reused":
        raise lifecycle.LifecycleError("pid_reused_refusing_operation")
    return record["identity"]["pid"] if state == "running" else None


def _stop_pid(name: str) -> None:
    lifecycle.stop_process(_pid_file(name))


def _case_root() -> Path:
    return Path(os.environ.get("FORGEFLOW_LOCAL_CASE_ROOT", str(ROOT / ".runtime" / f"demo-approvals{SUFFIX}")))


def _config() -> dict:
    return {"scenario": SCENARIO, "task": TASK, "container": CONTAINER,
            "image": IMAGE, "api_port": PORT, "odoo_port": ODOO_PORT,
            "socket": str(Path(SOCKET).absolute()), "cases": str(_case_root().absolute())}


def _project_id() -> str:
    return hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest()


def _instance_path() -> Path:
    return SANDBOX / "lifecycle.json"


def _instance(required: bool = True) -> dict | None:
    record = lifecycle.read_record(_instance_path())
    if record is None:
        if required:
            raise lifecycle.LifecycleError("unmanaged_sandbox_use_doctor")
        return None
    if (record.get("schema_version") != 1 or record.get("project") != _project_id()
            or record.get("uid") != os.getuid() or record.get("config") != _config()
            or not isinstance(record.get("instance"), str) or len(record["instance"]) != 32):
        raise lifecycle.LifecycleError("lifecycle_configuration_mismatch")
    return record


def _saved_features(record: dict) -> dict[str, bool]:
    """Validate old bindings without rewriting or silently upgrading them."""
    if "features" in record and "acceptance" in record:
        raise lifecycle.LifecycleError("ambiguous_revision_binding")
    saved = record.get("features", record.get("acceptance", {"revisions": False}))
    allowed = {"revisions", "candidates"} if "features" in record else {"revisions"}
    if (not isinstance(saved, dict) or "revisions" not in saved or not set(saved) <= allowed
            or type(saved["revisions"]) is not bool):
        raise lifecycle.LifecycleError("invalid_revision_binding")
    if "candidates" in saved and type(saved["candidates"]) is not bool:
        raise lifecycle.LifecycleError("invalid_candidate_binding")
    return {"revisions": saved["revisions"], "candidates": saved.get("candidates", False)}


def _revision_capability(record: dict | None, *, new_instance: bool = True) -> bool:
    """New managed instances enable revisions; existing instances retain their choice."""
    from forgeflow.product.local import revision_capability_setting  # noqa: PLC0415

    try:
        requested = revision_capability_setting()
    except ValueError as error:
        raise lifecycle.LifecycleError(str(error)) from None
    if record is None:
        return new_instance if requested is None else requested
    saved = _saved_features(record)
    if requested is not None and requested != saved["revisions"]:
        raise lifecycle.LifecycleError("revision_configuration_changed_use_fresh_sandbox")
    return saved["revisions"]


def _candidate_capability(record: dict | None) -> bool:
    """Candidates require an explicit initial choice, then retain it on resume."""
    from forgeflow.product.local import candidate_capability_setting  # noqa: PLC0415

    try:
        requested = candidate_capability_setting()
    except ValueError as error:
        raise lifecycle.LifecycleError(str(error)) from None
    if record is None:
        return requested is True
    saved = _saved_features(record)["candidates"]
    if requested is not None and requested != saved:
        raise lifecycle.LifecycleError("candidate_configuration_changed_use_fresh_sandbox")
    return saved


def _candidate_profile(record: dict | None, enabled: bool) -> str:
    """Profiles are immutable instance choices; missing legacy metadata stays v1."""
    from forgeflow.product.local import candidate_profile_setting  # noqa: PLC0415

    try:
        requested = candidate_profile_setting()
        saved = record.get("candidate_profile", LEGACY_CANDIDATE_PROFILE) if record is not None else None
        if saved is not None:
            profile_objectives(saved)
    except ValueError as error:
        raise lifecycle.LifecycleError(str(error)) from None
    if record is not None and requested is not None and requested != saved:
        raise lifecycle.LifecycleError("candidate_profile_changed_use_fresh_sandbox")
    profile = saved if record is not None else (requested or LEGACY_CANDIDATE_PROFILE)
    if not enabled and profile != LEGACY_CANDIDATE_PROFILE:
        raise lifecycle.LifecycleError("candidate_profile_requires_candidates")
    return profile


def _require_dedicated_revision_sandbox() -> None:
    # The legacy acceptance workflow still requires explicit dedicated destinations.
    # Normal new managed instances use the validated product capability by default.
    required = ("FORGEFLOW_LOCAL_SANDBOX_ROOT", "FORGEFLOW_LOCAL_CASE_ROOT",
                "FORGEFLOW_LOCAL_CONTAINER_NAME", "FORGEFLOW_LOCAL_SOCKET_PATH")
    ordinary_paths = {str((ROOT / ".runtime" / name).resolve()) for name in (
        "local-sandbox", "local-sandbox-b", "demo-approvals", "demo-approvals-b",
    )}
    if (not all(os.environ.get(key, "").strip() for key in required)
            or str(SANDBOX.resolve()) in ordinary_paths
            or str(_case_root().resolve()) in ordinary_paths
            or CONTAINER in {"ff-local-workbench", "ff-local-workbench-b"}
            or str(Path(SOCKET).resolve()) in {
                str(Path(path).resolve()) for path in (
                    "/tmp/ff-local/executor.sock", "/tmp/ff-local-b/executor.sock")
            }):
        raise lifecycle.LifecycleError("revision_acceptance_requires_dedicated_sandbox")


def _check_revision_bundle(enabled: bool) -> None:
    if not enabled:
        return
    from forgeflow.recovery.service import RecoveryError, verify_sources  # noqa: PLC0415

    planner = SANDBOX / "planner"
    try:
        if not _valid_planner(planner):
            raise ValueError("invalid bundle")
        sources = verify_sources(planner)
        if not {"forgeflow/recovery/product_versions.py", "forgeflow/recovery/product_constraints.py"} <= (
            sources["files_sha256"].keys()
        ):
            raise ValueError("missing revision modules")
    except (OSError, ValueError, TypeError, AttributeError, RecoveryError):
        raise lifecycle.LifecycleError("revision_bound_bundle_unsupported") from None


def _check_candidate_bundle(enabled: bool, profile: str = LEGACY_CANDIDATE_PROFILE) -> None:
    if not enabled:
        return
    from forgeflow.recovery.service import RecoveryError, verify_sources  # noqa: PLC0415

    planner = SANDBOX / "planner"
    required = {f"forgeflow/recovery/{name}.py" for name in (
        "product_versions", "product_constraints", "product_costs", "product_candidates")}
    required.add("forgeflow/planning/objectives.py")
    try:
        profile_objectives(profile)
        if not _valid_planner(planner):
            raise ValueError("invalid bundle")
        sources = verify_sources(planner)
        if not required <= sources["files_sha256"].keys():
            raise ValueError("missing candidate modules")
        profiles = sources.get("product_candidate_profiles", [LEGACY_CANDIDATE_PROFILE])
        if type(profiles) is not list or profile not in profiles:
            raise ValueError("unsupported candidate profile")
    except (OSError, ValueError, TypeError, AttributeError, RecoveryError):
        raise lifecycle.LifecycleError("candidate_bound_bundle_unsupported") from None


_CONTAINER_FORMAT = (
    '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
    '"state":{{json .State.Status}},"auto_remove":{{json .HostConfig.AutoRemove}},'
    '"mounts":{{json .Mounts}},"ports":{{json .HostConfig.PortBindings}},'
    '"instance":{{json (index .Config.Labels "io.forgeflow.instance")}},'
    '"project":{{json (index .Config.Labels "io.forgeflow.project")}},'
    '"task":{{json (index .Config.Labels "io.forgeflow.task")}}}'
)


def _container_info() -> dict | None:
    # Explicit fields exclude Docker environment variables and command arguments.
    result = _docker("container", "inspect", "--format", _CONTAINER_FORMAT,
                     CONTAINER, check=False, capture=True)
    if result.returncode:
        listing = _docker("ps", "-a", "--filter", f"name=^/{CONTAINER}$",
                          "--format", "{{.ID}}", check=False, capture=True)
        if listing.returncode:
            raise lifecycle.LifecycleError("docker_unavailable")
        if not listing.stdout.strip():
            return None
        raise lifecycle.LifecycleError("container_inspection_failed")
    try:
        value = json.loads(result.stdout)
        if (not isinstance(value, dict) or not isinstance(value.get("id"), str)
                or not value["id"] or not isinstance(value.get("mounts"), list)):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise lifecycle.LifecycleError("container_inspection_failed") from None


def _mount_digest(info: dict) -> str:
    # Docker's Mounts is an unordered set. Bind every field, including Source,
    # while making an inspect response's list order immaterial.
    mounts = info["mounts"]
    if (not isinstance(mounts, list) or len(mounts) > 128
            or any(not isinstance(m, dict) or not isinstance(m.get("Destination"), str)
                   or not m["Destination"].startswith("/") for m in mounts)
            or len({m["Destination"] for m in mounts}) != len(mounts)):
        raise lifecycle.LifecycleError("container_mounts_invalid")
    ordered = sorted(mounts, key=lambda m: json.dumps(m, sort_keys=True, separators=(",", ":")))
    raw = json.dumps(ordered, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _mounts_match(info: dict, record: dict) -> bool:
    canonical = _mount_digest(info)
    version = record.get("mount_digest_version", 1)
    if type(version) is not int:
        return False
    if version == 2:
        return canonical == record.get("mounts_sha256")
    if version != 1:
        return False
    # Early v10 records hashed the inspect order. Prove equality against the
    # entire previously bound set; never accept just destinations or names.
    mounts = info["mounts"]
    def digest(items):
        return hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if digest(mounts) == record.get("mounts_sha256"):
        return True
    if len(mounts) > 6:
        return False  # Bounded legacy migration; ambiguous larger sets stay refused.
    return any(digest(order) == record.get("mounts_sha256") for order in itertools.permutations(mounts))


def _check_container(info: dict | None, record: dict) -> dict:
    if info is None:
        raise lifecycle.LifecycleError("preserved_container_missing")
    if (info.get("instance") != record["instance"] or info.get("project") != record["project"]
            or info.get("task") != TASK or info.get("name") != "/" + CONTAINER):
        raise lifecycle.LifecycleError("container_not_owned")
    if info.get("auto_remove") is not False:
        raise lifecycle.LifecycleError("auto_remove_refusing_preserving_operation")
    expected_ports = {"8069/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(ODOO_PORT)}]}
    if info.get("ports") != expected_ports:
        raise lifecycle.LifecycleError("container_ports_changed")
    if record.get("container_id") and (
            info["id"] != record["container_id"] or info.get("image") != record.get("image_id")
            or not _mounts_match(info, record)):
        raise lifecycle.LifecycleError("container_identity_or_mounts_changed")
    return info


def _save_instance(record: dict, phase: str) -> None:
    record["phase"] = phase
    measurements = record.setdefault("resources", [])
    processes = {}
    for role in ("api", "executor"):
        process, state = lifecycle.managed_process(_pid_file(role))
        processes[role] = {"state": state, "rss_bytes": (
            lifecycle.process_rss(process["identity"]) if state == "running" else None)}
    measurements.append({"phase": phase, "processes": processes,
                         **lifecycle.resource_snapshot(SANDBOX)})
    record["resources"] = measurements[-24:]
    lifecycle.write_record(_instance_path(), record)


def _scope_lease():
    # The executor uses this same nonblocking scope lock through verifier/readback.
    # Hold it across stop so no new operation can start after the busy check.
    return lifecycle.exclusive_lease(SANDBOX / "store" / "scope.lock")


def _require_image() -> None:
    found = _docker("image", "inspect", IMAGE, check=False, capture=True)
    if found.returncode != 0:
        raise SystemExit(
            f"Missing image {IMAGE}. Build it once with the task Dockerfile "
            "(see docs/LOCAL_WORKBENCH.md) or set FORGEFLOW_LOCAL_IMAGE."
        )


def _wait_sandbox() -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        probe = _docker("exec", CONTAINER, "sh", "-c", local_entrypoint.READY_CHECK,
                        check=False, capture=True)
        if probe.returncode == 0:
            database = _docker("exec", "--user", "postgres", CONTAINER, "psql", "-d", "bench",
                               "-At", "-c", "SELECT 1;", check=False, capture=True)
            if database.returncode == 0 and database.stdout.strip() == "1":
                return
        time.sleep(2)
    raise lifecycle.LifecycleError("sandbox_setup_timeout_preserved")


def _wait_erp() -> None:
    """A retained setup marker is not proof that a restarted ERP can serve reads."""
    code = """
import sys
from pathlib import Path
import odoolib
from forgeflow.executor.main import read_key_text
try:
    connection = odoolib.get_connection(hostname='127.0.0.1', protocol='json2',
        port=int(sys.argv[1]), database='bench', password=read_key_text(Path(sys.argv[2])))
    count = connection.get_model('sale.order').search_count(domain=[])
    assert type(count) is int and count > 0
except Exception:
    sys.exit(1)
"""
    deadline = time.monotonic() + 180
    environment = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "HOME", "LANG", "TMPDIR", "PYTHONPATH"}}
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            probe = subprocess.run(
                [sys.executable, "-c", code, str(ODOO_PORT), str(SANDBOX / "api_key")],
                cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=min(5, remaining), check=False,
            )
            if probe.returncode == 0:
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise lifecycle.LifecycleError("erp_readiness_unconfirmed_preserved")


def _preserving_entrypoint(record: dict, info: dict) -> None:
    """Repair only an owned, stopped container; bind and recheck exact source."""
    configured = _docker("inspect", "--format", "{{json .Config.Entrypoint}}",
                         info["id"], capture=True)
    if json.loads(configured.stdout) != [local_entrypoint.PATH]:
        raise lifecycle.LifecycleError("local_entrypoint_unrecognized")
    with tempfile.TemporaryDirectory(prefix="ff-local-entrypoint-") as temporary:
        source = Path(temporary) / "entrypoint.sh"

        def read_source():
            _docker("cp", f"{info['id']}:{local_entrypoint.PATH}", str(source), capture=True)
            metadata = source.lstat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_size > 65536):
                raise lifecycle.LifecycleError("local_entrypoint_unrecognized")
            return source.read_bytes()

        current = read_source()
        patched, binding = local_entrypoint.preserving_source(current)
        if record.get("entrypoint") not in (None, binding):
            raise lifecycle.LifecycleError("local_entrypoint_binding_changed")
        if record.get("entrypoint") is not None and current != patched:
            raise lifecycle.LifecycleError("local_entrypoint_binding_changed")
        if current != patched:
            if info["state"] == "running":
                raise lifecycle.LifecycleError("local_entrypoint_stop_before_upgrade")
            # The configured source may contain upstream local credentials. It
            # stays inside this private temporary directory and is never logged.
            source.write_bytes(patched)
            source.chmod(0o700)
            _check_container(_container_info(), record)
            _docker("cp", str(source), f"{info['id']}:{local_entrypoint.PATH}", capture=True)
            source.unlink()
            if read_source() != patched:
                raise lifecycle.LifecycleError("local_entrypoint_install_unconfirmed")
        if record.get("entrypoint") != binding:
            record["entrypoint"] = binding
            _save_instance(record, "preserving_entrypoint_bound")


def _start_sandbox() -> None:
    record = _instance()
    info = _container_info()
    if info is None:
        if record.get("container_id"):
            raise lifecycle.LifecycleError("preserved_container_missing")
        _docker("create", "--name", CONTAINER,
                "--label", f"io.forgeflow.instance={record['instance']}",
                "--label", f"io.forgeflow.project={record['project']}",
                "--label", f"io.forgeflow.task={TASK}",
                "-p", f"127.0.0.1:{ODOO_PORT}:8069", IMAGE)
        info = _check_container(_container_info(), record)
        record.update(container_id=info["id"], image_id=info["image"],
                      mounts_sha256=_mount_digest(info), mount_digest_version=2)
        _save_instance(record, "container_created")
    else:
        info = _check_container(info, record)
        if info["state"] not in ("running", "exited", "created"):
            raise lifecycle.LifecycleError("container_state_not_resumable")
        if not record.get("container_id"):
            # Recover the checkpoint after creation, without a new container.
            record.update(container_id=info["id"], image_id=info["image"],
                          mounts_sha256=_mount_digest(info), mount_digest_version=2)
            _save_instance(record, "container_recovered")
        elif record.get("mount_digest_version") != 2:
            record.update(mounts_sha256=_mount_digest(info), mount_digest_version=2)
            _save_instance(record, "mount_binding_upgraded")
    _preserving_entrypoint(record, info)
    if info["state"] != "running":
        _docker("start", info["id"])
    _wait_sandbox()
    key = SANDBOX / "api_key"
    if not key.exists():
        _docker("cp", f"{CONTAINER}:/etc/odoo/api_key", str(key))
        key.chmod(0o400)
    _wait_erp()
    if not record.get("ready"):
        tests = ROOT / "vendor" / "erp-bench" / "tasks" / TASK / "tests"
        _docker("cp", str(tests), f"{CONTAINER}:/tests")


def _valid_planner(directory: Path) -> bool:
    """Check the bound bundle itself, not whether one entry point happens to exist."""
    sys.path.insert(0, str(ROOT / "src"))
    from forgeflow.planning.bundle import ERP_BENCH_COMMIT, OWN_FILES, UPSTREAM_FILES  # noqa: PLC0415
    from forgeflow.recovery.service import RecoveryError, verify_sources  # noqa: PLC0415

    if directory.is_symlink() or not directory.is_dir():
        return False
    try:
        if any(path.is_symlink() for path in directory.rglob("*")):
            return False
        record = verify_sources(directory)
        required = {"plan.py", "requirements.txt", "forgeflow/__init__.py"}
        required |= {f"forgeflow/planning/{name}" for name in OWN_FILES}
        required |= set(UPSTREAM_FILES.values())
        return (
            record.get("profile") == "cp-sat-recovery-v1"
            and record.get("upstream_commit") == ERP_BENCH_COMMIT
            and required <= record["files_sha256"].keys()
        )
    except (OSError, ValueError, TypeError, AttributeError, RecoveryError):
        return False


def _publish_planner(candidate: Path, planner: Path) -> None:
    """Retain an invalid cache for diagnosis and roll back a failed rename."""
    backup = None
    if planner.exists() or planner.is_symlink():
        backup = SANDBOX / f"planner.previous-{uuid.uuid4().hex}"
        planner.rename(backup)
    try:
        candidate.rename(planner)
    except OSError:
        if backup is not None:
            backup.rename(planner)
        raise


def _planner() -> None:
    planner = SANDBOX / "planner"
    if _valid_planner(planner):
        # Existing plans bind these source bytes. Never upgrade a valid cache here.
        return
    nested = planner / "planner"
    misplaced = (
        not planner.is_symlink() and planner.is_dir()
        and {path.name for path in planner.iterdir()} == {"planner"}
        and _valid_planner(nested)
    )
    if not misplaced:
        # Rebuilding damaged sources could invalidate an approval or an execution
        # journal. Moving a complete nested bundle, in contrast, preserves bytes.
        bound = any(
            path.exists() and (not path.is_dir() or any(path.iterdir()))
            for path in (SANDBOX / "store", SANDBOX / "approvals")
        )
        if bound or _read_pid("executor") is not None:
            raise SystemExit(
                "Planner bundle is invalid while execution state exists. "
                "Preserve the store and approvals; restore the original bound bundle."
            )
    with tempfile.TemporaryDirectory(prefix=".planner-stage-", dir=SANDBOX) as temporary:
        stage = Path(temporary)
        candidate = stage / "planner"
        if misplaced:
            shutil.copytree(nested, candidate, symlinks=True)
        else:
            from forgeflow.planning.bundle import build_planner_bundle  # noqa: PLC0415

            archive = stage / "planner.tar.gz"
            build_planner_bundle(ROOT, archive, recovery=True)
            with tarfile.open(archive, "r:gz") as bundle:
                for member in bundle.getmembers():
                    relative = PurePosixPath(member.name)
                    if (
                        relative.is_absolute() or ".." in relative.parts
                        or not relative.parts or relative.parts[0] != "planner"
                        or not (member.isfile() or member.isdir())
                    ):
                        raise SystemExit("Planner archive contains an unsupported entry.")
                # The archive already contains planner/; extract to its parent.
                bundle.extractall(stage, filter="data")
        if not _valid_planner(candidate):
            raise SystemExit("Planner bundle failed completeness or source-integrity checks.")
        _publish_planner(candidate, planner)


def _cleanup_owned_socket(record: dict | None) -> None:
    path = Path(SOCKET)
    if not path.exists() and not path.is_symlink():
        return
    expected = (record or {}).get("socket_identity")
    info = path.lstat()
    if (not expected or not stat.S_ISSOCK(info.st_mode)
            or expected != [info.st_dev, info.st_ino]):
        raise lifecycle.LifecycleError("executor_socket_unowned")
    lock_path = path.with_name("executor.lock")
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise lifecycle.LifecycleError("executor_socket_lock_unavailable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise lifecycle.LifecycleError("executor_socket_lock_untrusted")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise lifecycle.LifecycleError("executor_socket_active") from None
        current = path.lstat()
        if [current.st_dev, current.st_ino] != expected:
            raise lifecycle.LifecycleError("executor_socket_changed")
        path.unlink()
    finally:
        os.close(fd)


def _start_executor() -> None:
    if _read_pid("executor") is not None:
        return
    (SANDBOX / "store").mkdir(mode=0o700, exist_ok=True)
    (SANDBOX / "approvals").mkdir(mode=0o700, exist_ok=True)
    Path(SOCKET).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Never unlink an executor socket/lock just because a PID file is stale.
    # A live foreign listener must not be disconnected. The daemon owns cleanup.
    if Path(SOCKET).exists() or Path(SOCKET).is_symlink():
        previous, state = lifecycle.managed_process(_pid_file("executor"))
        if state != "exited":
            raise lifecycle.LifecycleError("executor_socket_exists_use_doctor")
        _cleanup_owned_socket(previous)
    command = [
        sys.executable, "-m", "forgeflow.executor.main",
        "--socket", SOCKET,
        "--store", str(SANDBOX / "store"),
        "--bundle", str(SANDBOX / "planner"),
        "--key", str(SANDBOX / "api_key"),
        "--agent-user", os.environ.get("USER", "user"),
        "--approval-store", str(SANDBOX / "approvals"),
        "--policy-file", str(RUNTIME_POLICY),
        "--product-verifier-container", CONTAINER,
        "--database", "bench", "--host", "127.0.0.1", "--port", str(ODOO_PORT),
        "--allow-local-peer",
    ]
    with (SANDBOX / "executor.log").open("a") as log:
        process = lifecycle.spawn_managed(_pid_file("executor"), command, "executor",
                                           cwd=ROOT, env=DOCKER_ENV, stdout=log,
                                           stderr=subprocess.STDOUT)
    for _ in range(40):
        if process.poll() is not None or _read_pid("executor") != process.pid:
            raise lifecycle.LifecycleError("executor_exited_before_ready")
        if Path(SOCKET).exists():
            info = Path(SOCKET).lstat()
            if not stat.S_ISSOCK(info.st_mode):
                raise lifecycle.LifecycleError("executor_socket_not_socket")
            record = lifecycle.read_record(_pid_file("executor"))
            record["socket_identity"] = [info.st_dev, info.st_ino]
            lifecycle.write_record(_pid_file("executor"), record)
            return
        time.sleep(0.25)
    raise SystemExit("executor did not publish its socket; see .runtime/local-sandbox/executor.log")


def _template_contract():
    from forgeflow.contract import contract_from_payload  # noqa: PLC0415

    contract = contract_from_payload(json.loads(POLICY.read_text(encoding="utf-8")))
    if contract.task_id != TASK:
        raise SystemExit("Local policy does not match the configured scenario task")
    return contract


def _seed_order_dates(ids: tuple[int, ...]) -> list[dict]:
    # IDs are validated integers from the task contract, never a free-form SQL input.
    selected = ",".join(str(value) for value in ids)
    sql = (
        "SELECT coalesce(json_agg(row_to_json(t)),'[]'::json) FROM ("
        "SELECT id,date_order::date AS ordered,create_date::date AS created,"
        "commitment_date::date AS due FROM sale_order WHERE id IN ("
        + selected + ") ORDER BY id) t;"
    )
    result = _docker("exec", "--user", "postgres", CONTAINER, "psql", "-d", "bench",
                     "-At", "-c", sql, capture=True)
    return json.loads(result.stdout)


def _prepare_local_policy() -> None:
    """Bind a new product sandbox to its observed seed day, preserving the template."""
    from forgeflow.contract import contract_from_payload  # noqa: PLC0415

    template = _template_contract()
    rows = _seed_order_dates(template.sales_order_ids)
    try:
        if [row["id"] for row in rows] != list(template.sales_order_ids):
            raise ValueError("seed order scope mismatch")
        days = {row["ordered"] for row in rows} | {row["created"] for row in rows}
        if len(days) != 1 or None in days:
            raise ValueError("seed planning day is ambiguous")
        anchor = date.fromisoformat(days.pop())
        actual_deadlines = tuple((date.fromisoformat(row["due"]) - anchor).days for row in rows)
        if actual_deadlines != CONFIG["deadlines"]:
            raise ValueError("seed deadlines differ from the reviewed task instruction")
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"Cannot bind local planning day: {error}") from None
    payload = template.payload()
    payload["contract_id"] = f"ff-local-{TASK.split('_', 1)[0]}-{anchor.isoformat()}-v1"
    payload["anchor_date"] = anchor.isoformat()
    payload["sources"].update({
        "anchor_date": "Fresh sandbox sale.order date_order/create_date agree on UTC day; "
                       "all commitment_date offsets match the reviewed task instruction",
        "local_template_sha256": template.digest(),
        "local_template_contract_id": template.contract_id,
    })
    contract = contract_from_payload(payload)
    if RUNTIME_POLICY.exists():
        raise SystemExit("Local policy already exists; preserve it and use a fresh sandbox root")
    RUNTIME_POLICY.write_text(json.dumps(contract.payload(), indent=2) + "\n")
    (SANDBOX / "planning-day-evidence.json").write_text(json.dumps({
        "scenario": SCENARIO, "task_id": TASK, "template_sha256": template.digest(),
        "template_anchor_date": template.anchor_date, "observed_anchor_date": anchor.isoformat(),
        "policy_sha256": contract.digest(), "seed_order_dates": rows,
    }, indent=2) + "\n")


def _write_request(policy_path: Path | None = None) -> None:
    from forgeflow.contract import check_request, contract_from_payload  # noqa: PLC0415

    contract = (_template_contract() if policy_path is None else
                contract_from_payload(json.loads(policy_path.read_text(encoding="utf-8"))))
    if contract.task_id != TASK:
        raise SystemExit("Local policy does not match the configured scenario task")
    identity = "vendor_ref" if contract.task_type == "supplier_cancellation" else "workcenter_code"
    request = {
        "anchor_date": contract.anchor_date,
        "disruption": {"kind": contract.task_type, identity: contract.disruption[identity]},
        "margin": contract.margin,
        "sales_order_budget_caps": dict(contract.budgets),
        "sales_order_ids": list(contract.sales_order_ids),
        "target_product_code": contract.product_code,
    }
    if not check_request(request, contract)["ok"]:
        raise SystemExit("Local request does not satisfy its scenario policy")
    (SANDBOX / "request.json").write_text(json.dumps(request, indent=2) + "\n")


def _dotenv_value(name: str) -> str:
    try:
        lines = (ROOT / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _prepare_dsh(env: dict[str, str]) -> None:
    """Load the optional harness config; never print or persist the model key."""
    if os.environ.get("FORGEFLOW_DSH_ENABLED", "").lower() in ("0", "false", "no"):
        return
    env_file = ROOT / ".runtime" / "dsh.env"
    if not env_file.is_file():
        setup = ROOT / "scripts" / "setup_dsh_harness.py"
        if setup.is_file():
            subprocess.run([sys.executable, str(setup)], cwd=ROOT, text=True,
                           stdout=subprocess.DEVNULL, check=False)
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key] = " ".join(shlex.split(value))
    except OSError:
        return
    if not env.get("NEBIUS_API_KEY"):
        key = _dotenv_value("NEBIUS_API_KEY")
        if key:
            env["NEBIUS_API_KEY"] = key
    print(f"DSH harness: {env.get('FORGEFLOW_DSH_CLI', 'not configured')}")


def _api_session_secret() -> str:
    """Preserve owner identities across restarts without exposing the key."""
    path = SANDBOX / "api-session.key"
    if not path.exists() and not path.is_symlink():
        fd, temporary = tempfile.mkstemp(prefix=".api-session-", dir=SANDBOX)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_urlsafe(48) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass  # Another starter owns the identity; never replace it.
        finally:
            Path(temporary).unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ValueError("unsafe key file")
            raw = handle.read(257)
        value = raw.decode("ascii").strip()
        if (not 32 <= len(value) <= 128
                or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in value)):
            raise ValueError("invalid key")
        return value
    except (OSError, ValueError):
        raise SystemExit("Cannot safely reuse API identity; preserve the sandbox key and repair its permissions") from None


def _start_api() -> None:
    record = _instance(required=False)
    revisions = _revision_capability(record, new_instance=False)
    candidates = _candidate_capability(record)
    candidate_profile = _candidate_profile(record, candidates)
    if record is None and revisions:
        raise lifecycle.LifecycleError("revisions_require_managed_sandbox")
    if record is None and candidates:
        raise lifecycle.LifecycleError("candidates_require_managed_sandbox")
    _check_revision_bundle(revisions)
    _check_candidate_bundle(candidates, candidate_profile)
    if _read_pid("api") is not None:
        return
    # A healthy unrelated service on this port is not this API's readiness.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", PORT))
    except OSError:
        raise lifecycle.LifecycleError("api_port_unavailable") from None
    approvals = _case_root()
    approvals.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "DOCKER_HOST": DOCKER_ENV.get("DOCKER_HOST", ""),
        "DOCKER_CONFIG": DOCKER_ENV.get("DOCKER_CONFIG", ""),
        "FORGEFLOW_DATA_ROOT": str(ROOT / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(approvals),
        "FORGEFLOW_SESSION_SECRET": _api_session_secret(),
        "FORGEFLOW_INVITE_CODES": INVITE,
        "FORGEFLOW_PUBLIC_RUNS": "*",
        "FORGEFLOW_SECURE_COOKIE": "false",
        "FORGEFLOW_LOCAL_PRODUCT": "1",
        "FORGEFLOW_LOCAL_SCENARIO": SCENARIO,
        "FORGEFLOW_LOCAL_TASK": TASK,
        "FORGEFLOW_LOCAL_SOCKET": SOCKET,
        "FORGEFLOW_LOCAL_STORE": str(SANDBOX / "store"),
        "FORGEFLOW_LOCAL_APPROVALS": str(SANDBOX / "approvals"),
        "FORGEFLOW_LOCAL_REQUEST": str(SANDBOX / "request.json"),
        "FORGEFLOW_LOCAL_CONTAINER": CONTAINER,
        "FORGEFLOW_LOCAL_REVISIONS": "1" if revisions else "0",
        "FORGEFLOW_LOCAL_CANDIDATES": "1" if candidates else "0",
        "FORGEFLOW_LOCAL_CANDIDATE_PROFILE": candidate_profile,
    })
    env.pop("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", None)
    _prepare_dsh(env)
    with (SANDBOX / "api.log").open("a") as log:
        process = lifecycle.spawn_managed(
            _pid_file("api"),
            [sys.executable, "-m", "forgeflow.cli", "api", "--host", "127.0.0.1", "--port", str(PORT)],
            "api", cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        probe = _run(["curl", "-sf", "--max-time", "2", f"http://127.0.0.1:{PORT}/api/health"], check=False, capture=True)
        if process.poll() is not None:
            raise lifecycle.LifecycleError("api_exited_before_ready")
        if probe.returncode == 0 and _read_pid("api") == process.pid:
            return
        time.sleep(0.5)
    raise SystemExit("API did not become healthy; see .runtime/local-sandbox/api.log")


def _bound_hashes() -> dict:
    result = {}
    for name in ("task-policy.json", "request.json"):
        path = SANDBOX / name
        if path.is_symlink() or not path.is_file():
            raise lifecycle.LifecycleError("resume_configuration_missing")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _initialize() -> None:
    record = _instance(required=False)
    revisions = _revision_capability(record)
    candidates = _candidate_capability(record)
    candidate_profile = _candidate_profile(record, candidates)
    if record is None:
        from forgeflow.product.local import revision_acceptance_setting  # noqa: PLC0415

        if revision_acceptance_setting() is True:
            _require_dedicated_revision_sandbox()
    if _read_pid("api") is not None or _read_pid("executor") is not None:
        raise SystemExit("Workbench processes are already running; unmanaged state cannot be initialized")
    _require_image()
    _planner()
    _check_revision_bundle(revisions)
    _check_candidate_bundle(candidates, candidate_profile)
    _template_contract()
    if record is None:
        if (RUNTIME_POLICY.exists() or (SANDBOX / "api-session.key").exists()
                or any(path.exists() and (not path.is_dir() or any(path.iterdir()))
                       for path in (SANDBOX / "store", SANDBOX / "approvals", _case_root()))):
            raise lifecycle.LifecycleError("prior_state_unmanaged_preserve_use_doctor")
        if _container_info() is not None:
            raise lifecycle.LifecycleError("container_exists_refusing_initialize")
        record = {"schema_version": 1, "uid": os.getuid(), "project": _project_id(),
                  "instance": uuid.uuid4().hex, "config": _config(), "ready": False,
                  "features": {"revisions": revisions, "candidates": candidates},
                  "candidate_profile": candidate_profile}
        _save_instance(record, "initializing")
    if record.get("ready"):
        raise lifecycle.LifecycleError("already_initialized_use_resume")
    _start_sandbox()
    # Crash-safe initialization checkpoints never re-seed a retained database.
    if not RUNTIME_POLICY.exists():
        _prepare_local_policy()
    if not (SANDBOX / "request.json").exists():
        _write_request(RUNTIME_POLICY)
    _api_session_secret()
    record = _instance()
    record.update(ready=True, bindings=_bound_hashes())
    _save_instance(record, "initialized")


def _resume() -> None:
    record = _instance()
    revisions = _revision_capability(record)
    candidates = _candidate_capability(record)
    candidate_profile = _candidate_profile(record, candidates)
    _check_revision_bundle(revisions)
    _check_candidate_bundle(candidates, candidate_profile)
    if not record.get("ready"):
        raise lifecycle.LifecycleError("initialization_incomplete_use_init")
    if record.get("deleted"):
        raise lifecycle.LifecycleError("container_deleted_use_fresh_sandbox")
    if record.get("bindings") != _bound_hashes():
        raise lifecycle.LifecycleError("resume_configuration_changed")
    if not (SANDBOX / "api-session.key").exists():
        raise lifecycle.LifecycleError("resume_identity_missing")
    key = SANDBOX / "api_key"
    if key.is_symlink() or not key.is_file():
        raise lifecycle.LifecycleError("resume_erp_key_missing")
    _api_session_secret()
    container_before = _check_container(_container_info(), record)
    if not _valid_planner(SANDBOX / "planner"):
        raise lifecycle.LifecycleError("resume_bound_planner_invalid")
    before = {role: _read_pid(role) for role in ("api", "executor")}
    try:
        _start_sandbox()
        _start_executor()
        _start_api()
    except BaseException:
        # Only children launched by this attempt are eligible for cleanup.
        # Keep their bound records if ownership or termination is uncertain.
        try:
            with _scope_lease():
                for role in ("api", "executor"):
                    if before[role] is None:
                        process = lifecycle.read_record(_pid_file(role))
                        _stop_pid(role)
                        if role == "executor":
                            _cleanup_owned_socket(process)
                # Do not leave a container started by this failed attempt running.
                # A container already serving before resume belongs to its prior
                # session, so retain it without interruption.
                if container_before["state"] != "running":
                    current = _check_container(_container_info(), record)
                    if current["state"] == "running":
                        _docker("stop", "--time", "20", current["id"])
            _save_instance(_instance(), "start_failed_preserved")
        except (OSError, lifecycle.LifecycleError):
            pass  # The original failure is retained; doctor exposes remaining state.
        raise
    _save_instance(_instance(), "running")
    print(f"ForgeFlow local workbench: http://127.0.0.1:{PORT}")


def _operation():
    if SANDBOX.is_symlink():
        raise lifecycle.LifecycleError("unsafe_sandbox_directory")
    SANDBOX.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Shared across this project's workbench instances; scope lock additionally
    # fences ERP execution. Runner/DSH must explicitly adopt the shared lease.
    return lifecycle.exclusive_lease(ROOT / ".runtime" / "lifecycle" / "workbench.lock")


def _heavy_operation():
    if (ROOT / ".runtime").is_symlink() or (ROOT / ".runtime/lifecycle").is_symlink():
        raise lifecycle.LifecycleError("unsafe_heavy_operation_directory")
    return lifecycle.exclusive_lease(ROOT / ".runtime/lifecycle/heavy-operation.lock")


def init() -> None:
    with _heavy_operation(), _operation():
        try:
            _initialize()
        except BaseException:
            _cleanup_failed_initialization()
            raise


def _cleanup_failed_initialization() -> None:
    try:
        record = _instance(required=False)
        if record is not None and not record.get("ready"):
            _stop()
    except (OSError, lifecycle.LifecycleError, subprocess.SubprocessError):
        pass  # Preserve uncertain ownership/state for doctor; never force remove.


def start() -> None:
    with _heavy_operation(), _operation():
        initializing = _instance(required=False) is None
        try:
            if initializing:
                _initialize()
            _resume()
        except BaseException:
            if initializing:
                try:
                    if _instance(required=False) is not None:
                        _stop()
                except (OSError, lifecycle.LifecycleError, subprocess.SubprocessError):
                    pass
            raise


def resume() -> None:
    with _heavy_operation(), _operation():
        _resume()


def up() -> None:
    """Compatibility alias: safe start, never an implicit reset."""
    start()


def _stop() -> None:
    record = _instance()
    info = _container_info()
    if info is not None:
        _check_container(info, record)  # AutoRemove checked BEFORE any stop.
        if info["state"] not in ("running", "exited", "created"):
            raise lifecycle.LifecycleError("container_state_not_stoppable")
    elif not record.get("deleted"):
        raise lifecycle.LifecycleError("preserved_container_missing")
    with _scope_lease():
        # Validate BOTH identities before signalling either service.
        _read_pid("api")
        _read_pid("executor")
        process = lifecycle.read_record(_pid_file("executor"))
        _stop_pid("api")
        _stop_pid("executor")
        _cleanup_owned_socket(process)
        if info is not None and info["state"] == "running":
            _docker("stop", "--time", "20", info["id"])
            after = _check_container(_container_info(), record)
            if after["state"] not in ("exited", "created"):
                raise lifecycle.LifecycleError("container_stop_unconfirmed")
    _save_instance(record, "stopped")
    print("Stopped owned services; container, volumes, cases and bindings retained.")


def stop() -> None:
    with _operation():
        _stop()


def down() -> None:
    """Compatibility alias now preserves data exactly like stop."""
    stop()


def delete(*, confirm_container_id: str) -> None:
    """Explicit destruction removes only the stopped container, never volumes/state."""
    with _operation():
        record = _instance()
        info = _container_info()
        if record.get("deleted") and info is None:
            if confirm_container_id != record.get("container_id"):
                raise lifecycle.LifecycleError("container_confirmation_mismatch")
            return
        info = _check_container(info, record)
        if confirm_container_id != info["id"]:
            raise lifecycle.LifecycleError("container_confirmation_mismatch")
        _stop()
        with _scope_lease():
            info = _check_container(_container_info(), record)
            if info["state"] not in ("exited", "created"):
                raise lifecycle.LifecycleError("container_not_stopped")
            _docker("rm", info["id"])  # no -f, no -v, no host-state deletion
        record["deleted"] = True
        _save_instance(record, "container_deleted")


def restart_api() -> None:
    """Restart only HTTP serving, retaining the database and authenticated owner."""
    record = _instance(required=False)
    revisions = _revision_capability(record, new_instance=False)
    candidates = _candidate_capability(record)
    candidate_profile = _candidate_profile(record, candidates)
    if revisions and record is None:
        raise lifecycle.LifecycleError("revisions_require_managed_sandbox")
    if candidates and record is None:
        raise lifecycle.LifecycleError("candidates_require_managed_sandbox")
    _check_revision_bundle(revisions)
    _check_candidate_bundle(candidates, candidate_profile)
    if not (SANDBOX / "api-session.key").exists():
        raise SystemExit("This sandbox has no persisted API identity; leave its running API intact")
    _api_session_secret()  # Validate before stopping the existing API.
    _stop_pid("api")
    _start_api()
    print(f"API restarted with its existing identity: http://127.0.0.1:{PORT}")


def doctor() -> dict:
    """Read-only: never mkdir, initialize, load credentials or contact a model."""
    report = {"scenario": SCENARIO, "task": TASK, "configured_ports": {"api": PORT, "odoo": ODOO_PORT},
              "processes": {}, "container": {"status": "unknown"}}
    try:
        record = _instance(required=False)
        report["managed"] = record is not None
    except lifecycle.LifecycleError as error:
        record = None
        report["configuration_error"] = str(error)
    if "configuration_error" not in report:
        try:
            report["features"] = {"revisions": _revision_capability(record),
                                  "candidates": _candidate_capability(record)}
            report["candidate_profile"] = _candidate_profile(record, report["features"]["candidates"])
            report["feature_binding"] = "persisted" if record else "new_instance_default"
            # Binding metadata only; doctor does not read the source or secrets.
            report["entrypoint"] = record.get("entrypoint") if record else None
        except lifecycle.LifecycleError as error:
            report["feature_error"] = str(error)
    for name in ("api", "executor"):
        try:
            process, state = lifecycle.managed_process(_pid_file(name))
            report["processes"][name] = {
                "state": state, "identity": process["identity"] if process else None,
                "rss_bytes": lifecycle.process_rss(process["identity"]) if state == "running" else None,
            }
        except lifecycle.LifecycleError as error:
            report["processes"][name] = {"state": "unmanaged", "reason": str(error)}
    try:
        info = _container_info()
        if info is None:
            report["container"] = {"status": "absent"}
        else:
            report["container"] = {
                "status": info["state"], "id": info["id"], "auto_remove": info.get("auto_remove"),
                "mounts": [{"type": mount.get("Type"), "destination": mount.get("Destination"),
                            "writable": mount.get("RW")} for mount in info["mounts"]],
            }
            try:
                if record is None:
                    raise lifecycle.LifecycleError("container_not_owned")
                _check_container(info, record)
                report["container"]["ownership"] = "matched"
            except lifecycle.LifecycleError as error:
                report["container"]["ownership"] = str(error)
    except lifecycle.LifecycleError as error:
        report["container"] = {"status": "unavailable", "reason": str(error)}
    scope_path = SANDBOX / "store" / "scope.lock"
    if not scope_path.exists():
        report["execution_lock"] = "not_recorded"
    else:
        try:
            with lifecycle.exclusive_lease(scope_path, create=False):
                report["execution_lock"] = "idle_at_check"
        except lifecycle.LifecycleError as error:
            report["execution_lock"] = str(error)
    report["resources"] = lifecycle.resource_snapshot(SANDBOX if SANDBOX.is_dir() else ROOT)
    print(json.dumps(report, indent=2))
    return report


def status() -> None:
    doctor()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("up", "start", "init", "resume", "stop", "down",
                                           "delete", "doctor", "status", "restart-api"),
                        nargs="?", default="start")
    parser.add_argument("--confirm-container-id", default="")
    args = parser.parse_args()
    try:
        if args.command == "delete":
            delete(confirm_container_id=args.confirm_container_id)
        elif args.command == "restart-api":
            with _operation():
                _instance()
                with _scope_lease():
                    restart_api()
        else:
            {"up": up, "start": start, "init": init, "resume": resume, "stop": stop,
             "down": down, "doctor": doctor, "status": status}[args.command]()
    except lifecycle.LifecycleError as error:
        print(f"Workbench operation refused: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
