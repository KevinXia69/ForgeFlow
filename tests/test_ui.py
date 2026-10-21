from __future__ import annotations

import json
import threading

import httpx
import pytest

from forgeflow.ui import build_snapshot, make_server, run_detail


def write_run(root, name, agent, score, *, state="completed", valid=True):
    run_dir = root / "runs" / name
    run_dir.mkdir(parents=True)
    manifest = {
        "agent": agent,
        "task": "2280_medium_repair_plan_medium",
        "run_id": name,
        "state": state,
        "harbor_returncode": 0,
        "started_at_utc": "2026-10-20T13:00:00+00:00",
        "finished_at_utc": "2026-10-20T13:01:30+00:00" if state == "completed" else None,
        "model": {"model": "nvidia/test", "api_key": "do-not-expose-manifest-key"},
        "error": {"type": "PrivateError", "message": "do-not-expose-error-text"},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    if score is not None:
        trial = run_dir / "harbor/job/trial"
        (trial / "verifier").mkdir(parents=True)
        reward = {
            "overall_score": score,
            "passed": score >= 99,
            "rules": {"by_dimension": {"constraint": [{
                "status": "PASS" if score == 100 else "FAIL",
                "applicable": True,
                "expr": "check_order_links",
            }]}},
        }
        (trial / "verifier/reward.json").write_text(json.dumps(reward))
        (trial / "result.json").write_text(json.dumps({
            "task_name": "agentic-labs/2280_medium_repair_plan_medium",
            "verifier_result": {"rewards": {"reward": score}},
            "exception_info": None if valid else {"exception_type": "VerifierError"},
        }))
    return run_dir


def test_snapshot_separates_controls_and_probes_from_valid_agent_results(tmp_path):
    write_run(tmp_path, "reference", "oracle", 100)
    write_run(tmp_path, "noop", "nop", 0)
    write_run(tmp_path, "pi-success", "pi", 100)
    write_run(tmp_path, "pi-failure", "pi", 0)
    write_run(tmp_path, "pi-invalid", "pi", 100, valid=False)
    write_run(tmp_path, "pi-running", "pi", None, state="running")
    probe_dir = tmp_path / "runs/model-probes"
    probe_dir.mkdir()
    (probe_dir / "model-probe-test.json").write_text(json.dumps({
        "kind": "provider_tool_call_smoke_test",
        "status": "passed",
        "config": {"model": "nvidia/test", "api_key": "private-probe-key"},
        "exchanges": [{"response_body": "private-response-body"}],
    }))
    snapshot = build_snapshot(tmp_path)
    assert snapshot["counts"] == {
        "erp_runs": 6,
        "measured_agent_runs": 2,
        "strict_agent_successes": 1,
        "environment_controls": 2,
        "expected_environment_controls": 2,
    }
    assert len(snapshot["probes"]) == 1
    for secret in (
        "do-not-expose-manifest-key", "do-not-expose-error-text",
        "private-probe-key", "private-response-body",
    ):
        assert secret not in json.dumps(snapshot)
    running = next(record for record in snapshot["runs"] if record["run_id"] == "pi-running")
    assert running["score"] is None
    assert running["elapsed_seconds"] is None
    assert running["strict_success"] is None


def test_runs_sort_by_recorded_start_time_with_stable_id_fallback(tmp_path):
    starts = {
        "v0-older": "2026-10-20T13:00:00Z",
        "budget-newer": "2026-10-20T07:00:00-07:00",
        "a-same-time": "2026-10-20T14:00:00+00:00",
        "z-missing-20991231": None,
        "y-invalid": "not-a-time",
        "x-missing": None,
    }
    for name, started in starts.items():
        manifest_path = write_run(tmp_path, name, "pi", None, state="running") / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["started_at_utc"] = started
        manifest_path.write_text(json.dumps(manifest))
    assert [run["run_id"] for run in build_snapshot(tmp_path)["runs"]] == [
        "budget-newer", "a-same-time", "v0-older",
        "z-missing-20991231", "y-invalid", "x-missing",
    ]


def test_detail_preserves_zero_score_and_original_rules_without_modifying_files(tmp_path):
    run_dir = write_run(tmp_path, "noop", "nop", 0)
    before = {path: path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    detail = run_detail(tmp_path, "noop")
    assert detail["score"] == 0
    assert detail["strict_success"] is False
    assert detail["expected_control_outcome"] is True
    assert detail["rules"] == [{
        "dimension": "constraint",
        "rule": "check_order_links",
        "status": "FAIL",
        "applicable": True,
    }]
    assert detail["metrics"]["cost_usd"] is None
    assert before == {path: path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}


def test_detail_shows_only_recorded_output_budget_and_public_sampling_values(tmp_path):
    run_dir = write_run(tmp_path, "budget-comparison", "pi", 0)
    historical = run_detail(tmp_path, "budget-comparison")
    assert historical["max_output_tokens"] is None
    assert historical["output_policy_version"] is None
    assert historical["sampling_params"] == {"temperature": None, "top_p": None}
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["budget"] = {
        "max_output_tokens": 32768,
        "output_policy_version": "explicit",
        "sampling_params": {"temperature": 1.0, "top_p": 0.95, "api_key": "private-key"},
    }
    manifest_path.write_text(json.dumps(manifest))
    detail = run_detail(tmp_path, "budget-comparison")
    assert detail["max_output_tokens"] == 32768
    assert detail["output_policy_version"] == "explicit"
    assert detail["sampling_params"] == {"temperature": 1.0, "top_p": 0.95}
    assert detail["score"] == historical["score"] == 0
    assert "private-key" not in json.dumps(detail)
    manifest["budget"] = {
        "max_output_tokens": True,
        "output_policy_version": "private-policy-value",
        "sampling_params": {"temperature": float("nan"), "top_p": "private-value"},
    }
    manifest_path.write_text(json.dumps(manifest))
    malformed = run_detail(tmp_path, "budget-comparison")
    assert malformed["max_output_tokens"] is None
    assert malformed["output_policy_version"] is None
    assert malformed["sampling_params"] == {"temperature": None, "top_p": None}
    assert "private-value" not in json.dumps(malformed)
    assert "private-policy-value" not in json.dumps(malformed)


@pytest.mark.parametrize(
    ("guidance_profile", "guidance_label"),
    [
        ("odoo-read-v1", "Odoo 接口执行提示"),
        ("odoo-schedule-v1", "Odoo 排期与回读提示"),
    ],
)
@pytest.mark.parametrize(
    ("planner_profile", "planner_label"),
    [
        ("cp-sat-live-v1", "CP-SAT 命令行入口"),
        ("cp-sat-native-v1", "CP-SAT Pi 原生工具入口"),
        ("cp-sat-native-v2", "CP-SAT 原生工具 · 可修正输入反馈"),
    ],
)
def test_experiment_labels_preserve_history_and_project_only_known_public_fields(
    tmp_path, planner_profile, planner_label, guidance_profile, guidance_label,
):
    run_dir = write_run(tmp_path, "profiled", "pi", 100)
    original = run_detail(tmp_path, "profiled")
    assert original["data_profile"] == "original"
    assert original["data_profile_recorded"] is False
    assert original["execution_guidance"]["profile"] == "none"
    assert original["execution_guidance"]["recorded"] is False
    assert original["planner_profile"] == "none"
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(
        data_profile={
            "name": "planning-metadata-v1", "visibility_only": True,
            "field_sources": "private-data-provenance",
        },
        execution_guidance={
            "profile": guidance_profile, "version": 1, "sha256": "a" * 64,
            "text": "private-prompt-text", "extra": "private-guidance-extra",
        },
        planner={
            "profile": planner_profile, "enabled": True, "read_only": True,
            "extra": "private-planner-extra",
        },
    )
    manifest_path.write_text(json.dumps(manifest))
    detail = run_detail(tmp_path, "profiled")
    assert detail["data_profile"] == "planning-metadata-v1"
    assert detail["data_profile_label"] == "规划参数可见性变体"
    assert detail["execution_guidance"] == {
        "profile": guidance_profile, "label": guidance_label,
        "version": 1, "sha256": "a" * 64, "recorded": True,
    }
    assert detail["planner_profile"] == planner_profile
    assert detail["planner_label"] == planner_label
    assert detail["planner_recorded"] is True
    assert detail["trace"]["status"] == "unavailable"  # Configuration is not proof of tool use.
    encoded = json.dumps(detail)
    for private in ("private-data-provenance", "private-prompt-text",
                    "private-guidance-extra", "private-planner-extra"):
        assert private not in encoded
    assert detail["score"] == original["score"] == 100


def test_invalid_profile_values_stay_unknown_instead_of_becoming_original_data(tmp_path):
    run_dir = write_run(tmp_path, "malformed-profiles", "pi", 0)
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(
        data_profile={"name": "secret-data-profile"},
        execution_guidance={"profile": "secret-guidance", "version": True, "sha256": "secret-digest"},
        planner={"profile": "secret-planner"},
    )
    manifest_path.write_text(json.dumps(manifest))
    record = run_detail(tmp_path, "malformed-profiles")
    assert record["data_profile"] == "unknown"
    assert record["execution_guidance"]["profile"] == "unknown"
    assert record["execution_guidance"]["sha256"] is None
    assert record["planner_profile"] == "unknown"
    assert "secret-" not in json.dumps(record)


def test_groups_never_mix_data_guidance_planner_or_provider_default_with_missing_budget(tmp_path):
    cases = [
        ("original-fail", "original", "none", "none", 16384, "adapter_default", 0),
        ("original-pass", "original", "none", "none", 16384, "explicit_run_argument", 100),
        ("visibility-pass", "planning-metadata-v1", "none", "none", 16384, "explicit_run_argument", 100),
        ("guided-pass", "original", "odoo-read-v1", "none", 16384, "explicit_run_argument", 100),
        ("guided-32k-fail", "original", "odoo-read-v1", "none", 32768, "explicit_run_argument", 0),
        ("provider-default", "original", "none", "none", None, "provider_default", 100),
        ("historical-budget-missing", "original", "none", "none", None, None, 0),
        ("planner-pass", "original", "none", "cp-sat-live-v1", 16384, "explicit_run_argument", 100),
        ("native-planner-fail", "original", "none", "cp-sat-native-v1", 16384, "explicit_run_argument", 0),
        ("schedule-guided-fail", "original", "odoo-schedule-v1", "none", 16384, "explicit_run_argument", 0),
        ("feedback-planner-fail", "original", "none", "cp-sat-native-v2", 16384, "explicit_run_argument", 0),
    ]
    for name, data, guidance, planner, limit, source, score in cases:
        manifest_path = write_run(tmp_path, name, "pi", score) / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(
            data_profile={"name": data},
            execution_guidance={
                "profile": guidance, "version": 1 if guidance != "none" else None,
                "sha256": "a" * 64 if guidance != "none" else None,
            },
            planner={"profile": planner},
            budget={
                "max_output_tokens": limit, "max_output_tokens_source": source,
                "context_window_tokens": 131072, "agent_timeout_sec": 3600,
                "sampling_params": {"temperature": 1.0, "top_p": 0.95},
            },
        )
        manifest_path.write_text(json.dumps(manifest))
    snapshot = build_snapshot(tmp_path)
    groups = {
        (group["data_profile"], group["execution_guidance"]["profile"],
         group["planner_profile"], group["max_output_tokens"], group["max_output_tokens_source"]): group
        for group in snapshot["agent_groups"]
    }
    assert len(groups) == 10
    baseline = groups[("original", "none", "none", 16384, "recorded_limit")]
    assert baseline["measured_agent_runs"] == 2
    assert baseline["strict_agent_successes"] == 1
    assert groups[("planning-metadata-v1", "none", "none", 16384, "recorded_limit")]["strict_agent_successes"] == 1
    assert groups[("original", "none", "none", None, "provider_default")]["measured_agent_runs"] == 1
    assert groups[("original", "none", "none", None, "unrecorded")]["measured_agent_runs"] == 1
    assert groups[("original", "none", "cp-sat-live-v1", 16384, "recorded_limit")]["measured_agent_runs"] == 1
    native = groups[("original", "none", "cp-sat-native-v1", 16384, "recorded_limit")]
    assert native["measured_agent_runs"] == 1
    assert native["strict_agent_successes"] == 0
    assert native["planner_label"] == "CP-SAT Pi 原生工具入口"
    provider_default = run_detail(tmp_path, "provider-default")
    assert provider_default["max_output_tokens"] is None
    assert provider_default["max_output_tokens_source"] == "provider_default"
    assert run_detail(tmp_path, "historical-budget-missing")["max_output_tokens_source"] is None


def test_output_policy_versions_split_groups_without_upgrading_historical_records(tmp_path):
    manifests = {}
    for name, policy_version, score in (
        ("older-provider-default", None, 0),
        ("current-provider-default", "all-requests-v2", 100),
    ):
        path = write_run(tmp_path, name, "pi", score) / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["budget"] = {
            "max_output_tokens": None,
            "max_output_tokens_source": "provider_default",
            "context_window_tokens": 131072,
            "agent_timeout_sec": 3600,
        }
        if policy_version is not None:
            manifest["budget"]["output_policy_version"] = policy_version
        path.write_text(json.dumps(manifest))
        manifests[path] = path.read_bytes()
    snapshot = build_snapshot(tmp_path)
    groups = {group["output_policy_version"]: group for group in snapshot["agent_groups"]}
    assert set(groups) == {None, "all-requests-v2"}
    assert groups[None]["measured_agent_runs"] == 1
    assert groups[None]["strict_agent_successes"] == 0
    assert groups["all-requests-v2"]["measured_agent_runs"] == 1
    assert groups["all-requests-v2"]["strict_agent_successes"] == 1
    assert run_detail(tmp_path, "older-provider-default")["output_policy_version"] is None
    assert run_detail(tmp_path, "current-provider-default")["output_policy_version"] == "all-requests-v2"
    assert all(path.read_bytes() == content for path, content in manifests.items())


def test_thinking_modes_split_groups_without_assuming_historical_or_unknown_settings(tmp_path):
    before = {}
    for name, mode, score in (
        ("historical-thinking", None, 0),
        ("default-thinking", "provider-default", 0),
        ("off-thinking", "off", 100),
        ("malformed-thinking", "private-unknown-mode", 0),
    ):
        path = write_run(tmp_path, name, "pi", score) / "manifest.json"
        manifest = json.loads(path.read_text())
        if mode is not None:
            manifest["thinking_mode"] = mode
        path.write_text(json.dumps(manifest))
        before[path] = path.read_bytes()
    snapshot = build_snapshot(tmp_path)
    groups = {group["thinking_mode"]: group for group in snapshot["agent_groups"]}
    assert set(groups) == {None, "provider-default", "off"}
    assert groups[None]["measured_agent_runs"] == 2
    assert groups[None]["strict_agent_successes"] == 0
    assert groups["off"]["measured_agent_runs"] == 1
    assert groups["off"]["strict_agent_successes"] == 1
    assert groups["provider-default"]["strict_agent_successes"] == 0
    assert run_detail(tmp_path, "historical-thinking")["thinking_mode"] is None
    assert run_detail(tmp_path, "malformed-thinking")["thinking_mode"] is None
    assert "private-unknown-mode" not in json.dumps(snapshot)
    assert all(path.read_bytes() == content for path, content in before.items())


def test_empty_and_malformed_records_do_not_create_successes(tmp_path):
    assert build_snapshot(tmp_path)["counts"]["measured_agent_runs"] == 0
    run_dir = tmp_path / "runs/broken"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text("{broken")
    assert build_snapshot(tmp_path)["runs"] == []
    assert run_detail(tmp_path, "broken") is None


def test_viewer_rejects_path_traversal_and_symlinked_results(tmp_path):
    write_run(tmp_path, "legitimate", "oracle", 100)
    assert run_detail(tmp_path, "../../.env") is None
    assert run_detail(tmp_path, "../legitimate") is None
    (tmp_path / "runs/linked").symlink_to(tmp_path / "runs/legitimate", target_is_directory=True)
    assert run_detail(tmp_path, "linked") is None
    assert len(build_snapshot(tmp_path)["runs"]) == 1
    (tmp_path / "runs/legitimate/sensitive-link").symlink_to(tmp_path / ".env")
    assert run_detail(tmp_path, "legitimate") is None


@pytest.fixture
def local_server(tmp_path):
    server = make_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_http_surface_only_serves_read_only_result_routes(local_server, tmp_path):
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=private-key")
    base = f"http://127.0.0.1:{local_server.server_port}"
    with httpx.Client(base_url=base, trust_env=False) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "ForgeFlow" in page.text
        assert "default-src 'self'" in page.headers["Content-Security-Policy"]
        assert client.get("/app.js").headers["Content-Type"].startswith("text/javascript")
        assert client.get("/style.css").headers["Content-Type"].startswith("text/css")
        assert client.get("/api/runs").json()["runs"] == []
        for path in ("/.env", "/runs", "/vendor", "/api/runs/..%2F..%2F.env"):
            assert client.get(path).status_code == 404
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert client.request(method, "/api/runs").status_code == 405
        assert client.get("/api/runs", headers={"Host": "external.example"}).status_code == 403
    assert local_server.server_address[0] == "127.0.0.1"


def test_invalid_scores_and_untrusted_labels_cannot_become_successful_html(
    local_server, tmp_path
):
    run_dir = write_run(tmp_path, "invalid-score", "pi", 100)
    reward_path = run_dir / "harbor/job/trial/verifier/reward.json"
    reward = json.loads(reward_path.read_text())
    reward["overall_score"] = float("nan")
    reward_path.write_text(json.dumps(reward))
    payload = '<script src="https://external.example/steal.js"></script>'
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["model"]["model"] = payload
    manifest_path.write_text(json.dumps(manifest))
    base = f"http://127.0.0.1:{local_server.server_port}"
    with httpx.Client(base_url=base, trust_env=False) as client:
        snapshot = client.get("/api/runs").json()
        assert snapshot["counts"]["strict_agent_successes"] == 0
        assert snapshot["counts"]["measured_agent_runs"] == 0
        assert snapshot["runs"][0]["score"] is None
        assert snapshot["runs"][0]["strict_success"] is None
        assert snapshot["runs"][0]["model"] == payload
        detail = client.get("/api/runs/invalid-score")
        assert detail.headers["Content-Type"].startswith("application/json")
        page = client.get("/")
        assert payload not in page.text
        assert "https://" not in page.text
        assert "script-src 'self'" in page.headers["Content-Security-Policy"]
