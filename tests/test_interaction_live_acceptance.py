"""Default production composition and acceptance evidence, without ERP/model."""
import copy
import importlib.util
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_continuation_default_strategy import configured as _configured

from forgeflow.contract import contract_from_payload
from forgeflow.investigation import runtime
from forgeflow.investigation.cases import CaseLimits
from forgeflow.investigation.host import ContinuationHost
from forgeflow.recovery.storage import atomic_json, digest, read_json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("interaction_live_acceptance_review", ROOT / "scripts/interaction_live_acceptance.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


@pytest.fixture
def prepared_input(tmp_path, monkeypatch):
    options = _configured.__wrapped__(tmp_path, monkeypatch)
    env = runtime.ManagedOdooEnvironment(options["environment_config"])
    output = tmp_path / "acceptance"
    output.mkdir()
    options["environment_config"].bundle.mkdir()
    atomic_json(options["environment_config"].bundle / "provenance.json", {"version": "fixture"})
    atomic_json(env.config.lifecycle_file.parent / "task-policy.json", options["original_contract"])
    baseline = {table: {"count": 0, "digest": "empty"} for table in live.TABLES}
    sources = {"src/fixture.py": "fixed-source-hash"}
    model = {"image_id": "sha256:" + "a" * 64, "models": {"fixture": "no-provider"}}
    monkeypatch.setattr(live, "source_hashes", lambda _: copy.deepcopy(sources))
    monkeypatch.setattr(live, "model_configuration", lambda: copy.deepcopy(model))
    monkeypatch.setattr(live, "business_tables", lambda _: copy.deepcopy(baseline))
    monkeypatch.setattr(live, "load_config", lambda *_a, **_k: SimpleNamespace(public_dict=lambda: {"model": "fixture"}))
    contract = contract_from_payload(options["original_contract"])
    template, policy, event, script = live.build_inputs(contract, order_name="SO-30", variant="correct")
    values = {"environment-input.json": live.environment_input(env), "template.json": template,
        "partial-policy.json": policy.payload(), "event.json": event, "user-script.json": script,
        "preflight-after-tables.json": baseline}
    for name, value in values.items():
        atomic_json(output / name, value)
    manifest = {"status": "preflight_ready", "generation": env.generation(), "source_files": sources,
        "model": model, "limits": asdict(CaseLimits()), "variant": "correct",
        "container": env.config.container, "port": env.config.port,
        "bundle_provenance_sha256": digest({"version": "fixture"}),
        "inputs": {name: digest(value) for name, value in values.items()}, "original_policy_sha256": contract.digest()}
    atomic_json(output / "preflight.json", manifest)
    return env, output, manifest


def test_actual_default_composition_is_v2_fixed_and_start_cannot_repeat(prepared_input, monkeypatch):
    env, output, manifest = prepared_input
    original = live.build_runtime
    calls = []

    def composition(**kwargs):
        assert "strategy" not in kwargs and "reply_contract_version" not in kwargs
        assert kwargs["image"] == manifest["model"]["image_id"]
        value = original(**kwargs)
        calls.append(kwargs)
        return value

    monkeypatch.setattr(live, "build_runtime", composition)
    monkeypatch.setattr(ContinuationHost, "create", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("test_stop_before_model")))
    with pytest.raises(RuntimeError, match="test_stop_before_model"):
        live.execute(ROOT, env, output)
    binding = read_json(output / "runtime/runtime-binding.json")
    assert binding["strategy"] == "fixed" and binding["reply_contract_version"] == 2
    assert binding["policy"] == read_json(output / "partial-policy.json")
    assert read_json(output / "acceptance-result.json")["status"] == "failed_or_unknown"
    original_result = (output / "acceptance-result.json").read_bytes()
    with pytest.raises(RuntimeError, match="run_already_started_never_auto_retry"):
        live.execute(ROOT, env, output)
    assert len(calls) == 1 and (output / "acceptance-result.json").read_bytes() == original_result


