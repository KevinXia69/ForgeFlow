"""Upgrade driver boundaries and HTTP contract; no VPS, Docker or model calls."""
import importlib.util
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from forgeflow.planning.bundle import build_planner_bundle
from forgeflow.recovery.journal import _digest as journal_digest

SPEC = importlib.util.spec_from_file_location(
    "upgrade_acceptance_test", Path(__file__).resolve().parents[1] / "scripts/vps_upgrade_acceptance.py")
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)

CASE = "a" * 32
PLAN = "b" * 64
GENERATION = "c" * 32
REWARD = b'{"reward": 100}\n'


def target(tmp_path):
    config = {"instance_name": "qa3", "repo_root": str(driver.ROOT), "scenario": "A",
              "service_unit": "forgeflow-runtime@qa3.service", "api_port": 8108,
              "odoo_port": 18108, "container": "forgeflow-odoo-qa3",
              "api_user": "forgeflow-api-qa3", "dsh_user": "forgeflow-dsh-qa3",
              "allow_initialize": False}
    config.update({key: str(tmp_path / "qa3" / key) for key in driver.PATH_KEYS})
    config["api_port"] = 8108
    item = {"id": "qa3", "port": 8108, "service_unit": config["service_unit"],
            "invite_code": "qa3-private-invite"}
    index = {"trusted_hosts": ["example.test"], "legacy": {"invite_code": "legacy-invite"},
             "instances": {"qa3": item, "qa1": {"invite_code": "qa1-invite"}}}
    gateway = {"public_tenant": "judge1", "tenants": [
        {"id": "qa3", "port": 8108, "invite_sha256": [driver.sha256(item["invite_code"].encode())]}]}
    return config, item, index, gateway


@pytest.mark.parametrize("name", ["judge1", "legacy", "qa1", "qa2", "qa4", "../qa3"])
def test_only_fresh_designated_qa_name_is_accepted(name, tmp_path):
    config, _, index, gateway = target(tmp_path)
    with pytest.raises(driver.AcceptanceFailure, match="only_new_qa3"):
        driver.validate_target(name, "https://example.test", index, config, [], gateway)


@pytest.mark.parametrize("url", ["http://example.test", "https://evil.test", "https://example.test/path",
                                 "https://user:password@example.test", "https://example.test?key=secret"])
def test_private_invite_cannot_be_sent_to_other_host_or_insecure_url(url, tmp_path):
    config, _, index, gateway = target(tmp_path)
    with pytest.raises(driver.AcceptanceFailure, match="trusted_public_https"):
        driver.validate_target("qa3", url, index, config, [], gateway)


def test_checks_config_route_and_resource_identity_before_requests(tmp_path):
    config, _, index, gateway = target(tmp_path)
    driver.validate_target("qa3", "https://example.test", index, config, [], gateway)
    gateway["tenants"][0]["port"] = 8101
    with pytest.raises(driver.AcceptanceFailure, match="qa_gateway_binding"):
        driver.validate_target("qa3", "https://example.test", index, config, [], gateway)
    gateway["tenants"][0]["port"] = 8108
    other = {**config, "api_user": "other-api", "dsh_user": "other-dsh", "container": "other",
             "api_port": 8000, "odoo_port": 18069, "service_unit": "other.service"}
    with pytest.raises(driver.AcceptanceFailure, match="shared_qa_path"):
        driver.validate_target("qa3", "https://example.test", index, config, [other], gateway)
    config["allow_initialize"] = True
    with pytest.raises(driver.AcceptanceFailure, match="initialization_must_be_disabled"):
        driver.validate_target("qa3", "https://example.test", index, config, [], gateway)


def test_explicit_authorization_is_checked_before_private_index(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(driver.os, "geteuid", lambda: 0)
    monkeypatch.setattr(driver, "load_target", lambda *_: pytest.fail("must not read private target"))
    monkeypatch.setattr("sys.argv", ["driver", "--name", "qa3", "--base-url", "https://example.test",
                                     "--output", str(tmp_path / "new")])
    assert driver.main() == 1
    assert "explicit_execute_authorization_required" in capsys.readouterr().out


def test_fresh_gate_rejects_cases_owned_by_other_principals(tmp_path):
    config, _, _, _ = target(tmp_path)
    root = Path(config["case_root"])
    root.mkdir(parents=True)
    (root / (CASE + ".json")).write_text("{}")
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])),
                      base_url="https://example.test") as client:
        with pytest.raises(driver.AcceptanceFailure, match="qa_has_persisted_cases"):
            driver.require_fresh(client, config, {"available": True, "owned_by_me": False})


def selected_case():
    return {"case_id": CASE, "state": "awaiting_approval", "approval": None, "execution": None,
            "plan": {"plan_id": PLAN, "objective": "preserve_quantities", "actions": [{}, {}]}}


