"""Host scheduling failure boundaries. Fake launchers never contact a model."""

import copy
import importlib.util
import json
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("continuation_schedule_test", ROOT / "scripts/continuation_schedule.py")
# Standalone scripts use sibling imports exactly as their CLI entry does.
sys.path.insert(0, str(ROOT / "scripts"))
schedule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schedule)
sys.path.pop(0)


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    monkeypatch.setattr(schedule, "implementation_hashes", lambda: {"src/one.py": "a" * 64})
    monkeypatch.setattr(schedule.evaluator, "source_hashes", lambda: {"src/one.py": "a" * 64})
    models = {"providers": {"nebius": {"apiKey": "$NEBIUS_API_KEY", "models": [{"id": "test-only"}]}}}
    model = {"models": models, "image_id": "sha256:" + "d" * 64,
             "image": schedule.evaluator.IMAGE, "thinking": "provider-default", "cost_usd": None}
    monkeypatch.setattr(schedule, "safe_model_configuration", lambda: copy.deepcopy(model))
    monkeypatch.setattr(schedule, "credentials", lambda: ())
    freeze_file = tmp_path / "freeze.json"
    schedule.write(freeze_file, schedule.freeze_manifest("unit-batch"))
    # Exposed synthetic IDs only; not a heldout recipe or future case generation.
    suite_file = tmp_path / "known-inputs.json"
    schedule.write(suite_file, {"schema_version": 1, "split": "heldout", "suite_id": "synthetic-unit-test",
        "generated_at": time.time(), "freeze_sha256": schedule.file_sha(freeze_file),
        "cases": [{"case_id": f"known-{i}"} for i in range(6)]})
    binding_file = tmp_path / "binding.json"
    schedule.write(binding_file, schedule.bind_suite(freeze_file, suite_file))
    plan_file = tmp_path / "plan.json"
    schedule.write(plan_file, schedule.make_plan(binding_file, tmp_path / "batch"))
    return plan_file, freeze_file, suite_file, binding_file


def test_freeze_without_suite_then_exact_30_case_chains(frozen):
    plan_file, freeze_file, _, _ = frozen
    freeze, plan = schedule.read(freeze_file), schedule.read(plan_file)
    assert freeze["suite_bound"] is False
    assert freeze["configuration"] == schedule.evaluator.configuration()
    assert freeze["models_config"]["providers"]["nebius"]["apiKey"] == "$NEBIUS_API_KEY"
    assert freeze["image_id"].startswith("sha256:")
    assert len(plan["jobs"]) == len({j["attempt_id"] for j in plan["jobs"]}) == 30
    assert {arm: sum(j["arm"] == arm for j in plan["jobs"]) for arm in schedule.ARMS} == {
        "rules": 6, "fixed": 12, "interactive": 12}
    assert {j["replicate"] for j in plan["jobs"] if j["arm"] == "rules"} == {1}
    assert {j["replicate"] for j in plan["jobs"] if j["arm"] != "rules"} == {1, 2}


@pytest.mark.parametrize("change", ["old_generation", "wrong_freeze", "development", "five_cases", "duplicate"])
def test_suite_binding_rejects_old_wrong_or_reshaped_data(frozen, change):
    _, freeze_file, suite_file, _ = frozen
    value = schedule.read(suite_file)
    if change == "old_generation":
        value["generated_at"] = schedule.read(freeze_file)["created_at"] - 1
    elif change == "wrong_freeze":
        value["freeze_sha256"] = "f" * 64
    elif change == "development":
        value["split"] = "development"
    elif change == "five_cases":
        value["cases"].pop()
    else:
        value["cases"][-1] = value["cases"][0]
    schedule.write(suite_file, value, replace=True)
    with pytest.raises(ValueError, match="heldout_must"):
        schedule.bind_suite(freeze_file, suite_file)


def test_freeze_detects_sources_limits_and_model_drift(frozen, monkeypatch):
    _, freeze_file, _, _ = frozen
    value = schedule.read(freeze_file)
    for key in ("sources", "configuration", "models_config", "image_id", "limits"):
        changed = copy.deepcopy(value)
        changed[key] = "changed"
        with pytest.raises((ValueError, TypeError), match="frozen|indices"):
            schedule.check_freeze(changed)
    monkeypatch.setattr(schedule, "implementation_hashes", lambda: {"src/one.py": "b" * 64})
    with pytest.raises(ValueError, match="frozen"):
        schedule.check_freeze(value)


