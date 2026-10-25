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


def test_assistant_question_cannot_become_a_cli_option(tmp_path):
    harness = DshHarness(cli=tmp_path, patch=tmp_path, home=tmp_path, workspace=tmp_path)
    command = harness._command("--patch=/untrusted", None)
    assert command[-2:] == ["--", "--patch=/untrusted"]