def executed_case():
    return {**selected_case(), "state": "succeeded", "result": {"verified": True},
            "approval": {"plan_id": PLAN, "nonce": "private-approval-nonce"},
            "execution": {"verifier": {"strict_success": True, "evaluation_valid": True,
                "evaluation_scope": "product_original_verifier", "passed_rule_count": 51,
                "applicable_rule_count": 51, "failed_rule_count": 0, "score": 100,
                "reward_sha256": driver.sha256(REWARD)},
                "readback": {"execution_validated": True, "verified_steps": 7,
                             "receipt_sha256": "d" * 64}}}


def test_selection_uses_current_comparison_binding():
    seen = []
    def respond(request):
        seen.append(json.loads(request.content))
        assert request.url.path == f"/api/product/cases/{CASE}/select"
        return httpx.Response(200, json=selected_case())
    case = {**selected_case(), "state": "awaiting_selection", "plan": None,
            "comparison": {"comparison_id": "f" * 32, "selection_revision": 3,
                           "plans": [selected_case()["plan"]]}}
    with httpx.Client(transport=httpx.MockTransport(respond), base_url="https://example.test") as client:
        assert driver.select_default(client, case)["plan"]["plan_id"] == PLAN
    assert seen == [{"plan_id": PLAN, "comparison_id": "f" * 32, "selection_revision": 3}]


def test_approved_execution_requires_original_rules_and_exact_plan():
    driver.verify_execution(executed_case(), PLAN)
    changed = executed_case()
    changed["execution"]["verifier"]["score"] = 99
    with pytest.raises(driver.AcceptanceFailure, match="a_execution_not_verified"):
        driver.verify_execution(changed, PLAN)
    with pytest.raises(driver.AcceptanceFailure, match="a_execution_not_verified"):
        driver.verify_execution(executed_case(), "e" * 64)


def test_execute_checks_replay_restart_and_raw_reward_without_reinitializing(monkeypatch, tmp_path):
    config, _, _, _ = target(tmp_path)
    calls, commands = [], []
    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/environment":
            return httpx.Response(200, json={"generation": GENERATION, "owned_by_me": True})
        if request.method == "POST" and request.url.path == "/api/product/cases":
            return httpx.Response(201, json=selected_case())
        if request.method == "POST":
            assert request.url.path.endswith("/approve")
            assert json.loads(request.content)["plan_id"] == PLAN
        return httpx.Response(200, json=executed_case())
    def command(*args, **kwargs):
        commands.append(args)
        return REWARD if args[0] == "docker" else b""
    monkeypatch.setattr(driver, "command", command)
    snapshots = iter([{"purchase_order": {"count": n, "rows_md5": "a" * 32}}
                      for n in (6, 6, 7, 7, 7)])
    monkeypatch.setattr(driver, "tables", lambda _: next(snapshots))
    monkeypatch.setattr(driver, "independent_margin", lambda *_: {"margin_basis": "new_spend_coverage_v2"})
    receipt_checks = []
    def receipt_check(_config, case, reward):
        receipt_checks.append((case["case_id"], reward))
        return {"http_receipt_matches_local": True}
    monkeypatch.setattr(driver, "verify_receipt", receipt_check)
    out = tmp_path / "output"
    out.mkdir()
    report = {}
    with httpx.Client(transport=httpx.MockTransport(respond), base_url="https://example.test") as client:
        driver.execute(client, config, {"generation": GENERATION}, out, report)
    assert report["duplicate_approval_idempotent"]
    assert report["restart_preserves_receipt_and_business_tables"]
    assert calls.count(("POST", f"/api/product/cases/{CASE}/approve")) == 2
    assert commands == [("docker", "exec", config["container"], "cat", "/logs/verifier/reward.json"),
                        ("systemctl", "restart", "forgeflow-runtime@qa3.service")]
    assert (out / "original-reward.raw.json").read_bytes() == REWARD
    assert "private-approval-nonce" not in (out / "executed.json").read_text()
    assert receipt_checks == [(CASE, REWARD)] * 3