def fake_result(command):
    def arg(flag):
        return command[command.index(flag) + 1]

    out, frozen = Path(arg("--out")), schedule.read(arg("--freeze"))
    suite = Path(arg("--suite"))
    arm, identity = arg("--arm"), arg("--case")
    out.mkdir(parents=True)
    schedule.write(out / "dataset.json", {"sha256": schedule.file_sha(suite), "split": "heldout", "case_id": identity})
    schedule.write(out / "source-hashes.json", frozen["sources"])
    schedule.write(out / "configuration.json", {**frozen["configuration"], "arm": arm})
    schedule.write(out / "result.json", {"case_id": identity, "arm": arm, "passed": True,
        "terminal": "needs_clarification", "checks": {"source_stable": True, "no_business_writes": True},
        "usage": {key: 0 for key in schedule.USAGE_FIELDS}, "known_usage": {"tokens": 0},
        "interaction": {"initial_model_question_ticket_correct": True, "clarification_rounds": 1},
        "provider_metrics": {"provider_responses": 0}})


def test_thirty_attempts_at_most_two_launches_and_resume_never_repeats(frozen, monkeypatch):
    plan_file, _, _, _ = frozen
    active = maximum = launches = 0
    mutex = threading.Lock()

    class FakeProcess:
        pid = 999999

        def __init__(self, command, **kwargs):
            nonlocal active, maximum, launches
            self.command = command
            with mutex:
                active += 1
                launches += 1
                maximum = max(maximum, active)

        def wait(self):
            nonlocal active
            time.sleep(0.005)
            fake_result(self.command)
            with mutex:
                active -= 1
            return 0

    monkeypatch.setattr(schedule.subprocess, "Popen", FakeProcess)
    state = schedule.run_batch(plan_file)
    assert state["state"] == "finished" and launches == 30 and maximum <= 2
    assert all(r["launch_count"] == 1 and "archive_sha256" in r for r in state["attempts"].values())
    assert schedule.run_batch(plan_file)["state"] == "finished" and launches == 30
    result = schedule.summarize(plan_file)
    assert result["denominator"] == 30 and result["evidence_complete"]
    assert {arm: row["passed"] for arm, row in result["arms"].items()} == {"rules": 6, "fixed": 12, "interactive": 12}


def test_launcher_failure_stays_in_denominator_unknown_and_archived(frozen, monkeypatch):
    plan_file, _, _, _ = frozen
    monkeypatch.setattr(schedule.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("no launcher")))
    state = schedule.run_batch(plan_file)
    result = schedule.summarize(plan_file)
    assert state["state"] == "finished"
    assert all(r["launch_count"] == 1 and "archive_sha256" in r for r in state["attempts"].values())
    assert sum(row["failed"] for row in result["arms"].values()) == 30
    assert all(row["usage"]["tokens"]["known_attempts"] == 0 for row in result["arms"].values())
    assert all(row["result"]["zero_writes"] is None and row["result"]["cost_usd"] is None for row in result["attempts"])


