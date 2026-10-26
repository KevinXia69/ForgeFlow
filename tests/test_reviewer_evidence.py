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
