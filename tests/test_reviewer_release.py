"""Release drift and interrupted QA must not be reported as one successful run."""
import copy
import importlib.util
import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    SPEC = importlib.util.spec_from_file_location("reviewer_acceptance_test", SCRIPTS / "vps_reviewer_acceptance.py")
    acceptance = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(acceptance)
    import reviewer_release as binding
finally:
    sys.path.pop(0)


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def release(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    driver = tmp_path / "scripts/vps_reviewer_acceptance.py"
    driver.parent.mkdir()
    driver.write_text("# capture\n")
    package = tmp_path / "src/forgeflow/__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("# package\n")
    (tmp_path / ".gitignore").write_text("src/forgeflow/api/static/app/\n")
    frontend = tmp_path / "src/forgeflow/api/static/app"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text('<script src="/app/a.js"></script>')
    (frontend / "a.js").write_text("console.log('release');")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "fixture")
    revision = git(tmp_path, "rev-parse", "HEAD")
    original = binding.command

    def probe(*args, cwd):
        return str(package) if args[0] == "installed-python" else original(*args, cwd=cwd)

    monkeypatch.setattr(binding, "command", probe)
    configs = {name: {"repo_root": str(tmp_path), "python": "installed-python"} for name in ("qa1", "qa2")}
    return configs, revision, driver


def test_clean_revision_includes_full_source_and_ignored_frontend(release):
    configs, revision, driver = release
    value = binding.snapshot(configs, revision, driver=driver)
    assert value["git_revision"] == revision
    assert "src/forgeflow/__init__.py" in value["source_sha256"]
    assert set(value["frontend_sha256"]) == {"index.html", "a.js"}
    assert value["runtime_paths"]["qa1"]["package_path"].endswith("src/forgeflow/__init__.py")


@pytest.mark.parametrize("mutation", ["revision", "dirty", "untracked", "missing_frontend", "other_checkout", "installed_package"])
def test_wrong_revision_sources_build_or_installed_package_fail_before_actions(release, monkeypatch, mutation):
    configs, revision, driver = release
    root = driver.parent.parent
    if mutation == "revision":
        revision = "0" * 40
    elif mutation == "dirty":
        driver.write_text("changed")
    elif mutation == "untracked":
        (root / "src/forgeflow/extra.py").write_text("added")
    elif mutation == "missing_frontend":
        (root / "src/forgeflow/api/static/app/index.html").unlink()
    elif mutation == "other_checkout":
        configs["qa2"]["repo_root"] = str(root / "other")
    else:
        original = binding.command
        monkeypatch.setattr(binding, "command", lambda *args, cwd:
                            "/old/package/__init__.py" if args[0] == "installed-python"
                            else original(*args, cwd=cwd))
    with pytest.raises(RuntimeError):
        binding.snapshot(configs, revision, driver=driver)


def test_frontend_rebuild_is_detected_even_when_git_stays_clean(release):
    configs, revision, driver = release
    before = binding.snapshot(configs, revision, driver=driver)
    (driver.parent.parent / "src/forgeflow/api/static/app/a.js").write_text("changed")
    after = binding.snapshot(configs, revision, driver=driver)
    assert before["source_sha256"] == after["source_sha256"]
    with pytest.raises(RuntimeError, match="acceptance_release_changed"):
        binding.check(before, after)


@pytest.fixture
def stage_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, "STATE", tmp_path / "capture.json")
    frozen = {"git_revision": "a" * 40, "source_sha256": {"file": "b" * 64}}
    monkeypatch.setattr(acceptance, "release_snapshot", lambda *_a, **_kw: copy.deepcopy(frozen))
    monkeypatch.setattr(acceptance, "Sampler", lambda _c: nullcontext(SimpleNamespace(samples=[], errors=0)))
    monkeypatch.setattr(acceptance, "check_generations", lambda *_a: None)
    state = {"schema": 2, "instances": {}, "resources": {}, "stages": {}}
    index = {name: {"name": name} for name in ("qa1", "qa2")}
    return state, index, frozen


def run_stage(stage, fixture):
    state, index, _frozen = fixture
    acceptance.execute_stage(stage, state, index, {name: {} for name in index}, "host", "a" * 40)


def test_stage_order_and_replay_guard_preserve_original_capture(stage_runner, monkeypatch):
    state, _index, _frozen = stage_runner
    monkeypatch.setattr(acceptance, "prepare", lambda *_a: {"prepared": {"case_id": "case"}})
    with pytest.raises(RuntimeError, match="previous_stage_not_passed"):
        run_stage("approve", stage_runner)
    assert not acceptance.STATE.exists()
    run_stage("prepare", stage_runner)
    assert state["stages"]["prepare"]["status"] == "passed"
    original = acceptance.STATE.read_bytes()
    with pytest.raises(RuntimeError, match="already_attempted"):
        run_stage("prepare", stage_runner)
    assert acceptance.STATE.read_bytes() == original


