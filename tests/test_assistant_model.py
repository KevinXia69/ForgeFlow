from forgeflow.api.model_settings import assistant_model
from forgeflow.vps_runtime import dsh_patch


def test_default_binding_is_deepseek_flash() -> None:
    assert assistant_model({}) == {
        "profile": "deepseek_flash",
        "provider": "deepseek",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
    }


def test_deepseek_profile_uses_its_own_key_variable() -> None:
    spec = assistant_model({"FORGEFLOW_ASSISTANT_PROFILE": "deepseek_flash"})
    assert spec["provider"] == "deepseek"
    assert spec["model"] == "deepseek-flash"
    assert spec["base_url"] == "https://api.deepseek.com/v1"
    assert spec["api_key_env"] == "DEEPSEEK_API_KEY"


def test_unreviewed_profile_falls_back_and_explicit_overrides_apply() -> None:
    spec = assistant_model({
        "FORGEFLOW_ASSISTANT_PROFILE": "unreviewed",
        "FORGEFLOW_ASSISTANT_MODEL": "custom-model",
        "FORGEFLOW_ASSISTANT_BASE_URL": "https://example.invalid/v1",
    })
    assert spec["profile"] == "deepseek_flash"
    assert spec["provider"] == "deepseek"
    assert spec["model"] == "custom-model"
    assert spec["base_url"] == "https://example.invalid/v1"


def test_patch_binds_provider_and_web_tools_without_enabling_broad_tools() -> None:
    patch = dsh_patch(
        {"python": "/test/python", "repo_root": "/repo"},
        "deepseek-flash", "https://api.deepseek.com/v1",
        provider="deepseek", api_key_env="DEEPSEEK_API_KEY",
    )
    model = next(row["config"] for row in patch if row.get("id") == "agent-default-model")
    llm = next(row["config"] for row in patch if row.get("id") == "llm-pi-ai")
    assert model == {"provider": "deepseek", "model": "deepseek-flash"}
    assert llm["providers"]["deepseek"]["apiKeyEnv"] == "DEEPSEEK_API_KEY"
    assert llm["providers"]["deepseek"]["baseURL"] == "https://api.deepseek.com/v1"
    disabled = {row["id"] for row in patch if row.get("disabled") is True}
    for name in ("tool-bash", "tool-subagent", "tool-workflow"):
        assert name in disabled
    assert "tool-web" not in disabled
    # Only the owner-scoped composition registers read_image through its guard.
    assert "tool-fs" in disabled
    file_tools = next(row for row in patch if row.get("id") == "tool-fs")
    assert file_tools["config"] == {"read": False, "readImage": True, "write": False, "edit": False}
    web = next(row for row in patch if row.get("id") == "web")
    search = next(row for row in patch if row.get("id") == "web-search-deepseek")
    assert web["name"] == "@deepseek-ai/dsh-web"
    assert search["name"] == "@deepseek-ai/dsh-web-search-deepseek"
    assert search["config"]["searchProvider"] == "deepseek-official"
    assert any(row.get("id") == "web-fetch-http" for row in patch)


def test_web_search_sources_become_citations_and_have_a_visible_label() -> None:
    from forgeflow.api.dsh_harness import _tool_label, _web_citations

    urls = _web_citations("See https://example.test/report, and https://example.test/report#x.")
    assert urls == ["https://example.test/report", "https://example.test/report#x"]
    assert _tool_label("web_search", "query") == "query"
    assert _tool_label("web_search", "") == ""
    assert _tool_label("web_fetch", "https://example.test") == "https://example.test"
    assert _tool_label("read_image", "/private/path.png") == "attached image"