@pytest.mark.parametrize("persisted_status", [True, False])
def test_started_and_crash_before_status_both_require_reconciliation(frozen, monkeypatch, persisted_status):
    plan_file, _, _, _ = frozen
    plan = schedule.read(plan_file)
    directory = schedule.ROOT / plan["output_root"]
    directory.mkdir()
    state = schedule._load_state(plan_file, plan, directory)
    job = plan["jobs"][0]
    launch = directory / "receipts" / job["attempt_id"] / "launch.json"
    schedule.write(launch, {"started_at": 1})
    if persisted_status:
        state["attempts"][job["attempt_id"]].update(phase="started", launch_count=1, launch_sha256=schedule.file_sha(launch))
        schedule.write(directory / "status.json", state, replace=True)
    monkeypatch.setattr(schedule.subprocess, "Popen", lambda *a, **k: pytest.fail("Uncertain attempts cannot be relaunched"))
    with pytest.raises(ValueError, match="require_host_reconciliation"):
        schedule.run_batch(plan_file)
    assert schedule.read(directory / "status.json")["attempts"][job["attempt_id"]]["phase"] == "needs_reconciliation"
    receipt = schedule.ROOT / "review.json"
    evidence = schedule.ROOT / "external-host-inspection.json"
    evidence.write_text('{"quiescence":"confirmed by test host"}\n')
    schedule.write(receipt, {"kind": "host_worker_reconciliation", "attempt_id": job["attempt_id"],
        "launch_sha256": schedule.file_sha(launch), "quiescence": "confirmed",
        "observed_elapsed_seconds": 10, "evidence": [{"file": "external-host-inspection.json", "sha256": schedule.file_sha(evidence)}]})
    result = schedule.reconcile_attempt(plan_file, job["attempt_id"], receipt)
    assert result["passed"] is False and result["usage"]["tokens"] is None
    assert result["elapsed_basis"] == "lower_bound"
    summary = schedule.summarize(plan_file)
    assert summary["arms"]["rules"]["denominator"] == 6 and summary["arms"]["rules"]["completed"] == 1
    assert summary["arms"]["rules"]["latency"]["complete_count"] == 0
    assert summary["arms"]["rules"]["latency"]["lower_bounds"] == [10]


def test_wait_exception_cannot_free_a_potentially_live_worker_slot(frozen, monkeypatch):
    plan_file, _, _, _ = frozen

    class LostWait:
        pid = 888888

        def __init__(self, *a, **k):
            pass

        def wait(self):
            raise OSError("connection lost")

    monkeypatch.setattr(schedule.subprocess, "Popen", LostWait)
    with pytest.raises(RuntimeError, match="quiescence_unknown"):
        schedule.run_batch(plan_file, concurrency=1)
    state = schedule.read(schedule.ROOT / "batch/status.json")
    assert sum(r["launch_count"] for r in state["attempts"].values()) == 1
    assert sum(r["phase"] == "needs_reconciliation" for r in state["attempts"].values()) == 1


def test_archive_retains_original_bytes_and_excludes_live_bridge_private_env(frozen):
    run = schedule.ROOT / "one-run"
    run.mkdir()
    original = b'{"case_id":"known","passed":false}\n'
    (run / "result.json").write_bytes(original)
    private = run / "trusted/cases/ic-test/workers/episode-1/runtime-inputs"
    private.mkdir(parents=True)
    (private / "bridge.json").write_text('{"token":"DO-NOT-ARCHIVE"}')
    (run / ".env").write_text("SECRET=DO-NOT-ARCHIVE")
    index = schedule.archive_attempt(run, schedule.ROOT / "archive", secret_values=(b"DO-NOT-ARCHIVE",))
    with tarfile.open(schedule.ROOT / "archive/attempt.tar.gz") as archive:
        assert archive.getnames() == ["result.json"]
        assert archive.extractfile("result.json").read() == original
    assert index["members"][0]["sha256"] == schedule.sha(original)
    with pytest.raises(ValueError, match="output_exists"):
        schedule.archive_attempt(run, schedule.ROOT / "archive")


@pytest.mark.parametrize("bad", ["credential", "path", "symlink", "fake_prepared"])
def test_archive_rejects_sensitive_or_unbound_claims(frozen, bad):
    run = schedule.ROOT / "one-run"
    run.mkdir()
    if bad == "credential":
        (run / "result.json").write_text('{"secret":"actual-secret-1234"}')
    elif bad == "path":
        (run / "result.json").write_text('{"path":"/Users/private/work"}')
    elif bad == "fake_prepared":
        (run / "result.json").write_text('{"terminal":"prepared"}')
    else:
        external = schedule.ROOT / "elsewhere.json"
        external.write_text('{}')
        (run / "result.json").symlink_to(external)
    with pytest.raises(ValueError):
        schedule.archive_attempt(run, schedule.ROOT / "archive", secret_values=(b"actual-secret-1234",))