@pytest.mark.parametrize("when", ["before", "after"])
def test_drift_before_stage_blocks_operation_after_stage_keeps_evidence(stage_runner, monkeypatch, when):
    state, _index, frozen = stage_runner
    state["stages"]["prepare"] = {"status": "passed"}
    state["release"] = copy.deepcopy(frozen)
    changed = {**frozen, "git_revision": "c" * 40}
    probes = iter([changed] if when == "before" else [frozen, changed])
    monkeypatch.setattr(acceptance, "release_snapshot", lambda *_a, **_kw: next(probes))
    calls = []
    monkeypatch.setattr(acceptance, "model", lambda *_a: calls.append(1) or {"assistant": {"answer": "response"}})
    with pytest.raises(RuntimeError, match="acceptance_release_changed"):
        run_stage("model", stage_runner)
    saved = json.loads(acceptance.STATE.read_text())
    assert saved["stages"]["model"]["status"] == "failed"
    assert saved["release"] == frozen
    assert len(calls) == (0 if when == "before" else 2)
    if when == "after":
        assert saved["instances"]["qa1"]["assistant"]["answer"] == "response"


def test_one_worker_failure_keeps_both_partial_receipt_and_other_result(stage_runner, monkeypatch):
    def operation(item, _config, _host, _result, checkpoint):
        checkpoint({"prepared": {"case_id": item["name"]}})
        if item["name"] == "qa1":
            raise RuntimeError("post_response_check_failed")
        return {"validated": True}

    monkeypatch.setattr(acceptance, "prepare", operation)
    with pytest.raises(RuntimeError, match="instance_failed"):
        run_stage("prepare", stage_runner)
    saved = json.loads(acceptance.STATE.read_text())
    assert saved["instances"]["qa1"]["prepared"]["case_id"] == "qa1"
    assert saved["instances"]["qa2"]["validated"] is True
    assert saved["stages"]["prepare"]["status"] == "failed"
    with pytest.raises(RuntimeError, match="previous_stage_not_passed"):
        run_stage("approve", stage_runner)


def test_legacy_capture_is_not_upgraded_or_reused(stage_runner):
    stage_runner[0]["schema"] = 1
    with pytest.raises(RuntimeError, match="legacy_capture_preserved"):
        run_stage("prepare", stage_runner)
    assert not acceptance.STATE.exists()


@pytest.mark.parametrize("mutation", ["instance_name", "api_port", "api_user", "dsh_user", "service_unit", "shared_case_root"])
def test_misrouted_or_shared_qa_targets_are_rejected(mutation):
    configs = {name: {"instance_name": name, "api_port": 8106 + i,
                      "api_user": "forgeflow-api-" + name, "dsh_user": "forgeflow-dsh-" + name,
                      "service_unit": f"forgeflow-runtime@{name}.service",
                      **{field: name + field for field in ("container", "sandbox_root", "case_root", "socket")}}
               for i, name in enumerate(("qa1", "qa2"))}
    index = {name: {"port": config["api_port"]} for name, config in configs.items()}
    acceptance.validate_targets(index, configs)
    if mutation == "shared_case_root":
        configs["qa1"]["case_root"] = configs["qa2"]["case_root"]
    else:
        configs["qa1"][mutation] = "judge1"
    with pytest.raises(RuntimeError):
        acceptance.validate_targets(index, configs)


@pytest.mark.parametrize("changed", ["generation", "owned_by_me"])
def test_rotation_or_owner_change_rejected_before_next_stage(monkeypatch, changed):
    environment = {"generation": "initial", "owned_by_me": True}
    monkeypatch.setattr(acceptance, "client_for", lambda *_a: nullcontext(object()))
    monkeypatch.setattr(acceptance, "request", lambda *_a: environment)
    acceptance.check_generations({"qa1": {}}, "host", {"qa1": {"generation": "initial"}})
    environment[changed] = "rotated" if changed == "generation" else False
    with pytest.raises(RuntimeError, match="generation_or_owner_changed"):
        acceptance.check_generations({"qa1": {}}, "host", {"qa1": {"generation": "initial"}})


def test_extra_generation_checks_reuse_login_without_cross_instance_cookie(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.path == "/api/auth/session":
            return httpx.Response(201, headers={"Set-Cookie": f"ff_session=qa-{request.url.port}; Path=/"})
        return httpx.Response(200, json={})

    client_type = httpx.Client
    monkeypatch.setattr(acceptance.httpx, "Client", lambda **kwargs:
                        client_type(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(acceptance, "_SESSION_COOKIES", {})
    for port in (8106, 8106, 8107, 8107):
        with acceptance.client_for({"port": port, "invite_code": f"private-{port}"}, "host") as client:
            client.get("/api/environment")
    assert len([r for r in calls if r.url.path == "/api/auth/session"]) == 2
    for request in calls:
        if request.url.path == "/api/environment":
            assert request.headers["Cookie"] == f"ff_session=qa-{request.url.port}"


def test_capture_loader_accepts_large_private_results_and_rejects_exposed_files(tmp_path, monkeypatch):
    path = tmp_path / "capture.json"
    value = {"resources": "x" * 100000}
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    original = acceptance.os.fstat

    def root_owned(fd):
        info = original(fd)
        return SimpleNamespace(st_uid=0, st_mode=info.st_mode, st_size=info.st_size)

    monkeypatch.setattr(acceptance.os, "fstat", root_owned)
    assert acceptance.load_capture(path) == value
    path.chmod(0o644)
    with pytest.raises(ValueError, match="untrusted_acceptance_capture"):
        acceptance.load_capture(path)