def test_model_is_bound_to_case_turn_and_never_sends_approval(monkeypatch, tmp_path):
    requests = []
    def respond(request):
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == f"/api/product/cases/{CASE}/ask/stream"
            body = json.loads(request.content)
            assert body["turn_id"] and body["conversation_id"] and body["resume"] is False
            data = json.dumps({"type": "final", "mode": "harness", "answer": "private model answer"})
            return httpx.Response(200, text="id: 1\ndata: " + data + "\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=executed_case())
    monkeypatch.setattr(driver, "tables", lambda _: {"sale_order": {"count": 5}})
    report = {}
    with httpx.Client(transport=httpx.MockTransport(respond), base_url="https://example.test") as client:
        driver.model(client, {}, CASE, tmp_path, report)
    assert len(requests) == 3
    assert report["answer_sha256"] == driver.sha256(b"private model answer")
    assert "private model answer" not in json.dumps(report)
    assert report["business_approval_requested"] is False


def test_preparation_mutation_stops_before_approval(monkeypatch, tmp_path):
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(201, json=selected_case())
    snapshots = iter([{"purchase_order": {"count": n}} for n in (6, 7)])
    monkeypatch.setattr(driver, "tables", lambda _: next(snapshots))
    monkeypatch.setattr(driver, "independent_margin", lambda *_: {})
    monkeypatch.setattr(driver, "command", lambda *_args, **_kwargs: pytest.fail("no restart or write"))
    with httpx.Client(transport=httpx.MockTransport(respond), base_url="https://example.test") as client:
        with pytest.raises(driver.AcceptanceFailure, match="preparation_changed_business_tables"):
            driver.execute(client, {}, {}, tmp_path, {})
    assert calls == ["/api/product/cases"]


@pytest.mark.parametrize("event", [{"type": "error", "code": "private failure"},
                                   {"type": "final", "mode": "fallback", "answer": "not a model"}])
def test_model_fallback_or_stream_error_cannot_be_counted_as_real_model(monkeypatch, tmp_path, event):
    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, text="data: " + json.dumps(event) + "\n\n",
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=executed_case())
    monkeypatch.setattr(driver, "tables", lambda _: {})
    with httpx.Client(transport=httpx.MockTransport(respond), base_url="https://example.test") as client:
        with pytest.raises(driver.AcceptanceFailure):
            driver.model(client, {}, CASE, tmp_path, {})


def test_publication_removes_private_nested_fields_and_refuses_overwrite(tmp_path):
    value = {"case_id": CASE, "approval": {"nonce": "secret", "approver": "operator"},
             "answer": "private answer", "invite_code": "private invite",
             "nested": [{"owner_sha256": "owner", "token": "token", "plan_id": PLAN}]}
    driver.save(tmp_path, "report", value)
    content = (tmp_path / "report.json").read_text()
    assert not any(secret in content for secret in ("secret", "operator", "private", "token", "owner"))
    assert json.loads(content)["nested"] == [{"plan_id": PLAN}]
    with pytest.raises(FileExistsError):
        driver.save(tmp_path, "report", {})


@pytest.fixture
def current_bundle(tmp_path):
    archive = tmp_path / "current.tar.gz"
    build_planner_bundle(driver.ROOT, archive, recovery=True)
    with tarfile.open(archive) as handle:
        handle.extractall(tmp_path, filter="data")
    return tmp_path


def test_sources_accepts_real_current_bundle_with_generated_package_marker(current_bundle):
    assert (driver.ROOT / "src/forgeflow/__init__.py").read_bytes() != b""
    result = driver.sources({"sandbox_root": str(current_bundle)})
    assert result["bundle_generated_empty_package_markers"] == {
        name: driver.sha256(b"") for name in driver.GENERATED_PACKAGE_MARKERS}
    assert "forgeflow/__init__.py" not in result["bundle_matches_current_source"]
    assert "forgeflow/recovery/margin.py" in result["bundle_matches_current_source"]


@pytest.mark.parametrize("name", sorted(driver.GENERATED_PACKAGE_MARKERS))
def test_sources_rejects_changed_generated_marker_even_with_updated_manifest(current_bundle, name):
    bundle = current_bundle / "planner"
    path = bundle / name
    path.chmod(0o600)
    path.write_bytes(b"# injected entry point\n")
    manifest = bundle / "provenance.json"
    record = json.loads(manifest.read_text())
    record["files_sha256"][name] = driver.sha256(path.read_bytes())
    manifest.chmod(0o600)
    manifest.write_text(json.dumps(record))
    with pytest.raises(driver.AcceptanceFailure, match="qa_bundle_generated_marker_changed"):
        driver.sources({"sandbox_root": str(current_bundle)})


def test_sources_rejects_omitted_generated_marker(current_bundle):
    manifest = current_bundle / "planner/provenance.json"
    record = json.loads(manifest.read_text())
    del record["files_sha256"]["forgeflow/__init__.py"]
    manifest.chmod(0o600)
    manifest.write_text(json.dumps(record))
    with pytest.raises(driver.AcceptanceFailure, match="qa_bundle_generated_marker_missing"):
        driver.sources({"sandbox_root": str(current_bundle)})


def test_sources_uses_configured_active_bundle_instead_of_historical_directory(current_bundle):
    active = current_bundle / "active-current"
    (current_bundle / "planner").rename(active)
    (current_bundle / "planner").mkdir()
    (current_bundle / "planner/provenance.json").write_text('{"historical": true}')
    result = driver.sources({"sandbox_root": str(current_bundle), "active_bundle": str(active)})
    assert result["bundle_provenance_sha256"] == driver.digest(
        json.loads((active / "provenance.json").read_text()))


