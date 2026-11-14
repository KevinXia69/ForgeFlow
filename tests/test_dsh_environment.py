from pathlib import Path

from forgeflow.api.dsh_environment import assistant_environment
from forgeflow.api.dsh_harness import DshHarness


def test_assistant_environment_drops_api_erp_and_runtime_loader_secrets():
    names = ["FORGEFLOW_SESSION_SECRET", "FORGEFLOW_INVITE_CODES", "ERP_API_KEY",
             "DATABASE_URL", "DOCKER_HOST", "DOCKER_CONFIG", "PYTHONPATH", "NODE_OPTIONS",
             "LD_PRELOAD", "FORGEFLOW_APPROVALS_ROOT", "FORGEFLOW_LOCAL_SOCKET"]
    source = dict.fromkeys(names, "private-canary")
    source.update(NEBIUS_API_KEY="model-key", PATH="/usr/bin", HOME="/root")
    result = assistant_environment(source, home=Path("/assistant"), evidence=Path("/evidence.json"))
    assert not set(names) & result.keys()
    assert result["NEBIUS_API_KEY"] == "model-key"
    assert result["HOME"] == result["DSH_HOME"] == "/assistant"
    assert result["FORGEFLOW_ASSISTANT_EVIDENCE"] == "/evidence.json"


def test_native_modules_load_from_installed_files_without_caller_cache_override():
    source = {"NARB_DISABLE_NATIVE_CACHE": "0", "NARB_NATIVE_CACHE_DIR": "/untrusted"}
    result = assistant_environment(source, home=Path("/assistant"))
    assert result["NARB_DISABLE_NATIVE_CACHE"] == "1"
    assert "NARB_NATIVE_CACHE_DIR" not in result


def test_platform_fixed_chat_slot_requires_explicit_deployment_selection():
    source = {"FORGEFLOW_CHAT_API_KEY": "synthetic-installed-chat-key",
              "OPENAI_API_KEY": "unselected-key", "DEEPSEEK_API_KEY": "search-key"}
    assert "FORGEFLOW_CHAT_API_KEY" not in assistant_environment(source, home=Path("/assistant"))
    source["FORGEFLOW_ASSISTANT_API_KEY_ENV"] = "FORGEFLOW_CHAT_API_KEY"
    result = assistant_environment(source, home=Path("/assistant"))
    assert result["FORGEFLOW_CHAT_API_KEY"] == "synthetic-installed-chat-key"
    assert result["DEEPSEEK_API_KEY"] == "search-key"
    assert "OPENAI_API_KEY" not in result
    assert "FORGEFLOW_ASSISTANT_API_KEY_ENV" not in result


def test_user_model_key_does_not_replace_platform_key_or_search_key():
    source = {"FORGEFLOW_ASSISTANT_API_KEY_ENV": "FORGEFLOW_CHAT_API_KEY",
              "FORGEFLOW_CHAT_API_KEY": "synthetic-platform-key", "DEEPSEEK_API_KEY": "search-key"}
    result = assistant_environment(source, home=Path("/assistant"), model_api_key="synthetic-user-key")
    assert result["FORGEFLOW_CHAT_API_KEY"] == "synthetic-user-key"
    assert result["DEEPSEEK_API_KEY"] == "search-key"
    assert source["FORGEFLOW_CHAT_API_KEY"] == "synthetic-platform-key"


def test_assistant_question_cannot_become_a_cli_option(tmp_path):
    harness = DshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path, workspace=tmp_path)
    command = harness._command("--patch=/untrusted", None)
    assert command[-2:] == ["--", "--patch=/untrusted"]
