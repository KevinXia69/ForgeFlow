"""Structured evidence harness boundaries; no provider, Docker or live ERP calls."""
import copy
import importlib
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.contract import contract_from_payload
from forgeflow.investigation.cases import InvestigationCases
from forgeflow.investigation.fixtures import FixtureConnection
from forgeflow.investigation.intake import task_template
from forgeflow.investigation.policy import PartialPolicy
from forgeflow.investigation.rules import run_rules
from forgeflow.investigation.service import InvestigationService, Limits
from forgeflow.investigation.sources import OdooSource
from forgeflow.recovery.storage import atomic_json, digest, read_json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
evaluate = importlib.import_module("structured_recovery_evaluate")
schedule = importlib.import_module("structured_recovery_schedule")
live = importlib.import_module("structured_recovery_live")
archive = importlib.import_module("structured_recovery_archive")
SUITE = read_json(ROOT / "data/structured-recovery-v1/default-flows-v1.json")


def test_exact_three_flows_two_independent_attempts_and_new_sources_bound():
    jobs = schedule.layout(SUITE)
    assert len(jobs) == len({row["attempt_id"] for row in jobs}) == 6
    assert {flow: sum(row["flow"] == flow for row in jobs) for flow in evaluate.FLOWS} == dict.fromkeys(evaluate.FLOWS, 2)
    hashes = evaluate.source_hashes()
    assert all("scripts/" + name in hashes for name in ("structured_recovery_evaluate.py", "structured_recovery_schedule.py",
        "structured_recovery_live.py", "structured_recovery_archive.py"))
    assert evaluate.configuration()["case_limits"]["tokens"] == 6_000_000
    assert evaluate.configuration()["episode_allowance"]["tokens"] == 2_000_000


def waiting_case(case, tmp_path):
    connection = FixtureConnection(case["records"])
    contract = contract_from_payload(case["contract"])
    policy = evaluate.legacy.missing_policy(contract, case["missing_order_id"])
    def source(value):
        return OdooSource(connection, value.source_policy() if isinstance(value, PartialPolicy) else value)
    event = {"text": case["event"], "sha256": evaluate.hashlib.sha256(case["event"].encode()).hexdigest()}
    cases = InvestigationCases(tmp_path / "cases", source_factory=source, generation=lambda: "fixture-" + digest(str(tmp_path))[:20],
        currency_probe=lambda _: {"currency": "USD", "company_id": 1, "provenance": "fixture", "record_currencies": {"orders": "USD"}})
    view = cases.create("owner", operation_id="create-" + digest(str(tmp_path))[:20], template=task_template(contract), policy=policy, event=event,
                        decision_input_mode="structured_budget_v1")
    config = cases.episode_config(view["case_id"], "owner", view["current_episode"])
    service = InvestigationService(source=source(policy), contract=policy, event=event, store=config["store"],
        bundle=tmp_path / "unused", connection_factory=lambda: connection)
    service.dispatch(config["run_id"], "finish", {"status": "needs_clarification", "reason": "Missing authorized cap",
        "question": "What budget is authorized for SO-1?"})
    cases.record_terminal(view["case_id"], "owner", view["current_episode"])
    return cases, view["case_id"], connection


@pytest.mark.parametrize("case", SUITE["cases"], ids=lambda row: row["flow"])
def test_typed_driver_uses_real_candidate_controls_confirmation_and_no_reply_worker(case, tmp_path):
    cases, identity, connection = waiting_case(case, tmp_path)
    before = connection.fingerprint()
    trace = evaluate.drive_decisions(cases, identity, "owner", case["actions"])
    final = read_json(cases._directory(identity) / "case.json")
    assert all(evaluate.confirmed_scope(case["contract"], case["missing_order_id"], case["expected"]["budget"], final).values())
    assert final["replies"] == [] and not any(work["kind"] == "reply" for work in final["work"].values())
    assert connection.fingerprint() == before and not connection.write_attempts
    assert len(trace) == len(case["actions"]) and all(row["model_credit"] is False for row in trace)
    if case["flow"] != "normal":
        assert any(row["receipt"].get("rejected") is True for row in trace)
        assert final["draft_history"]


