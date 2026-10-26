"""Launcher bundle repair uses synthetic sources, never Docker or task artifacts."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import socket
import tarfile
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from forgeflow.planning import bundle as planner_bundle


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("FORGEFLOW_LOCAL_SCENARIO", "A")
    monkeypatch.delenv("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", raising=False)
    monkeypatch.delenv("FORGEFLOW_LOCAL_REVISIONS", raising=False)
    monkeypatch.delenv("FORGEFLOW_LOCAL_CANDIDATES", raising=False)
    monkeypatch.delenv("FORGEFLOW_LOCAL_CANDIDATE_PROFILE", raising=False)
    monkeypatch.setenv("FORGEFLOW_LOCAL_SANDBOX_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("FORGEFLOW_LOCAL_CONTAINER_NAME", "isolated-test-container")
    monkeypatch.setenv("FORGEFLOW_LOCAL_API_PORT", "18020")
    monkeypatch.setenv("FORGEFLOW_LOCAL_ODOO_PORT", "28020")
    socket_directory = tempfile.TemporaryDirectory(prefix="ff-socket-", dir="/tmp")
    monkeypatch.setenv("FORGEFLOW_LOCAL_SOCKET_PATH", str(Path(socket_directory.name) / "executor.sock"))
    monkeypatch.setenv("FORGEFLOW_LOCAL_CASE_ROOT", str(tmp_path / "cases"))
    monkeypatch.setenv("DOCKER_HOST", "unix:///unused-test-docker.sock")
    original_is_dir = Path.is_dir
    # Importing the launcher probes an optional Docker config. Tests must not
    # inspect the operator's runtime directory even for that optional probe.
    with monkeypatch.context() as isolated:
        isolated.setattr(
            Path, "is_dir",
            lambda path: False if path == root / ".runtime/docker-config"
            else original_is_dir(path),
        )
        spec = importlib.util.spec_from_file_location(
            "local_workbench_test", root / "scripts/local_workbench.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    module.ROOT = tmp_path
    module.SANDBOX.mkdir()
    monkeypatch.setattr(module, "_docker", lambda *_args, **_kwargs: pytest.fail("Docker called"))
    try:
        yield module
    finally:
        socket_directory.cleanup()


def _contents():
    names = {"plan.py", "recover.py", "requirements.txt", "forgeflow/__init__.py"}
    names |= {f"forgeflow/planning/{name}" for name in planner_bundle.OWN_FILES}
    names |= set(planner_bundle.UPSTREAM_FILES.values())
    names |= {f"forgeflow/recovery/{name}.py" for name in (
        "__init__", "service", "compiler", "readback", "scope_guard", "change_set",
        "journal", "odoo", "storage", "client", "index",
    )}
    names |= {f"forgeflow/executor/{name}.py" for name in (
        "__init__", "bootstrap", "daemon", "handlers", "main", "peer", "protocol",
    )}
    names |= {f"forgeflow/{name}.py" for name in ("approval", "contract", "odoo_credentials")}
    contents = {name: f"# synthetic launcher source: {name}\n".encode() for name in names}
    record = {
        "profile": "cp-sat-recovery-v1", "recovery_enabled": True,
        "upstream_commit": planner_bundle.ERP_BENCH_COMMIT,
        "files_sha256": {name: hashlib.sha256(data).hexdigest()
                         for name, data in contents.items()},
    }
    contents["provenance.json"] = json.dumps(record).encode()
    return contents


def test_api_restart_preserves_cookie_and_owner_identity(launcher):
    from forgeflow.api.auth import _identity, issue_cookie, verify_cookie
    from forgeflow.api.settings import Settings

    def settings():
        return Settings(session_secret=launcher._api_session_secret(),
                        invite_codes=("synthetic-test-invitation",))

    before, after = settings(), settings()
    assert before.session_secret == after.session_secret
    assert _identity(before, "synthetic-test-invitation") == _identity(after, "synthetic-test-invitation")
    cookie = issue_cookie(before, "synthetic-test-invitation")
    assert verify_cookie(after, cookie) == _identity(before, "synthetic-test-invitation")
    assert (launcher.SANDBOX / "api-session.key").stat().st_mode & 0o777 == 0o600


def test_product_executor_receives_only_configured_verifier_destination(launcher, monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(launcher.lifecycle, "process_identity", lambda pid: {
        "pid": pid, "uid": os.getuid(), "pgid": pid, "start": "synthetic:1",
    })
    listener = socket.socket(socket.AF_UNIX)

    def start(path, command, role, **kwargs):
        calls.append((command, kwargs))
        launcher.lifecycle.record_process(path, 123456, role)
        listener.bind(launcher.SOCKET)
        return SimpleNamespace(pid=123456, poll=lambda: None)

    monkeypatch.setattr(launcher.lifecycle, "spawn_managed", start)
    try:
        launcher._start_executor()
    finally:
        listener.close()
    (command, kwargs), = calls
    assert command[command.index("--product-verifier-container") + 1] == launcher.CONTAINER
    assert "--autonomous" not in command
    assert kwargs["env"] == launcher.DOCKER_ENV
    assert kwargs["env"]["DOCKER_HOST"] == "unix:///unused-test-docker.sock"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "corrupt", "directory"])
def test_api_identity_never_rotates_or_follows_an_unsafe_key(launcher, kind):
    path = launcher.SANDBOX / "api-session.key"
    target = launcher.SANDBOX / "existing-key"
    target.write_text("synthetic-test-value-that-must-be-preserved")
    target.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_text("invalid key" if kind == "corrupt" else "x" * 64)
        path.chmod(0o644 if kind == "public" else 0o600)
    with pytest.raises(SystemExit, match="Cannot safely reuse API identity"):
        launcher._api_session_secret()
    assert target.read_text() == "synthetic-test-value-that-must-be-preserved"
    assert path.is_symlink() if kind == "symlink" else path.exists()


def test_api_restart_does_not_reset_execution_or_identity(launcher, monkeypatch):
    secret = launcher._api_session_secret()
    socket = Path(launcher.SOCKET)
    socket.write_text("synthetic executor socket sentinel")
    calls = []
    monkeypatch.setattr(launcher, "_stop_pid", lambda name: calls.append(("stop", name)))
    monkeypatch.setattr(launcher, "_start_api", lambda: calls.append(("start", "api")))
    launcher.restart_api()
    assert calls == [("stop", "api"), ("start", "api")]
    assert launcher._api_session_secret() == secret
    assert socket.read_text() == "synthetic executor socket sentinel"


def test_legacy_api_without_persisted_identity_is_not_stopped(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "_stop_pid", lambda *_: pytest.fail("legacy API stopped"))
    with pytest.raises(SystemExit, match="leave its running API intact"):
        launcher.restart_api()


def _write_bundle(directory, contents):
    for name, data in contents.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _archive_builder(monkeypatch, contents):
    calls = []

    def build(root, destination, *, recovery):
        assert recovery is True
        calls.append(root)
        with tarfile.open(destination, "w:gz") as archive:
            for name, data in contents.items():
                member = tarfile.TarInfo("planner/" + name)
                member.size = len(data)
                member.mode = 0o444
                archive.addfile(member, io.BytesIO(data))

    monkeypatch.setattr(planner_bundle, "build_planner_bundle", build)
    return calls


def test_prefixed_archive_is_published_at_executor_bundle_root(launcher, monkeypatch):
    calls = _archive_builder(monkeypatch, _contents())
    launcher._planner()
    root = launcher.SANDBOX / "planner"
    assert launcher._valid_planner(root)
    assert (root / "provenance.json").is_file()
    assert not (root / "planner").exists()
    assert len(calls) == 1
    assert launcher.CONTAINER == "isolated-test-container"
    assert launcher.PORT == 18020
    assert launcher.ODOO_PORT == 28020
    assert launcher.SOCKET.endswith("executor.sock")


def test_valid_existing_bundle_preserves_exact_source_bytes(launcher, monkeypatch):
    contents = _contents()
    root = launcher.SANDBOX / "planner"
    _write_bundle(root, contents)
    original = (root / "provenance.json").stat().st_mtime_ns
    calls = _archive_builder(monkeypatch, {})
    launcher._planner()
    assert not calls
    assert (root / "provenance.json").stat().st_mtime_ns == original
    assert all((root / name).read_bytes() == data for name, data in contents.items())
    assert not list(launcher.SANDBOX.glob("planner.previous-*"))


def test_nested_cache_repair_preserves_manifest_and_existing_bindings(launcher, monkeypatch):
    contents = _contents()
    root = launcher.SANDBOX / "planner"
    _write_bundle(root / "planner", contents)
    for folder in ("store", "approvals"):
        path = launcher.SANDBOX / folder
        path.mkdir()
        (path / "retained.json").write_text("synthetic retained binding")
    calls = _archive_builder(monkeypatch, {})
    launcher._planner()
    assert not calls
    assert launcher._valid_planner(root)
    assert all((root / name).read_bytes() == data for name, data in contents.items())
    backup, = launcher.SANDBOX.glob("planner.previous-*")
    assert launcher._valid_planner(backup / "planner")
    for folder in ("store", "approvals"):
        assert (launcher.SANDBOX / folder / "retained.json").read_text() == (
            "synthetic retained binding"
        )


@pytest.mark.parametrize("damage", ["missing_manifest", "changed_source", "missing_entry"])
def test_incomplete_unbound_cache_is_validated_and_rebuilt(launcher, monkeypatch, damage):
    root = launcher.SANDBOX / "planner"
    _write_bundle(root, _contents())
    if damage == "missing_manifest":
        (root / "provenance.json").unlink()
    elif damage == "changed_source":
        (root / "plan.py").write_text("# changed bytes")
    else:
        (root / "recover.py").unlink()
    calls = _archive_builder(monkeypatch, _contents())
    launcher._planner()
    assert len(calls) == 1
    assert launcher._valid_planner(root)
    assert len(list(launcher.SANDBOX.glob("planner.previous-*"))) == 1


@pytest.mark.parametrize("protected", ["store", "approvals", "running_executor"])
def test_invalid_bound_cache_is_not_replaced(launcher, monkeypatch, protected):
    root = launcher.SANDBOX / "planner"
    root.mkdir()
    (root / "plan.py").write_text("# incomplete original")
    if protected == "running_executor":
        monkeypatch.setattr(launcher, "_read_pid", lambda _name: 12345)
    else:
        (launcher.SANDBOX / protected).mkdir()
        (launcher.SANDBOX / protected / "binding.json").write_text("retained")
    calls = _archive_builder(monkeypatch, _contents())
    with pytest.raises(SystemExit, match="restore the original bound bundle"):
        launcher._planner()
    assert not calls
    assert (root / "plan.py").read_text() == "# incomplete original"
    assert not list(launcher.SANDBOX.glob("planner.previous-*"))


def test_invalid_new_archive_never_replaces_existing_cache(launcher, monkeypatch):
    root = launcher.SANDBOX / "planner"
    root.mkdir()
    (root / "plan.py").write_text("# retained incomplete cache")
    bad = _contents()
    bad["plan.py"] = b"# digest mismatch"
    _archive_builder(monkeypatch, bad)
    with pytest.raises(SystemExit, match="source-integrity"):
        launcher._planner()
    assert (root / "plan.py").read_text() == "# retained incomplete cache"
    assert not list(launcher.SANDBOX.glob(".planner-stage-*"))


def test_publish_failure_restores_original_directory(launcher, monkeypatch):
    root = launcher.SANDBOX / "planner"
    root.mkdir()
    (root / "original").write_text("retained")
    candidate = launcher.SANDBOX / "candidate"
    candidate.mkdir()
    rename = Path.rename

    def fail_publication(path, target):
        if path == candidate:
            raise OSError("synthetic rename failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_publication)
    with pytest.raises(OSError, match="synthetic rename"):
        launcher._publish_planner(candidate, root)
    assert (root / "original").read_text() == "retained"


def test_startup_validates_bundle_before_touching_the_sandbox(launcher, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_require_image", lambda: None)
    monkeypatch.setattr(launcher, "_start_sandbox", lambda: calls.append("sandbox"))

    def fail_validation():
        raise SystemExit("invalid planner")

    monkeypatch.setattr(launcher, "_planner", fail_validation)
    with pytest.raises(SystemExit, match="invalid planner"):
        launcher.up()
    assert not calls


@pytest.mark.parametrize("scenario", ["A", "B"])
def test_scenario_request_uses_exact_reviewed_contract(launcher, monkeypatch, scenario):
    from forgeflow.contract import check_request, contract_from_payload

    root = Path(__file__).resolve().parents[1]
    config = launcher.SCENARIO_CONFIG[scenario]
    policy = root / "contracts" / config["policy"]
    monkeypatch.setattr(launcher, "TASK", config["task"])
    monkeypatch.setattr(launcher, "POLICY", policy)
    launcher._write_request()
    request = json.loads((launcher.SANDBOX / "request.json").read_text())
    contract = contract_from_payload(json.loads(policy.read_text()))
    assert check_request(request, contract)["ok"]
    assert request["anchor_date"] == contract.anchor_date
    assert request["sales_order_budget_caps"] == dict(contract.budgets)
    if scenario == "B":
        assert request["disruption"] == {
            "kind": "workcenter_outage", "workcenter_code": "P1223565D60-WC01",
        }
        assert len(request["sales_order_ids"]) == 12
        assert sum(request["sales_order_budget_caps"].values()) == 943302


def test_wrong_task_policy_is_rejected_before_request_published(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "TASK", "2299_hard_repair_plan_hard")
    with pytest.raises(SystemExit, match="does not match"):
        launcher._write_request()
    assert not (launcher.SANDBOX / "request.json").exists()


def test_up_does_not_reset_erp_under_running_workbench(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "_read_pid", lambda _name: 12345)
    with pytest.raises(SystemExit, match="already running"):
        launcher.up()


@pytest.mark.parametrize("scenario", ["A", "B"])
def test_fresh_sandbox_policy_binds_observed_seed_day_without_changing_template(
    launcher, monkeypatch, scenario,
):
    root = Path(__file__).resolve().parents[1]
    config = launcher.SCENARIO_CONFIG[scenario]
    policy = root / "contracts" / config["policy"]
    original = policy.read_bytes()
    monkeypatch.setattr(launcher, "TASK", config["task"])
    monkeypatch.setattr(launcher, "CONFIG", config)
    monkeypatch.setattr(launcher, "POLICY", policy)
    today = date(2026, 10, 23)
    rows = [
        {"id": index, "ordered": today.isoformat(), "created": today.isoformat(),
         "due": (today + timedelta(days=deadline)).isoformat()}
        for index, deadline in enumerate(config["deadlines"], 1)
    ]
    monkeypatch.setattr(launcher, "_seed_order_dates", lambda _ids: rows)
    launcher._prepare_local_policy()
    launcher._write_request(launcher.RUNTIME_POLICY)
    request = json.loads((launcher.SANDBOX / "request.json").read_text())
    derived = json.loads(launcher.RUNTIME_POLICY.read_text())
    assert request["anchor_date"] == derived["anchor_date"] == "2026-10-23"
    assert derived["budgets"] == json.loads(original)["budgets"]
    assert policy.read_bytes() == original
    assert derived["sources"]["local_template_sha256"]
    with pytest.raises(SystemExit, match="already exists"):
        launcher._prepare_local_policy()


@pytest.mark.parametrize("damage", ["mixed_day", "wrong_deadline", "missing_order"])
def test_ambiguous_or_different_seed_fails_before_policy_publication(launcher, monkeypatch, damage):
    anchor = date(2026, 10, 23)
    rows = [
        {"id": index, "ordered": anchor.isoformat(), "created": anchor.isoformat(),
         "due": (anchor + timedelta(days=deadline)).isoformat()}
        for index, deadline in enumerate(launcher.CONFIG["deadlines"], 1)
    ]
    if damage == "mixed_day":
        rows[0]["created"] = "2026-10-22"
    elif damage == "wrong_deadline":
        rows[0]["due"] = "2026-11-20"
    else:
        rows.pop()
    monkeypatch.setattr(launcher, "_seed_order_dates", lambda _ids: rows)
    with pytest.raises(SystemExit, match="Cannot bind local planning day"):
        launcher._prepare_local_policy()
    assert not launcher.RUNTIME_POLICY.exists()


def _managed(launcher, monkeypatch, *, state="exited"):
    """An owned Docker state machine, with no Docker subprocess or secret read."""
    from types import SimpleNamespace

    lifecycle = launcher.lifecycle
    monkeypatch.setattr(lifecycle, "resource_snapshot", lambda _path: {
        "measured_at": 1, "available_memory_bytes": None, "swap_used_bytes": None,
    })
    monkeypatch.setattr(launcher, "_valid_planner", lambda _path: True)
    monkeypatch.setattr(launcher, "_wait_erp", lambda: None)
    monkeypatch.setattr(launcher, "_preserving_entrypoint", lambda _record, _info: None)
    for name in ("task-policy.json", "request.json"):
        (launcher.SANDBOX / name).write_text('{"synthetic": true}')
    (launcher.SANDBOX / "api_key").write_text("synthetic-private-test-value")
    launcher._api_session_secret()
    record = {"schema_version": 1, "uid": os.getuid(), "project": launcher._project_id(),
              "instance": "a" * 32, "config": launcher._config(), "ready": True,
              "bindings": launcher._bound_hashes()}
    info = {"id": "b" * 64, "name": "/" + launcher.CONTAINER, "image": "sha256:synthetic",
            "state": state, "auto_remove": False,
            "mounts": [{"Type": "volume", "Name": "synthetic-volume",
                        "Source": "/private/synthetic-volume", "Destination": "/data", "RW": True}],
            "ports": {"8069/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(launcher.ODOO_PORT)}]},
            "instance": record["instance"], "project": record["project"], "task": launcher.TASK}
    record.update(container_id=info["id"], image_id=info["image"],
                  mounts_sha256=launcher._mount_digest(info), mount_digest_version=2)
    lifecycle.write_record(launcher._instance_path(), record)
    container = {"info": info}
    calls = []

    def docker(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return SimpleNamespace(returncode=0 if container["info"] else 1,
                                   stdout=json.dumps(container["info"]) if container["info"] else "")
        if args[:2] == ("ps", "-a"):
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] in ("start", "stop", "rm"):
            assert args[-1] == info["id"]
            if args[0] == "rm":
                assert args == ("rm", info["id"])
                container["info"] = None
            else:
                info["state"] = "running" if args[0] == "start" else "exited"
            return SimpleNamespace(returncode=0, stdout="")
        if args[:1] == ("exec",):
            if args[1:] == ("--user", "postgres", launcher.CONTAINER, "psql", "-d", "bench",
                            "-At", "-c", "SELECT 1;"):
                return SimpleNamespace(returncode=0, stdout="1\n")
            assert args[2:] == ("sh", "-c", launcher.local_entrypoint.READY_CHECK)
            return SimpleNamespace(returncode=0, stdout="")
        pytest.fail(f"unexpected synthetic Docker operation: {args[0]}")

    monkeypatch.setattr(launcher, "_docker", docker)
    return record, container, calls


def _business_state(launcher):
    for folder in (launcher.SANDBOX / "store", launcher.SANDBOX / "approvals", launcher._case_root()):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "unknown-execution.json").write_text('{"state": "unknown", "retained": true}')
    paths = [launcher.SANDBOX / name for name in ("task-policy.json", "request.json", "api-session.key")]
    paths += [folder / "unknown-execution.json" for folder in
              (launcher.SANDBOX / "store", launcher.SANDBOX / "approvals", launcher._case_root())]
    return {path: path.read_bytes() for path in paths}


def test_start_and_stop_preserve_bound_unknown_state_and_container(launcher, monkeypatch):
    _record, container, calls = _managed(launcher, monkeypatch)
    original = _business_state(launcher)
    services = {"api": None, "executor": None}
    starts = []
    monkeypatch.setattr(launcher, "_read_pid", lambda role: services[role])

    def start(role):
        if services[role] is None:
            starts.append(role)
            services[role] = 12345 if role == "api" else 23456

    monkeypatch.setattr(launcher, "_start_executor", lambda: start("executor"))
    monkeypatch.setattr(launcher, "_start_api", lambda: start("api"))
    monkeypatch.setattr(launcher, "_stop_pid", lambda role: services.update({role: None}))
    launcher.up()
    launcher.start()
    assert starts == ["executor", "api"]
    assert len([args for args in calls if args[0] == "start"]) == 1
    launcher.down()
    launcher.stop()
    assert container["info"]["state"] == "exited"
    assert len([args for args in calls if args[0] == "stop"]) == 1
    assert not any(args[0] in ("run", "rm") for args in calls)
    assert {path: path.read_bytes() for path in original} == original
    assert len(launcher._instance()["resources"]) == 4


def test_interrupted_container_creation_recovers_identity_before_ready(launcher, monkeypatch):
    record, container, calls = _managed(launcher, monkeypatch)
    for key in ("container_id", "image_id", "mounts_sha256"):
        record.pop(key)
    record["ready"] = False
    launcher.lifecycle.write_record(launcher._instance_path(), record)
    docker = launcher._docker
    monkeypatch.setattr(launcher, "_docker", lambda *args, **kwargs:
                        None if args[:1] == ("cp",) else docker(*args, **kwargs))
    launcher._start_sandbox()
    restored = launcher._instance()
    assert restored["container_id"] == container["info"]["id"]
    assert restored["image_id"] == container["info"]["image"]
    assert restored["mounts_sha256"] == launcher._mount_digest(container["info"])
    assert not any(args[0] in {"run", "rm"} for args in calls)


def test_occupied_api_port_cannot_be_mistaken_for_started_workbench(launcher, monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        launcher.PORT = listener.getsockname()[1]
        monkeypatch.setattr(launcher, "_read_pid", lambda _: None)
        monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: pytest.fail("foreign port launch"))
        with pytest.raises(launcher.lifecycle.LifecycleError, match="api_port_unavailable"):
            launcher._start_api()


def _revision_bundle(launcher, *, candidates=False):
    contents = _contents()
    record = json.loads(contents["provenance.json"])
    names = ["product_versions", "product_constraints"]
    if candidates:
        names += ["product_costs", "product_candidates"]
    for name in names:
        path = f"forgeflow/recovery/{name}.py"
        contents[path] = b"# synthetic revision module\n"
        record["files_sha256"][path] = hashlib.sha256(contents[path]).hexdigest()
    contents["provenance.json"] = json.dumps(record).encode()
    _write_bundle(launcher.SANDBOX / "planner", contents)


def test_acceptance_is_persisted_only_when_initializing_a_new_dedicated_instance(launcher, monkeypatch):
    monkeypatch.setenv("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", "1")
    _revision_bundle(launcher)
    for name in ("_require_image", "_planner", "_template_contract", "_start_sandbox"):
        monkeypatch.setattr(launcher, name, lambda: None)
    monkeypatch.setattr(launcher, "_container_info", lambda: None)
    monkeypatch.setattr(launcher, "_prepare_local_policy", lambda:
                        launcher.RUNTIME_POLICY.write_text('{"synthetic": true}'))
    monkeypatch.setattr(launcher, "_write_request", lambda _policy:
                        (launcher.SANDBOX / "request.json").write_text('{"synthetic": true}'))
    launcher._initialize()
    saved = launcher._instance()
    assert saved["features"] == {"revisions": True, "candidates": False} and saved["ready"] is True
    monkeypatch.delenv("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE")
    assert launcher._revision_capability(launcher._instance()) is True
    assert launcher._instance() == saved


@pytest.mark.parametrize("field", ["sandbox", "cases", "container", "socket", "missing_destination"])
def test_acceptance_cannot_initialize_an_ordinary_destination(launcher, monkeypatch, field):
    monkeypatch.setenv("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", "1")
    if field == "sandbox":
        launcher.SANDBOX = launcher.ROOT / ".runtime/local-sandbox"
    elif field == "cases":
        monkeypatch.setenv("FORGEFLOW_LOCAL_CASE_ROOT", str(launcher.ROOT / ".runtime/demo-approvals-b"))
    elif field == "container":
        launcher.CONTAINER = "ff-local-workbench"
    elif field == "socket":
        launcher.SOCKET = "/tmp/ff-local-b/executor.sock"
    else:
        monkeypatch.delenv("FORGEFLOW_LOCAL_CASE_ROOT")
    monkeypatch.setattr(launcher, "_require_image", lambda: pytest.fail("image inspected"))
    with pytest.raises(launcher.lifecycle.LifecycleError, match="requires_dedicated_sandbox"):
        launcher._initialize()
    assert not launcher._instance_path().exists()


@pytest.mark.parametrize("saved,requested", [(None, "1"), (False, "1"), (True, "0")])
def test_acceptance_setting_cannot_change_on_resume_or_api_restart(launcher, monkeypatch, saved, requested):
    record, _container, calls = _managed(launcher, monkeypatch)
    if saved is not None:
        record["acceptance"] = {"revisions": saved}
        launcher.lifecycle.write_record(launcher._instance_path(), record)
    monkeypatch.setenv("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", requested)
    monkeypatch.setattr(launcher, "_stop_pid", lambda _: pytest.fail("API stopped before config validation"))
    for operation in (launcher.resume, launcher.restart_api):
        with pytest.raises(launcher.lifecycle.LifecycleError, match="revision_configuration_changed"):
            operation()
    assert launcher._instance() == record and calls == []


@pytest.mark.parametrize("damage", [None, "missing_constraint", "changed_bytes"])
def test_acceptance_checks_bound_bundle_without_rebuilding_it(launcher, damage):
    _revision_bundle(launcher)
    path = launcher.SANDBOX / "planner/forgeflow/recovery/product_constraints.py"
    if damage == "missing_constraint":
        path.unlink()
    elif damage == "changed_bytes":
        path.write_text("# changed source\n")
    if damage is None:
        launcher._check_revision_bundle(True)
    else:
        before = {str(p): p.read_bytes() for p in (launcher.SANDBOX / "planner").rglob("*") if p.is_file()}
        with pytest.raises(launcher.lifecycle.LifecycleError, match="bound_bundle_unsupported"):
            launcher._check_revision_bundle(True)
        assert before == {str(p): p.read_bytes() for p in (launcher.SANDBOX / "planner").rglob("*") if p.is_file()}


def test_acceptance_rejects_an_intact_legacy_bundle(launcher):
    _write_bundle(launcher.SANDBOX / "planner", _contents())
    assert launcher._valid_planner(launcher.SANDBOX / "planner")
    with pytest.raises(launcher.lifecycle.LifecycleError, match="bound_bundle_unsupported"):
        launcher._check_revision_bundle(True)
    launcher._check_revision_bundle(False)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("candidates", [None, False, True])
def test_api_start_uses_the_persisted_acceptance_choice(launcher, monkeypatch, enabled, candidates):
    from types import SimpleNamespace

    launcher.PORT = 0
    record, _container, _calls = _managed(launcher, monkeypatch)
    if candidates is None:
        record["acceptance"] = {"revisions": enabled}
    else:
        record["features"] = {"revisions": enabled, "candidates": candidates}
    launcher.lifecycle.write_record(launcher._instance_path(), record)
    _revision_bundle(launcher, candidates=candidates)
    monkeypatch.setattr(launcher, "_prepare_dsh", lambda _: None)
    monkeypatch.setattr(launcher, "_run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    spawned = []
    monkeypatch.setattr(launcher, "_read_pid", lambda _: 12345 if spawned else None)

    def spawn(_path, _command, _role, **kwargs):
        spawned.append(kwargs["env"])
        return SimpleNamespace(pid=12345, poll=lambda: None)

    monkeypatch.setattr(launcher.lifecycle, "spawn_managed", spawn)
    launcher._start_api()
    assert len(spawned) == 1
    assert spawned[0]["FORGEFLOW_LOCAL_REVISIONS"] == ("1" if enabled else "0")
    assert spawned[0]["FORGEFLOW_LOCAL_CANDIDATES"] == ("1" if candidates else "0")
    assert "FORGEFLOW_LOCAL_REVISION_ACCEPTANCE" not in spawned[0]


def test_preserving_stop_is_allowed_even_with_a_conflicting_acceptance_override(launcher, monkeypatch):
    record, container, _calls = _managed(launcher, monkeypatch, state="running")
    record["acceptance"] = {"revisions": True}
    launcher.lifecycle.write_record(launcher._instance_path(), record)
    monkeypatch.setenv("FORGEFLOW_LOCAL_REVISION_ACCEPTANCE", "0")
    monkeypatch.setattr(launcher, "_stop_pid", lambda _: None)
    launcher.stop()
    assert container["info"]["state"] == "exited"
    assert launcher._instance()["acceptance"] == {"revisions": True}


def test_runner_heavy_lease_blocks_startup_but_not_preserving_stop(launcher, monkeypatch):
    _record, container, calls = _managed(launcher, monkeypatch, state="running")
    monkeypatch.setattr(launcher, "_stop_pid", lambda _: None)
    with launcher.lifecycle.exclusive_lease(launcher.ROOT / ".runtime/lifecycle/heavy-operation.lock"):
        for operation in (launcher.init, launcher.start, launcher.resume):
            with pytest.raises(launcher.lifecycle.LifecycleError, match="operation_in_progress"):
                operation()
        launcher.stop()
    assert container["info"]["state"] == "exited"
    assert not any(args[0] in {"run", "start", "rm"} for args in calls)


@pytest.mark.parametrize("damage,reason", [
    ("owner", "container_not_owned"), ("auto_remove", "auto_remove"),
    ("mount", "identity_or_mounts_changed"), ("id", "identity_or_mounts_changed"),
    ("image", "identity_or_mounts_changed"), ("port", "ports_changed"),
    ("paused", "state_not_stoppable"),
])
def test_stop_refuses_unknown_or_unsafe_container_before_process_signal(launcher, monkeypatch, damage, reason):
    _record, container, calls = _managed(launcher, monkeypatch, state="running")
    info = container["info"]
    if damage == "owner":
        info["instance"] = "foreign"
    elif damage == "auto_remove":
        info["auto_remove"] = True
    elif damage == "mount":
        info["mounts"][0]["Source"] = "/different-source"
    elif damage in ("id", "image"):
        info[damage] = "different"
    elif damage == "port":
        info["ports"]["8069/tcp"][0]["HostIp"] = "0.0.0.0"
    else:
        info["state"] = "paused"
    monkeypatch.setattr(launcher, "_stop_pid", lambda *_: pytest.fail("service signalled"))
    with pytest.raises(launcher.lifecycle.LifecycleError, match=reason):
        launcher.stop()
    assert not any(args[0] in ("stop", "rm") for args in calls)


def test_active_execution_scope_refuses_stop_and_delete(launcher, monkeypatch):
    _record, _container, calls = _managed(launcher, monkeypatch, state="running")
    monkeypatch.setattr(launcher, "_stop_pid", lambda *_: pytest.fail("active service signalled"))
    with launcher.lifecycle.exclusive_lease(launcher.SANDBOX / "store/scope.lock"):
        for operation in (launcher.stop, lambda: launcher.delete(confirm_container_id="b" * 64)):
            with pytest.raises(launcher.lifecycle.LifecycleError, match="operation_in_progress"):
                operation()
        assert launcher.doctor()["execution_lock"] == "operation_in_progress"
    assert not any(args[0] in ("stop", "rm") for args in calls)


def test_stop_checks_both_pid_identities_before_signalling_api(launcher, monkeypatch):
    _managed(launcher, monkeypatch, state="running")

    def pid(role):
        if role == "executor":
            raise launcher.lifecycle.LifecycleError("pid_reused_refusing_operation")
        return 12345

    monkeypatch.setattr(launcher, "_read_pid", pid)
    monkeypatch.setattr(launcher, "_stop_pid", lambda *_: pytest.fail("API stopped before identity check"))
    with pytest.raises(launcher.lifecycle.LifecycleError, match="pid_reused"):
        launcher.stop()


def test_delete_requires_exact_identity_and_retains_state_and_volumes(launcher, monkeypatch):
    _record, container, calls = _managed(launcher, monkeypatch, state="running")
    original = _business_state(launcher)
    with pytest.raises(launcher.lifecycle.LifecycleError, match="confirmation_mismatch"):
        launcher.delete(confirm_container_id="b" * 12)
    assert not any(args[0] in ("stop", "rm") for args in calls)
    launcher.delete(confirm_container_id="b" * 64)
    launcher.delete(confirm_container_id="b" * 64)
    assert container["info"] is None
    assert [args for args in calls if args[0] == "rm"] == [("rm", "b" * 64)]
    assert {path: path.read_bytes() for path in original} == original
    with pytest.raises(launcher.lifecycle.LifecycleError, match="deleted_use_fresh"):
        launcher.start()


@pytest.mark.parametrize("damage,reason", [("request", "configuration_changed"),
                                          ("identity", "identity_missing"),
                                          ("erp_key", "erp_key_missing"),
                                          ("container", "container_missing")])
def test_resume_never_reinitializes_missing_or_changed_bindings(launcher, monkeypatch, damage, reason):
    _record, container, calls = _managed(launcher, monkeypatch)
    if damage == "request":
        (launcher.SANDBOX / "request.json").write_text('{"changed": true}')
    elif damage == "identity":
        (launcher.SANDBOX / "api-session.key").unlink()
    elif damage == "erp_key":
        (launcher.SANDBOX / "api_key").unlink()
    else:
        container["info"] = None
    monkeypatch.setattr(launcher, "_start_executor", lambda: pytest.fail("executor launched"))
    monkeypatch.setattr(launcher, "_initialize", lambda: pytest.fail("reinitialized"))
    with pytest.raises(launcher.lifecycle.LifecycleError, match=reason):
        launcher.resume()
    assert not any(args[0] in ("run", "start", "rm") for args in calls)


def test_failed_resume_cleans_only_new_children_and_newly_started_container(launcher, monkeypatch):
    _record, container, calls = _managed(launcher, monkeypatch)
    original = _business_state(launcher)
    active = {"api": None, "executor": None}
    stopped = []
    monkeypatch.setattr(launcher, "_read_pid", lambda role: active[role])
    monkeypatch.setattr(launcher, "_start_executor", lambda: active.update(executor=12345))

    def fail_api():
        active["api"] = 23456
        raise RuntimeError("synthetic API health failure")

    def stop(role):
        stopped.append(role)
        active[role] = None

    monkeypatch.setattr(launcher, "_start_api", fail_api)
    monkeypatch.setattr(launcher, "_stop_pid", stop)
    with pytest.raises(RuntimeError, match="synthetic API"):
        launcher.resume()
    assert stopped == ["api", "executor"]
    assert container["info"]["state"] == "exited"
    assert not any(args[0] == "rm" for args in calls)
    assert {path: path.read_bytes() for path in original} == original
    assert launcher._instance()["phase"] == "start_failed_preserved"


def test_failed_resume_does_not_stop_previous_services_or_container(launcher, monkeypatch):
    _managed(launcher, monkeypatch, state="running")
    monkeypatch.setattr(launcher, "_read_pid", lambda role: 12345 if role == "executor" else None)
    monkeypatch.setattr(launcher, "_start_executor", lambda: None)

    def fail():
        raise RuntimeError("synthetic failure")

    stopped = []
    monkeypatch.setattr(launcher, "_start_api", fail)
    monkeypatch.setattr(launcher, "_stop_pid", stopped.append)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        launcher.resume()
    assert stopped == ["api"]


def test_doctor_is_readonly_and_does_not_expose_mount_sources(launcher, monkeypatch, capsys):
    _managed(launcher, monkeypatch)
    before = {path.relative_to(launcher.ROOT): path.read_bytes()
              for path in launcher.ROOT.rglob("*") if path.is_file()}
    monkeypatch.setattr(launcher, "_api_session_secret", lambda: pytest.fail("secret read"))
    report = launcher.doctor()
    assert report["managed"] is True
    assert report["execution_lock"] == "not_recorded"
    assert report["container"]["ownership"] == "matched"
    assert "Source" not in json.dumps(report) and "/private/" not in capsys.readouterr().out
    assert {path.relative_to(launcher.ROOT): path.read_bytes()
            for path in launcher.ROOT.rglob("*") if path.is_file()} == before


def test_unknown_container_is_diagnostic_only_never_adopted(launcher, monkeypatch):
    _record, _container, calls = _managed(launcher, monkeypatch)
    launcher._instance_path().unlink()
    (launcher.SANDBOX / "api-session.key").unlink()
    (launcher.SANDBOX / "task-policy.json").unlink()
    _revision_bundle(launcher)
    monkeypatch.setattr(launcher, "_require_image", lambda: None)
    monkeypatch.setattr(launcher, "_planner", lambda: None)
    monkeypatch.setattr(launcher, "_template_contract", lambda: None)
    with pytest.raises(launcher.lifecycle.LifecycleError, match="container_exists_refusing_initialize"):
        launcher.start()
    assert launcher.doctor()["container"]["ownership"] == "container_not_owned"
    assert not any(args[0] in ("run", "start", "stop", "rm") for args in calls)


def test_docker_unavailable_is_not_missing_container(launcher, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(launcher, "_docker", lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(launcher.lifecycle.LifecycleError, match="docker_unavailable"):
        launcher._container_info()
    assert launcher.doctor()["container"]["status"] == "unavailable"


def test_socket_cleanup_requires_same_inode_and_unlocked_daemon(launcher):
    import fcntl

    listener = socket.socket(socket.AF_UNIX)
    listener.bind(launcher.SOCKET)
    path = Path(launcher.SOCKET)
    info = path.lstat()
    record = {"socket_identity": [info.st_dev, info.st_ino]}
    lock = path.with_name("executor.lock")
    try:
        with lock.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(launcher.lifecycle.LifecycleError, match="socket_active"):
                launcher._cleanup_owned_socket(record)
        with pytest.raises(launcher.lifecycle.LifecycleError, match="socket_unowned"):
            launcher._cleanup_owned_socket({"socket_identity": [0, 0]})
        launcher._cleanup_owned_socket(record)
        assert not path.exists() and lock.exists()
    finally:
        listener.close()


def test_mount_binding_ignores_only_inspect_list_order(launcher, monkeypatch):
    import copy
    _, container, _ = _managed(launcher, monkeypatch)
    info = container['info']
    info['mounts'].append({'Type': 'volume', 'Name': 'second', 'Source': '/second-source',
                           'Destination': '/another', 'RW': True})
    record = launcher._instance()
    record.update(mounts_sha256=launcher._mount_digest(info), mount_digest_version=2)
    reversed_info = copy.deepcopy(info)
    reversed_info['mounts'].reverse()
    assert launcher._check_container(reversed_info, record) == reversed_info
    reversed_info['mounts'][0]['Source'] = '/foreign-volume'
    with pytest.raises(launcher.lifecycle.LifecycleError, match='identity_or_mounts_changed'):
        launcher._check_container(reversed_info, record)


def test_early_v10_mount_binding_migrates_only_identical_full_mount_set(launcher, monkeypatch):
    import copy
    _, container, _ = _managed(launcher, monkeypatch)
    info = container['info']
    info['mounts'].append({'Type': 'volume', 'Name': 'second', 'Source': '/second-source',
                           'Destination': '/another', 'RW': True})
    record = launcher._instance()
    original = json.dumps(info['mounts'], sort_keys=True, separators=(',', ':'))
    record['mounts_sha256'] = hashlib.sha256(original.encode()).hexdigest()
    record.pop('mount_digest_version', None)
    reordered = copy.deepcopy(info)
    reordered['mounts'].reverse()
    assert launcher._check_container(reordered, record) == reordered
    reordered['mounts'][0]['RW'] = False
    with pytest.raises(launcher.lifecycle.LifecycleError, match='identity_or_mounts_changed'):
        launcher._check_container(reordered, record)


def test_duplicate_mount_destinations_are_rejected(launcher):
    with pytest.raises(launcher.lifecycle.LifecycleError, match='mounts_invalid'):
        launcher._mount_digest({'mounts': [{'Destination': '/data'}, {'Destination': '/data'}]})


def test_retained_setup_marker_does_not_skip_database_readiness(launcher, monkeypatch):
    from types import SimpleNamespace
    attempts = []
    def docker(*args, **kwargs):
        if args[2] == 'sh':
            assert args[-1] == launcher.local_entrypoint.READY_CHECK
            return SimpleNamespace(returncode=0, stdout='')
        assert args[-1] == 'SELECT 1;'
        attempts.append(args)
        return SimpleNamespace(returncode=0 if len(attempts)==2 else 1,
                               stdout='1\n' if len(attempts)==2 else '')
    monkeypatch.setattr(launcher, '_docker', docker)
    monkeypatch.setattr(launcher.time, 'sleep', lambda _: None)
    launcher._wait_sandbox()
    assert len(attempts) == 2


@pytest.mark.parametrize("initialized", [False, True])
def test_long_first_boot_budget_does_not_extend_resume(launcher, monkeypatch, initialized):
    from types import SimpleNamespace
    monkeypatch.setattr(launcher, '_instance', lambda **_: {'ready': initialized})
    times = iter([0, 181])
    monkeypatch.setattr(launcher.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(launcher, '_docker', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout='1\n'))
    if initialized:
        with pytest.raises(launcher.lifecycle.LifecycleError, match='sandbox_setup_timeout'):
            launcher._wait_sandbox()
    else:
        launcher._wait_sandbox()


def test_stopped_first_boot_fails_without_waiting_for_full_budget(launcher, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(launcher, '_instance', lambda **_: {'ready': False})
    def docker(*args, **kwargs):
        return SimpleNamespace(returncode=1 if args[0] == 'exec' else 0, stdout='false\n')
    monkeypatch.setattr(launcher, '_docker', docker)
    monkeypatch.setattr(launcher.time, 'sleep', lambda _: pytest.fail('must fail promptly'))
    with pytest.raises(launcher.lifecycle.LifecycleError, match='sandbox_stopped_before_ready'):
        launcher._wait_sandbox()


def test_erp_readiness_uses_bounded_read_only_probe_without_provider_credentials(launcher, monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setenv('NEBIUS_API_KEY', 'must-not-forward')
    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert 'NEBIUS_API_KEY' not in kwargs['env']
        assert 0 < kwargs['timeout'] <= 5
        assert 'search_count(domain=[])' in command[2]
        return SimpleNamespace(returncode=0 if len(calls)==2 else 1)
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    monkeypatch.setattr(launcher.time, 'sleep', lambda _: None)
    launcher._wait_erp()
    assert len(calls) == 2


def _synthetic_entrypoint(monkeypatch):
    from forgeflow import local_entrypoint
    source = b'''#!/usr/bin/env bash
set -euo pipefail
pg_ctlcluster 18 main start
su postgres -s /bin/bash -c "true"
su postgres -s /bin/bash -c "true"
export HOST=127.0.0.1
# Create database via HTTP POST (synthetic operation ledger)
echo initialize >> "$LEDGER"
echo synthetic-private-credential > /etc/odoo/api_key
rm /setup/setup_scenario.py
touch /tmp/saas_setup_complete

while true; do
  break
done
'''
    monkeypatch.setattr(local_entrypoint, "UPSTREAM_SHA256", hashlib.sha256(source).hexdigest())
    return source


def test_actual_shell_resume_skips_setup_and_credentials_and_replaces_boot_receipt(tmp_path, monkeypatch):
    import subprocess

    from forgeflow import local_entrypoint
    source = _synthetic_entrypoint(monkeypatch)
    patched, binding = local_entrypoint.preserving_source(source)
    assert local_entrypoint.preserving_source(patched) == (patched, binding)
    for name in ("tmp", "etc/odoo", "setup", "bin"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "setup/setup_scenario.py").touch()
    for name in ("pg_ctlcluster", "su"):
        stub = tmp_path / "bin" / name
        stub.write_text('#!/bin/sh\necho ' + name + ' >> "$LEDGER"\n')
        stub.chmod(0o700)
    ledger = tmp_path / "ledger"
    env = {**os.environ, "PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"],
           "LEDGER": str(ledger)}
    def relocate(code, boot):
        for prefix in ("/tmp/", "/etc/", "/setup/"):
            code = code.replace(prefix, str(tmp_path) + prefix)
        return code.replace(local_entrypoint.BOOT_ID, f"printf '{boot}'")
    script = tmp_path / "entrypoint.sh"
    def run(boot):
        script.write_text(relocate(patched.decode(), boot))
        return subprocess.run(["bash", str(script)], env=env, capture_output=True, timeout=5)
    assert run("boot-one").returncode == 0
    key = tmp_path / "etc/odoo/api_key"
    key_before = key.read_bytes(), key.stat().st_mtime_ns
    check = relocate(local_entrypoint.READY_CHECK, "boot-two")
    assert subprocess.run(["sh", "-c", check], env=env, capture_output=True).returncode == 1
    assert run("boot-two").returncode == 0
    assert subprocess.run(["sh", "-c", check], env=env, capture_output=True).returncode == 0
    assert (key.read_bytes(), key.stat().st_mtime_ns) == key_before
    assert ledger.read_text().splitlines() == ["pg_ctlcluster", "su", "su", "initialize", "pg_ctlcluster"]
    # A contradictory/incomplete retained environment cannot silently initialize.
    (tmp_path / "tmp/saas_setup_complete").unlink()
    assert run("boot-three").returncode != 0
    assert ledger.read_text().count("initialize") == 1
    assert not (tmp_path / "tmp/forgeflow_local_ready").exists()


@pytest.mark.parametrize("damage", ["original", "patched", "guard", "oversize"])
def test_entrypoint_unrecognized_bytes_never_repaired_or_disclosed(monkeypatch, damage):
    from forgeflow import local_entrypoint
    original = _synthetic_entrypoint(monkeypatch)
    patched, _ = local_entrypoint.preserving_source(original)
    bad = {"original": original + b"secret-canary", "patched": patched + b"secret-canary",
           "guard": patched.replace(local_entrypoint.OPEN, b""), "oversize": b"x" * 65537}[damage]
    with pytest.raises(local_entrypoint.LifecycleError, match="^local_entrypoint_unrecognized$"):
        local_entrypoint.preserving_source(bad)


@pytest.mark.parametrize("case", ["install", "interrupted", "running_old", "binding_changed", "wrong_entrypoint"])
def test_entrypoint_install_ownership_binding_and_interruption(launcher, monkeypatch, case):
    from types import SimpleNamespace
    function = launcher._preserving_entrypoint
    record, container, calls = _managed(launcher, monkeypatch)
    original = _synthetic_entrypoint(monkeypatch)
    patched, binding = launcher.local_entrypoint.preserving_source(original)
    content = [patched if case == "interrupted" else original]
    if case == "running_old":
        container["info"]["state"] = "running"
    if case == "binding_changed":
        record["entrypoint"] = {**binding, "installed_sha256": "0" * 64}
    installed = []
    docker = launcher._docker
    def mocked(*args, **kwargs):
        if args[0] == "inspect":
            return SimpleNamespace(stdout=json.dumps(["/different.sh"] if case == "wrong_entrypoint"
                                                    else [launcher.local_entrypoint.PATH]))
        if args[0] == "cp":
            if args[1].startswith(container["info"]["id"] + ":"):
                Path(args[2]).write_bytes(content[0])
            else:
                assert container["info"]["state"] != "running"
                assert launcher._instance()["container_id"] == container["info"]["id"]
                installed.append(Path(args[1]).read_bytes())
                content[0] = installed[-1]
            return SimpleNamespace(returncode=0)
        return docker(*args, **kwargs)
    monkeypatch.setattr(launcher, "_docker", mocked)
    if case in {"running_old", "binding_changed", "wrong_entrypoint"}:
        with pytest.raises(launcher.lifecycle.LifecycleError):
            function(record, container["info"])
        assert not installed and content[0] == original
        return
    function(record, container["info"])
    assert launcher._instance()["entrypoint"] == binding
    assert installed == ([patched] if case == "install" else [])
    function(launcher._instance(), container["info"])
    assert installed == ([patched] if case == "install" else [])


@pytest.mark.parametrize("setting,expected", [(None, True), ("1", True), ("0", False)])
@pytest.mark.parametrize("candidate_setting", [None, "1", "0"])
def test_new_managed_instance_persists_product_revision_default(
    launcher, monkeypatch, setting, expected, candidate_setting,
):
    if setting is not None:
        monkeypatch.setenv("FORGEFLOW_LOCAL_REVISIONS", setting)
    if candidate_setting is not None:
        monkeypatch.setenv("FORGEFLOW_LOCAL_CANDIDATES", candidate_setting)
    _revision_bundle(launcher, candidates=candidate_setting == "1")
    for name in ("_require_image", "_planner", "_template_contract", "_start_sandbox"):
        monkeypatch.setattr(launcher, name, lambda: None)
    monkeypatch.setattr(launcher, "_require_dedicated_revision_sandbox",
                        lambda: pytest.fail("normal product start used acceptance-only gate"))
    monkeypatch.setattr(launcher, "_container_info", lambda: None)
    monkeypatch.setattr(launcher, "_prepare_local_policy", lambda:
                        launcher.RUNTIME_POLICY.write_text('{"synthetic": true}'))
    monkeypatch.setattr(launcher, "_write_request", lambda _policy:
                        (launcher.SANDBOX / "request.json").write_text('{"synthetic": true}'))
    launcher._initialize()
    record = launcher._instance()
    assert record["features"] == {"revisions": expected, "candidates": candidate_setting == "1"}
    assert "acceptance" not in record
    monkeypatch.delenv("FORGEFLOW_LOCAL_REVISIONS", raising=False)
    monkeypatch.delenv("FORGEFLOW_LOCAL_CANDIDATES", raising=False)
    assert launcher._revision_capability(record) is expected
    assert launcher._candidate_capability(record) is (candidate_setting == "1")
    assert launcher._instance() == record


@pytest.mark.parametrize("binding", [None, {"acceptance": {"revisions": False}},
                                    {"acceptance": {"revisions": True}},
                                    {"features": {"revisions": False}},
                                    {"features": {"revisions": True}}])
def test_missing_override_retains_old_or_product_binding_without_rewriting(launcher, binding):
    record = {} if binding is None else binding
    before = json.loads(json.dumps(record))
    assert launcher._revision_capability(record) is (bool(binding) and next(iter(binding.values()))["revisions"])
    assert launcher._candidate_capability(record) is False
    assert record == before


@pytest.mark.parametrize("binding", [None, "false", {}, {"revisions": "true"},
                                    {"revisions": 1}, {"revisions": True, "unknown": False}])
def test_malformed_product_feature_binding_fails_closed(launcher, binding):
    with pytest.raises(launcher.lifecycle.LifecycleError, match="invalid_revision_binding"):
        launcher._revision_capability({"features": binding})


def test_dual_binding_is_not_silently_resolved(launcher):
    with pytest.raises(launcher.lifecycle.LifecycleError, match="ambiguous_revision_binding"):
        launcher._revision_capability({"features": {"revisions": True}, "acceptance": {"revisions": False}})


@pytest.mark.parametrize("saved,requested", [(True, "0"), (False, "1")])
def test_product_override_conflict_does_not_restart_or_touch_old_instance(launcher, monkeypatch, saved, requested):
    record, _container, calls = _managed(launcher, monkeypatch)
    record["features"] = {"revisions": saved}
    launcher.lifecycle.write_record(launcher._instance_path(), record)
    monkeypatch.setenv("FORGEFLOW_LOCAL_REVISIONS", requested)
    monkeypatch.setattr(launcher, "_stop_pid", lambda _: pytest.fail("stopped before feature validation"))
    for operation in (launcher.resume, launcher.restart_api):
        with pytest.raises(launcher.lifecycle.LifecycleError, match="revision_configuration_changed"):
            operation()
    assert launcher._instance() == record and calls == []


def test_unmanaged_api_is_not_promoted_by_new_instance_default(launcher):
    assert launcher._revision_capability(None) is True
    assert launcher._revision_capability(None, new_instance=False) is False


@pytest.mark.parametrize("saved,requested", [(None, "1"), (False, "1"), (True, "0")])
def test_candidate_override_cannot_change_existing_instance(launcher, monkeypatch, saved, requested):
    record, _container, calls = _managed(launcher, monkeypatch)
    if saved is not None:
        record["features"] = {"revisions": True, "candidates": saved}
        launcher.lifecycle.write_record(launcher._instance_path(), record)
    monkeypatch.setenv("FORGEFLOW_LOCAL_CANDIDATES", requested)
    monkeypatch.setattr(launcher, "_stop_pid", lambda _: pytest.fail("stopped before validation"))
    monkeypatch.setattr(launcher, "_require_image", lambda: pytest.fail("image checked before validation"))
    for operation in (launcher.init, launcher.resume, launcher.restart_api, launcher._start_api):
        with pytest.raises(launcher.lifecycle.LifecycleError, match="candidate_configuration_changed"):
            operation()
    assert launcher._instance() == record and calls == []


@pytest.mark.parametrize("value", [None, "true", 1, [], {}])
def test_invalid_candidate_binding_never_enables_a_feature(launcher, value):
    record = {"features": {"revisions": True, "candidates": value}}
    for resolver in (launcher._revision_capability, launcher._candidate_capability):
        with pytest.raises(launcher.lifecycle.LifecycleError, match="invalid_candidate_binding"):
            resolver(record)


@pytest.mark.parametrize("damage", [None, "old_bundle", "changed_bytes", "missing_file"])
def test_candidate_bundle_is_checked_without_repairing_retained_sources(launcher, damage):
    _revision_bundle(launcher, candidates=damage != "old_bundle")
    path = launcher.SANDBOX / "planner/forgeflow/recovery/product_candidates.py"
    if damage == "changed_bytes":
        path.write_text("# changed\n")
    elif damage == "missing_file":
        path.unlink()
    before = {str(p): p.read_bytes() for p in (launcher.SANDBOX / "planner").rglob("*") if p.is_file()}
    if damage is None:
        launcher._check_candidate_bundle(True)
    else:
        with pytest.raises(launcher.lifecycle.LifecycleError, match="candidate_bound_bundle_unsupported"):
            launcher._check_candidate_bundle(True)
    launcher._check_candidate_bundle(False)
    assert before == {str(p): p.read_bytes() for p in (launcher.SANDBOX / "planner").rglob("*") if p.is_file()}


def test_candidate_bundle_validation_precedes_api_stop(launcher, monkeypatch):
    record, _container, calls = _managed(launcher, monkeypatch)
    record["features"] = {"revisions": True, "candidates": True}
    launcher.lifecycle.write_record(launcher._instance_path(), record)
    _revision_bundle(launcher)
    monkeypatch.setattr(launcher, "_stop_pid", lambda _: pytest.fail("API stopped before validation"))
    for operation in (launcher.resume, launcher.restart_api, launcher._start_api):
        with pytest.raises(launcher.lifecycle.LifecycleError, match="candidate_bound_bundle_unsupported"):
            operation()
    assert launcher._instance() == record and calls == []


def test_candidate_flag_cannot_upgrade_unmanaged_api(launcher, monkeypatch):
    assert launcher._candidate_capability(None) is False
    monkeypatch.setenv("FORGEFLOW_LOCAL_CANDIDATES", "1")
    monkeypatch.setattr(launcher, "_read_pid", lambda _: pytest.fail("process inspected before validation"))
    for operation in (launcher._start_api, launcher.restart_api):
        with pytest.raises(launcher.lifecycle.LifecycleError, match="candidates_require_managed_sandbox"):
            operation()
    assert not launcher._instance_path().exists()


def test_candidate_override_does_not_prevent_preserving_stop(launcher, monkeypatch):
    record, container, _calls = _managed(launcher, monkeypatch, state="running")
    record["features"] = {"revisions": True, "candidates": True}
    launcher.lifecycle.write_record(launcher._instance_path(), record)
    monkeypatch.setenv("FORGEFLOW_LOCAL_CANDIDATES", "invalid")
    monkeypatch.setattr(launcher, "_stop_pid", lambda _: None)
    launcher.stop()
    assert container["info"]["state"] == "exited"
    assert launcher._instance()["features"] == record["features"]


def test_candidate_profile_never_upgrades_a_retained_instance(launcher, monkeypatch):
    from forgeflow.planning.objectives import CAPACITY_CANDIDATE_PROFILE, LEGACY_CANDIDATE_PROFILE

    record = {"features": {"revisions": True, "candidates": True}}
    assert launcher._candidate_profile(record, True) == LEGACY_CANDIDATE_PROFILE
    monkeypatch.setenv("FORGEFLOW_LOCAL_CANDIDATE_PROFILE", CAPACITY_CANDIDATE_PROFILE)
    with pytest.raises(launcher.lifecycle.LifecycleError, match="candidate_profile_changed"):
        launcher._candidate_profile(record, True)
    assert launcher._candidate_profile(None, True) == CAPACITY_CANDIDATE_PROFILE
    with pytest.raises(launcher.lifecycle.LifecycleError, match="candidate_profile_requires_candidates"):
        launcher._candidate_profile(None, False)
    assert "candidate_profile" not in record


def test_candidate_capacity_profile_requires_explicit_bundle_support(launcher):
    from forgeflow.planning.objectives import CAPACITY_CANDIDATE_PROFILE, PRODUCT_PROFILES

    _revision_bundle(launcher, candidates=True)
    path = launcher.SANDBOX / "planner/provenance.json"
    original = path.read_bytes()
    with pytest.raises(launcher.lifecycle.LifecycleError, match="candidate_bound_bundle_unsupported"):
        launcher._check_candidate_bundle(True, CAPACITY_CANDIDATE_PROFILE)
    assert path.read_bytes() == original
    record = json.loads(original)
    record["product_candidate_profiles"] = list(PRODUCT_PROFILES)
    path.write_text(json.dumps(record))
    launcher._check_candidate_bundle(True, CAPACITY_CANDIDATE_PROFILE)


@pytest.mark.parametrize("operation", ["_resume", "restart_api", "_start_api"])
def test_profile_mismatch_is_checked_before_stopping_or_starting_services(launcher, monkeypatch, operation):
    from forgeflow.planning.objectives import CAPACITY_CANDIDATE_PROFILE

    record, _, _ = _managed(launcher, monkeypatch)
    record["features"] = {"revisions": True, "candidates": True}
    launcher._save_instance(record, "test")
    monkeypatch.setenv("FORGEFLOW_LOCAL_CANDIDATE_PROFILE", CAPACITY_CANDIDATE_PROFILE)
    for name in ("_stop_pid", "_start_sandbox", "_start_executor"):
        monkeypatch.setattr(launcher, name, lambda *a: pytest.fail("mutated services"))
    with pytest.raises(launcher.lifecycle.LifecycleError, match="candidate_profile_changed"):
        getattr(launcher, operation)()