@pytest.mark.parametrize("kind", ["source", "model", "generation"])
def test_drift_blocks_before_claim_and_runtime(prepared_input, monkeypatch, kind):
    env, output, _ = prepared_input
    if kind == "source":
        monkeypatch.setattr(live, "source_hashes", lambda _: {"changed": "source"})
    elif kind == "model":
        monkeypatch.setattr(live, "model_configuration", lambda: {"changed": "model"})
    else:
        monkeypatch.setattr(env, "generation", lambda: "another-generation")
    monkeypatch.setattr(live, "build_runtime", lambda **_: pytest.fail("Drift must stop before composition"))
    with pytest.raises(RuntimeError, match="preflight_.*changed"):
        live.execute(ROOT, env, output)
    assert not (output / "run-started.json").exists()


def test_failure_during_composition_has_outcome_but_cannot_clobber_another_attempt(prepared_input, monkeypatch):
    env, output, _ = prepared_input
    monkeypatch.setattr(live, "build_runtime", lambda **_: (_ for _ in ()).throw(RuntimeError("composition_failed")))
    with pytest.raises(RuntimeError, match="composition_failed"):
        live.execute(ROOT, env, output)
    outcome = read_json(output / "acceptance-result.json")
    assert outcome["failure_code"] == "composition_failed" and outcome["usage"] is None
    (output / "acceptance-result.json").unlink()  # Simulate another still-running owner with no final receipt.
    with pytest.raises(RuntimeError, match="run_already_started_never_auto_retry"):
        live.execute(ROOT, env, output)
    assert not (output / "acceptance-result.json").exists()


def test_correction_proof_must_identify_exact_invalidated_high_cap():
    previous = {"draft_sha256": "old", "reply_id": "r2", "proposal": {"amount": "900.00", "order_id": "30", "currency": "USD"}}
    current = {"draft_sha256": "new", "reply_id": "r3"}
    row = {"draft_sha256": "old", "reply_id": "r2", "invalidated_by_reply_id": "r3", **previous["proposal"]}
    assert live.corrected_draft_proof(previous, current, [row])["invalidation"] == row
    with pytest.raises(RuntimeError, match="prior_draft_not_invalidated"):
        live.corrected_draft_proof(previous, current, [{**row, "amount": "700.00"}])


def test_original_verifier_requires_actual_scope_all_rules_and_reward_provenance():
    valid = {"strict_success": True, "evaluation_valid": True, "evaluation_scope": "product_original_verifier",
             "applicable_rule_count": 51, "passed_rule_count": 51, "failed_rule_count": 0, "reward_sha256": "a" * 64}
    assert live.original_verifier_passed(valid)
    for change in ({"evaluation_scope": "fixture"}, {"passed_rule_count": 50}, {"strict_success": None},
                   {"evaluation_valid": None}, {"reward_sha256": None}):
        assert not live.original_verifier_passed({**valid, **change})


def test_public_export_preserves_raw_sensitive_files_and_excludes_them(tmp_path):
    output, destination = tmp_path / "raw", tmp_path / "public"
    output.mkdir()
    atomic_json(output / "acceptance-result.json", {"status": "failed_or_unknown", "usage": None})
    atomic_json(output / "product-execution-view.json", {"approval": {"nonce": "private-nonce"}})
    raw = (output / "product-execution-view.json").read_bytes()
    projection = live.export_public_evidence(output, destination)
    assert "product-execution-view.json" in projection["omitted"]
    assert not (destination / "product-execution-view.json").exists()
    assert (output / "product-execution-view.json").read_bytes() == raw
    assert (destination / "acceptance-result.json").read_bytes() == (output / "acceptance-result.json").read_bytes()
    assert not projection["raw_files_deleted_or_redacted"]


def test_sensitive_value_in_allowlisted_json_aborts_export(tmp_path):
    output, destination = tmp_path / "raw", tmp_path / "public"
    output.mkdir()
    atomic_json(output / "acceptance-result.json", {"unexpected": {"approval_nonce": "private-nonce"}})
    with pytest.raises(RuntimeError, match="sensitive_live_evidence_not_exportable"):
        live.export_public_evidence(output, destination)
    assert not destination.exists()
