"""Optional model credentials stay in isolated process environments, never files."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from forgeflow.api.dsh_harness import DshHarness, DshUnavailable
from forgeflow.api.dsh_permissions import DISABLED_MODULES
from forgeflow.api.dsh_sdk import SdkDshHarness
from forgeflow.api.model_settings import ModelSettings
from forgeflow.dsh_launcher import assistant_key_env, assistant_launch_environment, launch_arguments
from forgeflow.vps_runtime import dsh_patch


class Inspection(BaseModel):
    model_config = ConfigDict(extra="allow")


def setting(key="synthetic-user-key-one", **overrides):
    values = dict(profile="custom", base_url="https://example.invalid/v1", model="synthetic-model",
                  api_key=key, updated_at="synthetic", api="openai-completions", provider="openai",
                  image_input=True)
    return ModelSettings(**{**values, **overrides})


def scoped(tmp_path, cls=DshHarness, *, cli=None, node="node"):
    base = tmp_path / "base.json"
    base.write_text(json.dumps(dsh_patch(
        {"python": sys.executable, "repo_root": str(tmp_path)}, "deepseek-flash",
        "https://api.deepseek.com/v1", provider="deepseek", api_key_env="DEEPSEEK_API_KEY")))
    evidence = tmp_path / "owner-1234.json"
    evidence.write_text('{"synthetic":true}')
    instance = cls(cli=cli or tmp_path / "cli.js", patch=base, home=tmp_path / "home",
                   workspace=tmp_path, node=node).with_evidence(evidence)
    instance.evidence_scope_path = evidence
    return instance


@pytest.mark.parametrize("cls", [DshHarness, SdkDshHarness])
def test_model_binding_requires_previously_authorized_evidence_scope(tmp_path, cls):
    harness = cls(cli=tmp_path, patch=tmp_path, home=tmp_path, workspace=tmp_path)
    with pytest.raises(DshUnavailable, match="dsh_model_requires_evidence_scope"):
        harness.with_model(setting())


@pytest.mark.parametrize("cls", [DshHarness, SdkDshHarness])
def test_parallel_keys_do_not_replace_default_scope_or_each_other(tmp_path, monkeypatch, cls):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-official-search-key")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-other-tenant-key")
    original_env = dict(os.environ)
    default = scoped(tmp_path, cls)
    original = default.patch.read_bytes()
    keys = [f"synthetic-private-key-{i}" for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        instances = list(pool.map(lambda key: default.with_model(setting(key)), keys))
    try:
        assert len({item.patch for item in instances}) == len(keys)
        assert len({item.sessions_file for item in instances}) == len(keys)
        assert len({item._session_namespace for item in instances}) == len(keys)
        assert default.patch.read_bytes() == original and dict(os.environ) == original_env
        for item, key in zip(instances, keys):
            assert item.evidence_path == item.evidence_scope_path == default.evidence_path
            assert item.image_root == default.image_root
            assert item.knowledge_root == default.knowledge_root
            rows = json.loads(item.patch.read_text())
            profile = [row for row in rows if row.get("id") == "llm-pi-ai"][-1]["config"]["providers"]["openai"]
            assert profile["apiKeyEnv"] == "FORGEFLOW_CHAT_API_KEY"
            assert "reasoning" not in profile
            assert profile["models"][0]["input"] == ["text", "image"]
            assert set(DISABLED_MODULES) <= {row["id"] for row in rows if row.get("disabled") is True}
            guard = next(row["insert"][0] for row in rows if row.get("insert", [{}])[0].get("id") == "forgeflow-image-guard")
            assert guard["config"]["root"] == str(default.image_root)
            assert item.patch.stat().st_mode & 0o077 == 0
            if cls is DshHarness:
                env = item._env()
                assert env["FORGEFLOW_CHAT_API_KEY"] == key
                assert env["DEEPSEEK_API_KEY"] == "synthetic-official-search-key"
                assert "OPENAI_API_KEY" not in env
            for path in tmp_path.glob("*.json"):
                assert not any(value in path.read_text() for value in keys)
        first = instances[0]
        first.close()
        assert not first.patch.exists() and first._model_api_key is None
        assert instances[1].patch.is_file() and default.patch.is_file()
        operation = first._env if cls is DshHarness else first._runtime
        with pytest.raises(DshUnavailable, match="dsh_model_binding_closed"):
            operation()
    finally:
        for item in instances:
            item.close()
        assert default.evidence_path.is_file() and default.patch.is_file()


def test_sdk_session_namespaces_do_not_cross_models_or_credentials(tmp_path):
    default = scoped(tmp_path, SdkDshHarness)
    first, second = default.with_model(setting()), default.with_model(setting("synthetic-other-key"))
    started = []

    def start_session(sid):
        started.append(sid)
        return SimpleNamespace(session_id=sid)

    try:
        for item in (first, second):
            item._runtime = lambda: SimpleNamespace(start_session=start_session)
        one = first._session("same-conversation")
        assert first._session("same-conversation") is one
        two = second._session("same-conversation")
        assert one.session_id != two.session_id and len(started) == 2
        for item in (first, second):
            assert "synthetic-user-key" not in item.sessions_file.read_text()
    finally:
        for item in (first, second):
            item.close()
            assert not item.sessions_file.exists()


@pytest.mark.parametrize("api,provider,has_api,has_models", [
    ("openai-completions", "custom", True, True),
    ("openai-responses", "openai", True, True),
    ("anthropic-messages", "anthropic", True, True),
    ("google-generative-ai", "google", False, False),
])
def test_model_protocol_metadata_does_not_inherit_deployment_fields(tmp_path, api, provider, has_api, has_models):
    default = scoped(tmp_path)
    bound = default.with_model(setting(api=api, provider=provider, image_input=False))
    try:
        rows = json.loads(bound.patch.read_text())
        profiles = [row["config"]["providers"] for row in rows if row.get("id") == "llm-pi-ai"]
        assert all(not row for row in profiles[:-1])
        profile = profiles[-1][provider]
        assert ("api" in profile) is has_api and ("models" in profile) is has_models
        if has_models:
            assert profile["models"][0]["input"] == ["text"]
    finally:
        bound.close()


def test_byok_deepseek_keeps_max_and_independent_search_credential(tmp_path):
    default = scoped(tmp_path)
    bound = default.with_model(setting(provider="deepseek", model="deepseek-flash"))
    try:
        rows = json.loads(bound.patch.read_text())
        provider = [row for row in rows if row.get("id") == "llm-pi-ai"][-1]["config"]["providers"]["deepseek"]
        assert provider["reasoning"] == "max" and provider["apiKeyEnv"] == "FORGEFLOW_CHAT_API_KEY"
        original = next(row for row in json.loads(default.patch.read_text())
                        if row.get("id") == "llm-pi-ai")["config"]["providers"]["deepseek"]
        # Same model spec as the pinned-adapter wire capture which preserves
        # catalog reasoning efforts while the profile selects Max.
        assert provider["models"] == original["models"]
        env = assistant_launch_environment({"home": str(tmp_path), "api_key_env": "DEEPSEEK_API_KEY"},
            bound.evidence_path, bound.patch, source={"FORGEFLOW_CHAT_API_KEY": "user-chat-key",
                "DEEPSEEK_API_KEY": "official-search-key", "ANTHROPIC_API_KEY": "unrelated-key"})
        assert env["FORGEFLOW_CHAT_API_KEY"] == "user-chat-key"
        assert env["DEEPSEEK_API_KEY"] == "official-search-key"
        assert "ANTHROPIC_API_KEY" not in env
    finally:
        bound.close()


def test_launcher_allows_only_owner_bound_random_patch_and_exact_byok_key(tmp_path, monkeypatch):
    bound = scoped(tmp_path).with_model(setting())
    monkeypatch.setattr(os, "geteuid", lambda: 123456)
    config = {"node": "/usr/bin/node", "cli": str(bound.cli), "evidence_root": str(tmp_path),
              "api_key_env": "DEEPSEEK_API_KEY"}
    arguments = [str(bound.cli), "--profile", "sdk", "--patch", str(bound.patch)]
    try:
        assert launch_arguments(arguments, config, bound.evidence_path)[-1] == str(bound.patch)
        assert assistant_key_env(bound.patch, config) == "FORGEFLOW_CHAT_API_KEY"
        with pytest.raises(ValueError, match="assistant_model_key_required"):
            assistant_launch_environment({**config, "home": str(tmp_path)}, bound.evidence_path,
                                        bound.patch, source={"DEEPSEEK_API_KEY": "official-key"})
        other = tmp_path / ("different-owner.byok-" + "a" * 32 + ".patch.json")
        other.write_bytes(bound.patch.read_bytes())
        with pytest.raises(ValueError, match="invalid_assistant_scope"):
            launch_arguments([*arguments[:-1], str(other)], config, bound.evidence_path)
        with pytest.raises(ValueError, match="invalid_assistant_scope"):
            launch_arguments([*arguments[:-1], str(tmp_path / "owner-1234.byok-notrandom.patch.json")],
                             config, bound.evidence_path)
    finally:
        bound.close()


def test_real_locked_sdk_and_cli_receive_only_their_own_optional_key(tmp_path, monkeypatch):
    # The real SDK creates/initializes a local JSON-RPC child. No model network,
    # ERP, constructor replacement or plaintext-key result is involved.
    cli = tmp_path / "synthetic-runtime.py"
    cli.write_text('''import hashlib,json,os,sys
view={"key_digest":hashlib.sha256(os.environ.get("FORGEFLOW_CHAT_API_KEY","").encode()).hexdigest(),
      "search_key_present":bool(os.environ.get("DEEPSEEK_API_KEY")),
      "unrelated_key_present":bool(os.environ.get("ANTHROPIC_API_KEY")),
      "erp_key_present":bool(os.environ.get("FORGEFLOW_ODOO20_PASSWORD")),
      "database_url_present":bool(os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL")),
      "evidence":os.environ.get("FORGEFLOW_ASSISTANT_EVIDENCE"),
      "permission_mode":os.environ.get("DSH_PERMISSION_MODE")}
if "headless" in sys.argv:
 print(json.dumps(view));sys.exit(0)
for line in sys.stdin:
 request=json.loads(line); method=request.get("method")
 if method=="initialize":view["initialized"]=request["params"]
 print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":view if method=="inspect" else {}}),flush=True)
 if method=="shutdown":break
''')
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-independent-search")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-unrelated-secret")
    monkeypatch.setenv("FORGEFLOW_ODOO20_PASSWORD", "synthetic-erp-password")
    monkeypatch.setenv("FORGEFLOW_CONTROL_DATABASE_URL", "postgresql://synthetic-secret")
    for cls in (DshHarness, SdkDshHarness):
        default = scoped(tmp_path, cls, cli=cli, node=sys.executable)
        bound = default.with_model(setting())
        try:
            if cls is SdkDshHarness:
                view = bound._runtime().client.request("inspect", None, response_model=Inspection).model_dump()
                assert view["initialized"]["provider"] == "openai"
                assert view["initialized"]["model"] == "synthetic-model"
                assert "reasoningEffort" not in view["initialized"]
            else:
                result = subprocess.run(bound._command("Synthetic greeting", None), env=bound._env(),
                                        capture_output=True, text=True, check=True, timeout=10)
                view = json.loads(result.stdout)
            assert view["key_digest"] == hashlib.sha256(setting().api_key.encode()).hexdigest()
            assert view["search_key_present"] and not view["unrelated_key_present"]
            assert not view["erp_key_present"] and not view["database_url_present"]
            assert view["evidence"] == str(bound.evidence_path)
            assert view["permission_mode"] == "read-only"
            assert setting().api_key not in bound.patch.read_text()
        finally:
            bound.close()


def test_real_sdk_initialize_failure_reaps_child_and_clears_runtime(tmp_path):
    pid_file = tmp_path / "child.pid"
    cli = tmp_path / "reject-initialize.py"
    cli.write_text('import json,os,sys\n'
                   f'open({str(pid_file)!r},"w").write(str(os.getpid()))\n'
                   'for line in sys.stdin:\n'
                   ' request=json.loads(line)\n'
                   ' reply={"jsonrpc":"2.0","id":request["id"]}\n'
                   ' if request["method"]=="initialize":reply["error"]={"code":-32000,"message":"synthetic-refusal"}\n'
                   ' else:reply["result"]={}\n'
                   ' print(json.dumps(reply),flush=True)\n'
                   ' if request["method"]=="shutdown":break\n')
    bound = scoped(tmp_path, SdkDshHarness, cli=cli, node=sys.executable).with_model(setting())
    try:
        with pytest.raises(Exception, match="synthetic-refusal"):
            bound._runtime()
        assert bound._harness is None
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        bound.close()
