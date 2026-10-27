"""The publication allowlist must not leak private acceptance fields."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "reviewer_evidence_test", Path(__file__).resolve().parents[1] / "scripts/export_reviewer_evidence.py")
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


def capture():
    reward = '{"passed": true}\n'
    private_marker = "PRIVATE-MUST-NOT-BE-PUBLISHED"
    result = {"prepared": {"case_id": "case", "plan": {"plan_id": "plan"}, "synthetic": True,
                           "state": "awaiting_approval", "execution": None, "approval": None,
                           "owner_uid": private_marker},
              "executed": {"state": "succeeded", "execution": {"status": "completed", "writes": [],
                             "readback": {"execution_validated": True, "private": private_marker},
                             "verifier": {"strict_success": True, "reward_sha256": hashlib.sha256(reward.encode()).hexdigest(),
                                          "private": private_marker}}},
              "before_prepare": {}, "after_prepare": {}, "before_duplicate": {}, "after_duplicate": {},
              "duplicate_idempotent": True, "original_reward_json": reward,
              "assistant": {"mode": "harness", "answer": private_marker}, "invite_code": private_marker}
    return {"instances": {name: copy.deepcopy(result) for name in ("qa1", "qa2")},
            "source_sha256": {"scripts/vps_reviewer_acceptance.py": "a" * 64},
            "isolation": {"cross_case_404": True, "private": private_marker},
            "restart": {"service_restart_passed": True, "private": private_marker},
            "resources": {"model": {"sample_errors": 0, "samples": [{"at_unix": 1, "memory": {},
                "units": {"qa1": {"MemoryPeak": "100", "Environment": private_marker}},
                "containers": [{"Name": "qa1", "Environment": private_marker}],
                "private": private_marker}]}}, "session_secret": private_marker}


def test_published_evidence_excludes_credentials_answers_and_future_diagnostic_fields(tmp_path):
    source, output = tmp_path / "private.json", tmp_path / "public"
    source.write_text(json.dumps(capture()))
    assert publisher.export(source, output)["public_files_written"] == 4
    for path in output.iterdir():
        assert "PRIVATE-MUST-NOT-BE-PUBLISHED" not in path.read_text()
    manifest = json.loads((output / "manifest.json").read_text())
    for row in manifest["files"]:
        assert hashlib.sha256((output / row["name"]).read_bytes()).hexdigest() == row["sha256"]


def test_non_synthetic_or_incomplete_workflows_are_not_exported_as_passed(tmp_path):
    for mutation in ("production", "preparation_write", "reward_mismatch"):
        value = capture()
        if mutation == "production":
            value["instances"]["qa1"]["prepared"]["synthetic"] = False
        elif mutation == "preparation_write":
            value["instances"]["qa1"]["after_prepare"] = {"changed": True}
        else:
            value["instances"]["qa1"]["original_reward_json"] = "{}"
        source = tmp_path / (mutation + ".json")
        source.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            publisher.export(source, tmp_path / mutation)


def bound_capture():
    value = capture()
    value.update({"schema": 2, "created_at": "2026-10-27T01:00:00+00:00",
                  "updated_at": "2026-10-27T01:10:00+00:00", "stages": {}})
    value["isolation"].update({"cross_session_401": True, "unix_file_isolation": True})
    value["restart"].update({"case_execution_preserved": True, "leases_preserved": True})
    value["release"] = {"schema": 1, "git_revision": "b" * 40, "tracked_worktree_clean": True,
        "source_sha256": value["source_sha256"], "frontend_sha256": {"index.html": "c" * 64},
        "runtime_paths": {"qa1": {"package_path": "PRIVATE-MUST-NOT-BE-PUBLISHED"}},
        "binding_scope": publisher.BINDING_SCOPE, "private": "PRIVATE-MUST-NOT-BE-PUBLISHED"}
    for index, name in enumerate(publisher.STAGES):
        value["stages"][name] = {"status": "passed",
            "started_at": f"2026-10-27T01:0{index * 2}:00+00:00",
            "finished_at": f"2026-10-27T01:0{index * 2 + 1}:00+00:00",
            "private": "PRIVATE-MUST-NOT-BE-PUBLISHED"}
    for index, result in enumerate(value["instances"].values()):
        case_id, plan_id = str(index + 1) * 32, "a" * 64
        result["generation"] = str(index + 3) * 32
        result["prepared"].update({"case_id": case_id, "plan": {"plan_id": plan_id}, "revision": {"version": 1}})
        result["executed"].update({"case_id": case_id, "plan": {"plan_id": plan_id}, "revision": {"version": 1},
            "approval": {"plan_id": plan_id, "issued_at": "2026-10-27T01:04:00+00:00",
                         "approver": "PRIVATE-MUST-NOT-BE-PUBLISHED", "nonce": "PRIVATE-MUST-NOT-BE-PUBLISHED"}})
        result["executed"]["execution"]["verifier"].update({"evaluation_valid": True,
            "evaluation_scope": "product_original_verifier"})
        result["assistant"].update({"source_mode": "model", "case_context": {
            "case_id": case_id, "plan_id": plan_id, "version": 1, "private": "PRIVATE-MUST-NOT-BE-PUBLISHED"}})
    return value


def test_frozen_release_stages_and_answer_hash_are_exported_without_private_fields(tmp_path):
    value = bound_capture()
    source, output = tmp_path / "source.json", tmp_path / "public"
    source.write_text(json.dumps(value))
    original = source.read_bytes()
    publisher.export(source, output)
    assert source.read_bytes() == original
    report = json.loads((output / "acceptance.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert report["schema"] == manifest["schema"] == 2
    assert report["date_utc"] == "2026-10-27"
    assert report["release_binding"] == manifest["release_binding"] == "frozen_files"
    assert report["release"] == manifest["release"]
    assert report["release"]["git_revision"] == "b" * 40
    assert report["release"]["binding_scope"] == publisher.BINDING_SCOPE
    assert report["release"]["frontend_sha256"] == {"index.html": "c" * 64}
    assert set(report["stages"]) == set(publisher.STAGES)
    assert report["provider_usage_or_billing_verified"] is False
    for name in ("qa1", "qa2"):
        instance = report["instances"][name]
        assert instance["version"] == 1
        assert instance["approval"]["plan_id"] == value["instances"][name]["prepared"]["plan"]["plan_id"]
        assert instance["assistant"]["source_mode"] == "model"
        assert instance["assistant"]["answer_sha256"] == hashlib.sha256(
            value["instances"][name]["assistant"]["answer"].encode()).hexdigest()
    for path in output.iterdir():
        assert "PRIVATE-MUST-NOT-BE-PUBLISHED" not in path.read_text()


@pytest.mark.parametrize("mutation", ["missing_stage", "failed_stage", "running_stage", "out_of_order",
    "missing_release", "dirty", "wrong_revision", "missing_frontend", "source_mismatch", "private_path",
    "wrong_case", "wrong_plan", "wrong_version", "wrong_approval", "wrong_answer_context",
    "deterministic_answer", "missing_generation", "invalid_verifier", "invalid_readback", "isolation_failed"])
def test_incomplete_or_mixed_release_is_not_published(tmp_path, mutation):
    value = bound_capture()
    result = value["instances"]["qa2"]
    if mutation == "missing_stage":
        del value["stages"]["model"]
    elif mutation in {"failed_stage", "running_stage"}:
        value["stages"]["model"]["status"] = mutation.split("_")[0]
    elif mutation == "out_of_order":
        value["stages"]["approve"]["started_at"] = "2026-10-27T00:00:00+00:00"
    elif mutation == "missing_release":
        del value["release"]
    elif mutation == "dirty":
        value["release"]["tracked_worktree_clean"] = False
    elif mutation == "wrong_revision":
        value["release"]["git_revision"] = "latest"
    elif mutation == "missing_frontend":
        value["release"]["frontend_sha256"] = {}
    elif mutation == "source_mismatch":
        value["source_sha256"] = {"scripts/other.py": "d" * 64}
    elif mutation == "private_path":
        value["release"]["frontend_sha256"]["/private/path"] = "d" * 64
    elif mutation == "wrong_case":
        result["executed"]["case_id"] = "f" * 32
    elif mutation == "wrong_plan":
        result["executed"]["plan"]["plan_id"] = "f" * 64
    elif mutation == "wrong_version":
        result["executed"]["revision"]["version"] = 2
    elif mutation == "wrong_approval":
        result["executed"]["approval"]["plan_id"] = "f" * 64
    elif mutation == "wrong_answer_context":
        result["assistant"]["case_context"]["plan_id"] = "f" * 64
    elif mutation == "deterministic_answer":
        result["assistant"]["source_mode"] = "deterministic"
    elif mutation == "missing_generation":
        del result["generation"]
    elif mutation == "invalid_verifier":
        result["executed"]["execution"]["verifier"]["evaluation_valid"] = False
    elif mutation == "invalid_readback":
        result["executed"]["execution"]["readback"]["execution_validated"] = False
    elif mutation == "isolation_failed":
        value["isolation"]["cross_session_401"] = False
    source, output = tmp_path / "source.json", tmp_path / "public"
    source.write_text(json.dumps(value))
    original = source.read_bytes()
    with pytest.raises(ValueError):
        publisher.export(source, output)
    assert source.read_bytes() == original
    assert not output.exists()


def test_historical_capture_keeps_unknown_release_and_uses_only_recorded_dates(tmp_path):
    value = capture()
    source, output = tmp_path / "source.json", tmp_path / "public"
    for recorded, expected in ((None, None), ("2026-10-25T20:01:00-07:00", "2026-10-26")):
        value["instances"]["qa1"]["prepared"]["updated_at"] = recorded
        source.write_text(json.dumps(value))
        publisher.export(source, output)
        report = json.loads((output / "acceptance.json").read_text())
        assert report["date_utc"] == expected
        assert report["release_binding"] == "historical_unknown"
        assert "release" not in report
        assert report["provider_usage_or_billing_verified"] is False
