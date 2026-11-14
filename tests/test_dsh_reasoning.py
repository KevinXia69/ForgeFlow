"""Shared reasoning policy reaches both transports without widening child access."""
from __future__ import annotations

import json

import pytest

from forgeflow.api.dsh_harness import DshHarness
from forgeflow.api.dsh_permissions import DISABLED_MODULES, scoped_patch
from forgeflow.api.dsh_sdk import SdkDshHarness
from forgeflow.vps_runtime import dsh_patch


def base_patch(tmp_path, *, provider="deepseek", model="deepseek-flash"):
    path = tmp_path / "base.json"
    path.write_text(json.dumps(dsh_patch(
        {"python": "/usr/bin/python3", "repo_root": str(tmp_path)}, model,
        "https://example.invalid/v1", provider=provider, api_key_env="SYNTHETIC_MODEL_KEY")))
    return path


def profile(path, provider="deepseek"):
    rows = json.loads(path.read_text())
    return next(row for row in rows if row.get("id") == "llm-pi-ai")["config"]["providers"][provider]


@pytest.mark.parametrize("harness_class", [DshHarness, SdkDshHarness])
@pytest.mark.parametrize("effort", [None, "off", "high", "max"])
def test_transport_scope_uses_shared_default_or_explicit_effort(tmp_path, harness_class, effort):
    base = base_patch(tmp_path)
    original = base.read_bytes()
    evidence = tmp_path / "owner-a-revision.json"
    evidence.write_text("{}")
    harness = harness_class(cli=tmp_path / "cli.js", patch=base, home=tmp_path / "home",
                            workspace=tmp_path / "workspace", reasoning_effort=effort)
    scoped = harness.with_evidence(evidence)
    assert scoped.reasoning_effort == effort
    assert profile(scoped.patch)["reasoning"] == (effort or "max")
    assert profile(scoped.patch)["models"] == profile(base)["models"]
    assert base.read_bytes() == original
    rows = json.loads(scoped.patch.read_text())
    assert set(DISABLED_MODULES) <= {row["id"] for row in rows if row.get("disabled") is True}
    if harness_class is DshHarness:
        assert "FORGEFLOW_DSH_REASONING_EFFORT" not in scoped._env()
        assert scoped._command("Synthetic question", None)[2:7] == [
            "--profile", "headless", "--patch", str(scoped.patch), "--json"]


@pytest.mark.parametrize("sdk", [False, True])
@pytest.mark.parametrize("effort", [None, "off", "high", "max"])
def test_runtime_explicit_setting_reaches_either_transport(tmp_path, monkeypatch, sdk, effort):
    monkeypatch.setenv("FORGEFLOW_DSH_ENABLED", "1")
    monkeypatch.setenv("FORGEFLOW_DSH_SDK", "1" if sdk else "0")
    monkeypatch.setenv("FORGEFLOW_DSH_CLI", str(tmp_path / "cli.js"))
    monkeypatch.setenv("FORGEFLOW_DSH_PATCH", str(base_patch(tmp_path)))
    monkeypatch.setenv("FORGEFLOW_DSH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FORGEFLOW_DSH_WORKSPACE", str(tmp_path / "workspace"))
    if effort is None:
        monkeypatch.delenv("FORGEFLOW_DSH_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("FORGEFLOW_DSH_REASONING_EFFORT", effort)
    harness = DshHarness.from_env()
    assert isinstance(harness, SdkDshHarness if sdk else DshHarness)
    assert harness.reasoning_effort == effort
    evidence = tmp_path / "owner-revision.json"
    evidence.write_text("{}")
    assert profile(harness.with_evidence(evidence).patch)["reasoning"] == (effort or "max")


def test_explicit_override_is_owner_bound_and_does_not_change_other_routes(tmp_path):
    base = base_patch(tmp_path)
    rows = json.loads(base.read_text())
    next(row for row in rows if row.get("id") == "llm-pi-ai")["config"]["providers"]["other"] = {
        "models": [{"id": "unverified-custom-model"}], "reasoning": "low"}
    base.write_text(json.dumps(rows))
    off = scoped_patch(base, tmp_path / "owner-a.json", reasoning_effort="off")
    high = scoped_patch(base, tmp_path / "owner-b.json", reasoning_effort="high")
    assert off != high
    assert profile(off)["reasoning"] == "off"
    assert profile(high)["reasoning"] == "high"
    assert profile(base)["reasoning"] == "max"
    assert profile(off, "other") == profile(high, "other") == profile(base, "other")


@pytest.mark.parametrize("effort", ["unknown-level", "low"])
def test_unknown_or_unsupported_effort_is_not_silently_changed(tmp_path, effort):
    # The installed adapter rejects unknown-level and low for v4-pro. Keep the
    # value intact here, rather than silently falling back to max/off or adding
    # a reasoning capability to make an unsupported choice appear valid.
    base = base_patch(tmp_path, model="deepseek-v4-pro")
    target = scoped_patch(base, tmp_path / "owner.json", reasoning_effort=effort)
    assert profile(target)["reasoning"] == effort
    assert profile(target)["models"] == profile(base)["models"]


def test_missing_reasoning_route_refuses_override_instead_of_ignoring_it(tmp_path):
    base = base_patch(tmp_path)
    rows = [row for row in json.loads(base.read_text()) if row.get("id") != "agent-default-model"]
    base.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="missing_assistant_reasoning_provider"):
        scoped_patch(base, tmp_path / "owner.json", reasoning_effort="off")