@pytest.fixture
def bound_receipt(current_bundle):
    active = current_bundle / "active-current"
    (current_bundle / "planner").rename(active)
    config = {"sandbox_root": str(current_bundle), "active_bundle": str(active)}
    provenance = driver.verify_sources(active)
    snapshot = {"models": {"sale.order": [{"id": 1}]}}
    snapshot_sha = driver.digest(snapshot)
    manifest = {"sources": provenance, "product": {"run_id": "local-" + CASE},
                "policy_sha256": "e" * 64, "artifacts": {"snapshot": snapshot_sha}}
    plan_id = driver.digest(manifest)
    directory = current_bundle / "store" / plan_id
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "snapshot.json").write_text(json.dumps(snapshot))
    unsigned = {"index": 0, "previous": "0" * 64, "event": "dispatch", "data": {}}
    journal = (json.dumps({**unsigned, "sha256": journal_digest(unsigned)}) + "\n").encode()
    (directory / "journal.jsonl").write_bytes(journal)
    case = executed_case()
    case["plan"].update(plan_id=plan_id, policy_sha256=manifest["policy_sha256"], snapshot_sha256=snapshot_sha)
    case["approval"]["plan_id"] = plan_id
    case["execution"]["readback"].update(readback_sha256="f" * 64, verified_at="2026-10-29T00:00:00Z")
    bindings = {"run_id": "local-" + CASE, "case_id": CASE, "plan_id": plan_id,
                "policy_sha256": manifest["policy_sha256"], "snapshot_sha256": snapshot_sha,
                "journal_sha256": driver.sha256(journal), "readback_sha256": "f" * 64,
                "sources_sha256": driver.digest(provenance)}
    payload = {"schema_version": 1, "bindings": bindings, "verifier": case["execution"]["verifier"],
               "verified_steps": 7, "verified_at": case["execution"]["readback"]["verified_at"],
               "original_reward_json": REWARD.decode()}
    receipt = {"payload": payload, "sha256": driver.digest(payload)}
    case["execution"]["readback"]["receipt_sha256"] = receipt["sha256"]
    path = directory / "product-verification.json"
    path.write_text(json.dumps(receipt))
    return config, case, receipt, path


def test_receipt_binding_matches_http_manifest_journal_actual_active_source_and_raw_reward(bound_receipt):
    config, case, receipt, _ = bound_receipt
    result = driver.verify_receipt(config, case, REWARD)
    assert result["receipt_sha256"] == receipt["sha256"]
    assert result["sources_sha256"] == receipt["payload"]["bindings"]["sources_sha256"]
    assert set(result["verified_binding_fields"]) == set(receipt["payload"]["bindings"])
    assert "original_reward_json" not in result and "bindings" not in result
    assert "private-approval-nonce" not in json.dumps(result)


@pytest.mark.parametrize("binding", ["run_id", "case_id", "plan_id", "policy_sha256",
                                      "snapshot_sha256", "journal_sha256", "readback_sha256", "sources_sha256"])
def test_receipt_rejects_rebound_input_even_when_outer_and_http_hashes_are_recomputed(bound_receipt, binding):
    config, case, receipt, path = bound_receipt
    receipt["payload"]["bindings"][binding] = "9" * 64
    receipt["sha256"] = driver.digest(receipt["payload"])
    case["execution"]["readback"]["receipt_sha256"] = receipt["sha256"]
    path.write_text(json.dumps(receipt))
    with pytest.raises(driver.AcceptanceFailure, match="local_receipt_binding_mismatch"):
        driver.verify_receipt(config, case, REWARD)


def test_receipt_rejects_corruption_and_raw_reward_substitution(bound_receipt):
    config, case, receipt, path = bound_receipt
    with pytest.raises(driver.AcceptanceFailure, match="local_receipt_reward_mismatch"):
        driver.verify_receipt(config, case, b'{"reward": 0}\n')
    receipt["payload"]["verified_steps"] = 8
    path.write_text(json.dumps(receipt))
    with pytest.raises(driver.AcceptanceFailure, match="local_receipt_digest_mismatch"):
        driver.verify_receipt(config, case, REWARD)


def test_receipt_rejects_current_bundle_provenance_different_from_plan_source(bound_receipt):
    config, case, _, _ = bound_receipt
    path = Path(config["active_bundle"]) / "provenance.json"
    provenance = json.loads(path.read_text())
    provenance["release_note"] = "different source identity"
    path.chmod(0o600)
    path.write_text(json.dumps(provenance))
    with pytest.raises(driver.AcceptanceFailure, match="receipt_plan_source_mismatch"):
        driver.verify_receipt(config, case, REWARD)