def test_result_from_other_case_or_dataset_is_not_accepted(frozen):
    plan_file, _, _, binding_file = frozen
    plan, binding = schedule.read(plan_file), schedule.read(binding_file)
    job = plan["jobs"][0]
    command = schedule.command_for(plan, job, binding)
    fake_result(command)
    run = Path(command[command.index("--out") + 1])
    raw = (run / "result.json").read_bytes()
    schedule.write(run / "dataset.json", {"sha256": "f" * 64, "split": "heldout", "case_id": job["case_id"]}, replace=True)
    result = schedule.attempt_result(run, job, returncode=0, elapsed=1, binding=binding)
    assert not result["passed"] and result["usage"]["tokens"] is None
    assert result["observed_prefix"]["usage"]["tokens"] == 0
    assert (run / "result.json").read_bytes() == raw


def test_archive_rejects_nested_bundle_secret_and_second_archive(frozen):
    import io

    run = schedule.ROOT / "run"
    (run / "trusted").mkdir(parents=True)
    for name, body in [("planner/readme.txt", b"actual-secret-1234"), ("planner/deep.zip", b"PK\x03\x04nested")]:
        with tarfile.open(run / "trusted/bundle.tar.gz", "w:gz") as archive:
            item = tarfile.TarInfo(name)
            item.size = len(body)
            archive.addfile(item, io.BytesIO(body))
        with pytest.raises(ValueError):
            schedule.archive_attempt(run, schedule.ROOT / "archive", secret_values=(b"actual-secret-1234",))


def test_actual_freeze_binding_command_and_real_rules_evaluator_archive(monkeypatch):
    """Replay exposed dev-01 only. No unseen case, Docker launch or provider."""
    from forgeflow.agents.pi_nebius import build_models_config
    from forgeflow.config import load_config

    config = load_config(ROOT, require_model=False)
    models = build_models_config(config, config.model)
    monkeypatch.setattr(schedule, "safe_model_configuration", lambda: {
        "models": models, "image": schedule.evaluator.IMAGE, "image_id": "sha256:" + "d" * 64,
        "thinking": "provider-default", "cost_usd": None})
    with tempfile.TemporaryDirectory(prefix="schedule-control-", dir=ROOT / ".runtime") as folder:
        folder = Path(folder)
        freeze_file = folder / "freeze.json"
        schedule.write(freeze_file, schedule.freeze_manifest("known-development"))
        known = json.loads((ROOT / "data/continuation/development-v1.json").read_text())["cases"]
        suite = {"schema_version": 1, "split": "heldout", "suite_id": "exposed-development-integration-only",
                 "freeze_sha256": schedule.file_sha(freeze_file), "generated_at": time.time(), "cases": known}
        suite_file, binding_file, plan_file = folder / "known.json", folder / "binding.json", folder / "plan.json"
        schedule.write(suite_file, suite)
        schedule.write(binding_file, schedule.bind_suite(freeze_file, suite_file))
        plan = schedule.make_plan(binding_file, folder / "batch")
        schedule.write(plan_file, plan)
        binding = schedule.check_plan(plan)
        job = plan["jobs"][0]
        command = schedule.command_for(plan, job, binding)
        assert command[command.index("--arm") + 1] == "rules"
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        run = ROOT / plan["output_root"] / "attempts" / job["attempt_id"]
        verified = schedule.attempt_result(run, job, returncode=0, elapsed=1, binding=binding)
        assert verified["passed"] and verified["usage"]["tokens"] == 0
        index = schedule.archive_attempt(run, folder / "archive", secret_values=schedule.credentials())
        assert len(index["prepared_bindings"]) == 1
        assert len(index["prepared_bindings"][0]["artifact_sha256"]) == 7
        assert all("runtime-inputs" not in row["path"] for row in index["members"])
        # Mutate a real prepared artifact, keeping its manifest/review unchanged.
        plan_file = next(run.glob("trusted/cases/*/episodes/*/store/*/plan.json"))
        plan_file.write_text('{}')
        with pytest.raises(ValueError, match="prepared_artifact_binding"):
            schedule.archive_attempt(run, folder / "corrupt-archive")


def test_frozen_sources_include_pinned_solver_and_dependency_inputs():
    hashes = schedule.implementation_hashes()
    assert {"upstream.lock.json", "vendor/erp-bench.source.json", "build-inputs/planner/requirements.txt",
            "vendor/erp-bench/erp_bench/procurement/solver.py"} <= set(hashes)
    assert "scripts/continuation_schedule.py" in hashes