def test_repetitions_do_not_share_case_or_policy_state(tmp_path):
    case = SUITE["cases"][0]
    first, one, conn_one = waiting_case(case, tmp_path / "one")
    second, two, conn_two = waiting_case(case, tmp_path / "two")
    assert one != two and conn_one is not conn_two and conn_one.fingerprint() == conn_two.fingerprint()
    evaluate.drive_decisions(first, one, "owner", case["actions"])
    assert second.get(two, "owner")["confirmation"] is None
    assert len(second.get(two, "owner")["episodes"]) == 1


def test_literal_preflight_checks_all_three_supported_shapes_without_method_or_model(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    suite_path = tmp_path / "suite.json"
    atomic_json(suite_path, SUITE)
    result = schedule.preflight(suite_path, tmp_path / "preflight")
    assert result["passed"] and len(result["results"]) == 3
    assert all(row["model_called"] is False and row["method_tested"] is False and row["checks"]["no_writes"] for row in result["results"])


def frozen_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    monkeypatch.setattr(evaluate, "source_hashes", lambda: {"source.py": schedule.file_sha(tmp_path / "source.py")})
    monkeypatch.setattr(evaluate, "model_configuration", lambda: {"image_id": "local-no-provider"})
    (tmp_path / "source.py").write_text("source = 1\n")
    (tmp_path / "plan.md").write_text("Frozen six plus one plan\n")
    registered = {"kind": "structured_recovery_registration", "authorized": True, "version": "structured-recovery-v1",
        "fixture_case_limit": 6, "live_case_limit": 1, "fixture_run_root": "runs/structured-recovery-v1",
        "live_output": "runs/structured-recovery-live-v1", "live_environment": {"container": "structured-recovery-a-v1", "port": 18128},
        "plan": "plan.md", "plan_sha256": schedule.file_sha(tmp_path / "plan.md")}
    atomic_json(tmp_path / "registration.json", registered)
    atomic_json(tmp_path / "suite.json", SUITE)
    atomic_json(tmp_path / "preflight.json", {"protocol": evaluate.PROTOCOL, "model_called": False, "method_tested": False,
        "suite_sha256": schedule.file_sha(tmp_path / "suite.json"), "passed": True,
        "results": [{"case_id": case["case_id"], "case_sha256": digest(case), "passed": True, "model_called": False,
                     "method_tested": False, "checks": dict.fromkeys(("intake_ticket_bound", "real_prepare", "no_writes",
                         "request_unchanged", "manifest_bound", "artifacts_bound", "sources_bound"), True)} for case in SUITE["cases"]]})
    return schedule.freeze(tmp_path / "registration.json", tmp_path / "suite.json", tmp_path / "preflight.json")


def stored_attempt(tmp_path, batch, job, *, status="success", tokens=5):
    directory = tmp_path / batch["fixture_run_root"] / job["attempt_id"]
    directory.mkdir()
    (directory / "run").mkdir()
    atomic_json(directory / "run/structured-trace.json", [])
    def segment(value):
        return {"tokens": value, "known_token_prefix": value or 0, "unknown_workers": [] if value is not None else ["one"],
            "provider_workers_dispatched": 0, "provider_responses": 0, "workers": [],
            "usage_by_field": {"input": value, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "known_usage_by_field": {"input": value or 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
    result = {**job, "protocol": evaluate.PROTOCOL, "status": status, "usage": {"tokens": tokens},
        "usage_by_stage": {"parent": segment(tokens), "child": segment(0), "product_prepare": segment(0)},
        "trace_sha256": schedule.file_sha(directory / "run/structured-trace.json")}
    atomic_json(directory / "started.json", {"job": job, "batch_sha256": digest(batch)})
    atomic_json(directory / "run/result.json", result)
    atomic_json(directory / "receipt.json", {"job": job, "result_sha256": schedule.file_sha(directory / "run/result.json")})
    return directory


def test_freeze_binds_registration_sources_and_fixed_layout(tmp_path, monkeypatch):
    batch = frozen_batch(tmp_path, monkeypatch)
    schedule.check_batch(batch)
    assert (tmp_path / batch["fixture_run_root"] / "frozen-source/source.py").read_text() == "source = 1\n"
    with pytest.raises(ValueError, match="already_frozen"):
        schedule.freeze(tmp_path / "registration.json", tmp_path / "suite.json", tmp_path / "preflight.json")
    (tmp_path / "source.py").write_text("source = 2\n")
    with pytest.raises(ValueError, match="frozen_source"):
        schedule.check_batch(batch)


def test_unknown_and_unstarted_keep_denominator_and_unknown_cost(tmp_path, monkeypatch):
    batch = frozen_batch(tmp_path, monkeypatch)
    stored_attempt(tmp_path, batch, batch["jobs"][0], status="unknown", tokens=None)
    result = schedule.snapshot(batch)
    assert result["planned_denominator"] == 6 and result["outcomes"]["not_started"] == 5
    assert result["outcomes"]["unknown"] == 1 and result["tokens"] is None and not result["fixture_gate_passed"]


def test_live_requires_six_successes_and_one_canonical_slot(tmp_path, monkeypatch):
    batch = frozen_batch(tmp_path, monkeypatch)
    output = tmp_path / batch["live_output"]
    with pytest.raises(ValueError, match="six_fixture"):
        schedule.claim_live(batch, output, batch["live_environment"])
    for job in batch["jobs"]:
        stored_attempt(tmp_path, batch, job)
    with pytest.raises(ValueError, match="unregistered_live"):
        schedule.claim_live(batch, tmp_path / "another-output", batch["live_environment"])
    schedule.claim_live(batch, output, batch["live_environment"])
    with pytest.raises(ValueError, match="already_claimed"):
        schedule.claim_live(batch, output, batch["live_environment"])
    assert schedule.snapshot(batch)["tokens"] == 30


def test_recorded_trace_is_bound_and_started_attempt_is_not_retried(tmp_path, monkeypatch):
    batch = frozen_batch(tmp_path, monkeypatch)
    directory = stored_attempt(tmp_path, batch, batch["jobs"][0])
    (directory / "run/structured-trace.json").write_text("[]\n\n")
    with pytest.raises(ValueError, match="original_attempt_evidence_changed"):
        schedule.snapshot(batch)


def test_started_without_receipt_cannot_be_redispatched(tmp_path, monkeypatch):
    batch = frozen_batch(tmp_path, monkeypatch)
    job = batch["jobs"][0]
    path = tmp_path / batch["fixture_run_root"] / job["attempt_id"]
    path.mkdir()
    atomic_json(path / "started.json", {"job": job, "batch_sha256": digest(batch)})
    monkeypatch.setattr(evaluate, "run_case", lambda *args, **kwargs: pytest.fail("must not dispatch again"))
    with pytest.raises(ValueError, match="original_attempt_requires_reconciliation"):
        schedule.run_next(batch)


def test_request_variants_are_explicit_and_wrong_scope_never_matches():
    expected = SUITE["cases"][0]["expected"]
    request = copy.deepcopy(expected["request"])
    request["disruption"].pop("offer_key")
    assert evaluate.request_matches(request, expected)
    assert not evaluate.request_matches(request, {**expected, "allow_vendor_only_disruption": False})
    request["sales_order_budget_caps"]["2"] += 1
    assert not evaluate.request_matches(request, expected)


def test_live_script_contains_typed_correction_not_language_reply():
    contract = contract_from_payload(SUITE["cases"][0]["contract"])
    _, _, _, script = live.build_inputs(contract, order_name="SO-1", variant="correction")
    assert script["actions"][-2:] == [{"kind": "reject_superseded_confirmation"}, {"kind": "confirm"}]
    assert all("text" not in row for row in script["actions"])
    source = (ROOT / "scripts/structured_recovery_live.py").read_text()
    assert "cases.submit_reply(" not in source and 'host.schedule("reply"' not in source
    assert 'schedule.claim_live(batch, output, selected)' in source


@pytest.mark.parametrize("change", [{"passed_rule_count": 2}, {"failed_rule_count": 1},
    {"evaluation_valid": False}, {"evaluation_scope": "replacement"}, {"reward_sha256": ""}])
def test_original_verifier_requires_every_applicable_rule_and_original_receipt(change):
    verifier = {"strict_success": True, "evaluation_valid": True, "evaluation_scope": "product_original_verifier",
        "applicable_rule_count": 3, "passed_rule_count": 3, "failed_rule_count": 0, "reward_sha256": "a" * 64}
    assert live.original_verifier_passed(verifier)
    assert not live.original_verifier_passed({**verifier, **change})


def test_safe_archive_keeps_original_bytes_and_excludes_host_bearers(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    root = tmp_path / "run"
    (root / "runtime-inputs").mkdir(parents=True)
    atomic_json(root / "result.json", {"status": "success"})
    original = (root / "result.json").read_bytes()
    atomic_json(root / "runtime-inputs/bridge.json", {"token": "host-bearer"})
    archive.archive(root, tmp_path / "evidence.tar.gz", secrets=(b"host-bearer",))
    with tarfile.open(tmp_path / "evidence.tar.gz") as packed:
        assert packed.extractfile("result.json").read() == original
        assert "runtime-inputs/bridge.json" not in packed.getnames()
    assert (root / "runtime-inputs/bridge.json").exists()


@pytest.mark.parametrize("secret", [{"nonce": "approval-reusable"}, {"session_secret": "private-session"}, {"Authorization": "Bearer private"}])
def test_unknown_sensitive_fields_reject_archive_without_rewriting(tmp_path, monkeypatch, secret):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    root = tmp_path / "run"
    root.mkdir()
    atomic_json(root / "trace.json", secret)
    before = (root / "trace.json").read_bytes()
    with pytest.raises(ValueError, match="reusable_credential"):
        archive.archive(root, tmp_path / "out.tar.gz", secrets=())
    assert (root / "trace.json").read_bytes() == before and not (tmp_path / "out.tar.gz").exists()


@pytest.mark.parametrize("flow", ["distinct_offer", "shared_offer", "invalid_action"])
def test_full_fixture_harness_with_zero_model_rule_boundary_runs_real_host_and_product(tmp_path, monkeypatch, flow):
    case = copy.deepcopy(SUITE["cases"][0])
    if flow == "distinct_offer":
        # An independent positive control for the budget continuation. Keep the
        # archived shared-price case intact to verify that its second PO survives.
        records = case["records"]
        old = next(row for row in records["product.supplierinfo"] if row["id"] == 2)
        records["product.supplierinfo"].append(old | {"id": 4, "price": 55, "min_qty": 2})
        next(row for row in records["purchase.order.line"] if row["order_id"] == 2)["price_unit"] = 55
    if flow == "invalid_action":
        case["actions"] = case["actions"][:1] + [{"kind": "unregistered_action"}]
    class LocalBoundaryWorker:
        def episode(self, *, config, allowance, parent_context, work_directory):
            policy = config["policy"]
            service = InvestigationService(source=factory_args["source_factory"](policy), contract=policy,
                event={"text": case["event"], "sha256": evaluate.hashlib.sha256(case["event"].encode()).hexdigest()},
                store=config["store"], bundle=factory_args["bundle"], connection_factory=factory_args["connection_factory"],
                limits=Limits(**{key: allowance[key] for key in ("queries", "validations", "prepares", "seconds")}))
            trace = []
            def dispatch(action, payload):
                result = service.dispatch(config["run_id"], action, payload)
                trace.append({"index": len(trace), "request": {"action": action, "payload": payload}, "result": result})
                return result
            run_rules(case["event"], dispatch)
            work_directory.mkdir(parents=True, exist_ok=True)
            atomic_json(work_directory / "tool-trace.json", trace)
            atomic_json(work_directory / "provider-usage.json", {"tokens": 0, "messages": []})
            state = read_json(config["store"] / "investigation/state.json")
            return {"usage": {"tokens": 0, "seconds": 0, **state["usage"]}, "runtime_valid": True}
    factory_args = {}
    def factory(arm, **kwargs):
        assert arm == "fixed"
        factory_args.update(kwargs)
        return LocalBoundaryWorker()
    monkeypatch.setattr(evaluate.legacy, "_worker", factory)
    result = evaluate.run_case(case, output=tmp_path / "local-harness-only", image="not-dispatched", attempt_id="local-only")
    if flow == "shared_offer":
        assert result["status"] == "observed_failure" and result["checks"]["no_business_writes"]
        assert result["error"]["code"] == "child_did_not_prepare"
        states = [read_json(path) for path in (tmp_path / "local-harness-only").rglob("investigation/state.json")]
        assert any(state["status"] == "unsupported"
                   and state["terminal"]["reason"] == "additional_purchase_cancellation_unsupported"
                   for state in states)
        return
    if flow == "invalid_action":
        checkpoint = tmp_path / "local-harness-only/structured-trace.json"
        assert result["status"] == "observed_failure", result
        assert result["error"]["code"] == "unregistered_structured_action"
        assert len(read_json(checkpoint)) == 1
        assert read_json(checkpoint)[0]["action"]["kind"] == "submit_budget"
        assert result["trace_sha256"] == schedule.file_sha(checkpoint)
        assert result["actual_children"] == 0
        return
    assert result["status"] == "success", result
    assert result["checks"]["product_request_unchanged"] and result["checks"]["independent_product_prepare"]
    assert result["structured_reply_model_calls"] == 0
    assert all(row["tokens"] == 0 for row in result["usage_by_stage"].values())
    evidence_index = read_json(tmp_path / "local-harness-only/product-evidence/index.json")
    assert any(row["export_file"].endswith("/request.json") for row in evidence_index["files"])


def test_product_evidence_preserves_chain_and_redacts_only_explicit_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "ROOT", tmp_path)
    output = tmp_path / "run"
    output.mkdir()
    directory = tmp_path / "environment/product-registry/cases/case-a"
    store = directory / "store"
    plan = store / "plan-a"
    plan.mkdir(parents=True)
    atomic_json(plan / "request.json", {"authorization": "User explicitly authorized this acceptance", "budget": 1830})
    atomic_json(plan / "readback-a.json", {"verified": True})
    reward = '{"strict_success":true}\n'
    atomic_json(plan / "product-verification.json", {"payload": {"original_reward_json": reward,
        "verifier": {"reward_sha256": evaluate.hashlib.sha256(reward.encode()).hexdigest()}}})
    (plan / "journal.jsonl").write_text('{"step":1,"status":"succeeded"}\n')
    atomic_json(directory / "decision.json", {"approval": {"nonce": "local-approval-nonce"}, "plan": "plan-a"})
    original = (directory / "decision.json").read_bytes()
    manifest = archive.capture_product_evidence(output, SimpleNamespace(store=store, directory=directory), secrets=(b"local-approval-nonce",))
    rows = {row["export_file"]: row for row in manifest["files"]}
    for name in ("request.json", "readback-a.json", "product-verification.json", "journal.jsonl"):
        target = "product-evidence/store/plan-a/" + name
        assert rows[target]["kind"] == "exact_original_bytes"
        assert (output / target).read_bytes() == (plan / name).read_bytes()
    decision = rows["product-evidence/binding/decision.json"]
    assert decision["kind"] == "explicit_redacted_derivative" and decision["removed_fields"] == ["/0/approval/nonce"]
    assert (directory / "decision.json").read_bytes() == original
    source = output / "frozen-source/src/forgeflow/product/service.py"
    source.parent.mkdir(parents=True)
    source.write_text("# Frozen implementation must remain in evidence.\n")
    archive.archive(output, tmp_path / "evidence.tar.gz", secrets=(b"local-approval-nonce",))
    with tarfile.open(tmp_path / "evidence.tar.gz") as packed:
        assert packed.extractfile("frozen-source/src/forgeflow/product/service.py").read() == source.read_bytes()
        assert packed.extractfile("product-evidence/store/plan-a/product-verification.json").read() == (plan / "product-verification.json").read_bytes()
