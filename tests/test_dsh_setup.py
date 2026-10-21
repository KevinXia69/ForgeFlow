from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from forgeflow.api.dsh_permissions import DISABLED_MODULES, DISABLED_TOOLS

REPO = Path(__file__).resolve().parents[1]


def _load(name: str, relative: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = _load("setup_dsh_harness", REPO / "scripts" / "setup_dsh_harness.py")
workspace = _load("assistant_workspace", REPO / "scripts" / "assistant_workspace.py")


def test_generated_patch_disables_broad_file_and_web_tools(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(setup, "PATCH", tmp_path / "dsh-nebius.yml")
    setup._write_patch()
    text = (tmp_path / "dsh-nebius.yml").read_text(encoding="utf-8")
    assert setup.DISABLED_TOOLS == DISABLED_TOOLS
    assert setup.DISABLED_MODULES == DISABLED_MODULES
    for tool in DISABLED_MODULES:
        assert f"- id: {tool}\n  disabled: true" in text
    assert "- id: mcp-forgeflow" in text
    assert "serverName: forgeflow" in text


def test_generated_patch_carries_forgeflow_persona(tmp_path, monkeypatch) -> None:
    patch = tmp_path / "dsh-nebius.yml"
    monkeypatch.setattr(setup, "PATCH", patch)
    setup._write_patch()
    text = patch.read_text(encoding="utf-8")

    lines = text.splitlines()
    assert "- id: system-prompt" in lines
    assert lines.index("- id: system-prompt") < lines.index("- insert:")
    assert "    personaPrefix: |" in lines
    assert "    personaSuffix: |" in lines
    assert "You are ForgeFlow's production-exception assistant" in text
    assert "- id: agent-instructions" in lines
    assert "      - .git" in lines
    assert f"      - {setup.AGENT_INSTRUCTIONS_MARKER}" in lines
    assert setup.AGENT_INSTRUCTIONS_MARKER == workspace.ROOT_MARKER

    persona = f"{setup.PERSONA_PREFIX}\n{setup.PERSONA_SUFFIX}"
    assert 150 <= len(persona.split()) <= 250
    assert "{{" not in persona and "}}" not in persona
    lowered = " ".join(persona.lower().split())
    for phrase in (
        "production-exception assistant",
        "read-only",
        "not a coding agent",
        "cite only",
        "never imply",
        "unless the case record shows it",
        "must not ask",
        "at most one natural next question",
    ):
        assert phrase in lowered


def test_trim_agents_drops_engineering_prefix() -> None:
    source = (
        "# ForgeFlow working instructions\n\n"
        "- Develop ForgeFlow in this workspace.\n"
        "- Never expose `optimal_plan.json`.\n\n"
        "---\n\n"
        "## Read-only project assistant mode\n\n"
        "# ForgeFlow project agent instructions\n\n"
        "You are the ForgeFlow project assistant.\n"
    )
    trimmed = workspace.trim_agents(source)
    assert trimmed.startswith("# ForgeFlow project agent instructions")
    assert "Develop ForgeFlow" not in trimmed
    assert "## Read-only project assistant mode" not in trimmed
    assert "You are the ForgeFlow project assistant." in trimmed


def test_trim_agents_rejects_unmarked_file() -> None:
    with pytest.raises(ValueError, match="Read-only project assistant mode"):
        workspace.trim_agents("# ForgeFlow working instructions\n\n- Develop ForgeFlow.\n")


def test_sync_layers_agents_and_excludes_private_state(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / ".env").write_text("SECRET=not-real\n", encoding="utf-8")
    (root / "PROJECT_MEMORY.md").write_text("# Project memory\n", encoding="utf-8")
    (root / "AGENTS.md").write_text(
        "# ForgeFlow working instructions\n\n"
        "- Develop ForgeFlow in this workspace.\n"
        "- Never substitute a different provider's credential for Nebius.\n\n"
        "---\n\n"
        "## Read-only project assistant mode\n\n"
        "# ForgeFlow project agent instructions\n\n"
        "You are the ForgeFlow project assistant.\n\n"
        "## What ForgeFlow is\n\n"
        "ForgeFlow is a manufacturing production/delivery exception-handling agent.\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("NEBIUS_API_KEY=not-real\n", encoding="utf-8")
    (root / ".runtime").mkdir()
    (root / ".runtime" / "dsh.env").write_text("NEBIUS_API_KEY=not-real\n", encoding="utf-8")
    (root / "runs").mkdir()
    (root / "runs" / "private.json").write_text("{}\n", encoding="utf-8")

    target = tmp_path / "assistant-workspace"
    monkeypatch.setattr(workspace, "ROOT", root)
    monkeypatch.setattr(workspace, "TARGET", target)
    workspace.sync()

    agents = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert "Develop ForgeFlow in this workspace" not in agents
    assert "Never substitute a different provider's credential" not in agents
    assert "You are the ForgeFlow project assistant." in agents
    assert "production/delivery exception-handling agent" in agents
    assert (target / "src" / "module.py").is_file()
    assert (target / "PROJECT_MEMORY.md").is_file()
    assert (target / workspace.ROOT_MARKER).is_file()
    assert not (target / ".env").exists()
    assert not (target / ".runtime").exists()
    assert not (target / "runs").exists()
    assert not (target / "docs" / ".env").exists()


def test_home_agents_written_idempotently(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(setup, "HOME", tmp_path / "dsh-home")
    setup._write_home_agents()
    path = tmp_path / "dsh-home" / "AGENTS.md"
    first = path.read_text(encoding="utf-8")

    setup._write_home_agents()
    assert path.read_text(encoding="utf-8") == first

    path.write_text("stale instruction\n", encoding="utf-8")
    setup._write_home_agents()
    assert path.read_text(encoding="utf-8") == first

    lowered = first.lower()
    for phrase in (
        "production-exception assistant",
        "read-only",
        "never approve",
        "mcp__forgeflow__",
        "never print credentials",
        "at most one natural next question",
    ):
        assert phrase in lowered
    assert "NEBIUS_API_KEY" not in first
    assert "{{" not in first and "}}" not in first


def test_composition_errors_reject_missing_rows_and_enabled_tools() -> None:
    valid = "\n".join([
        "- id: system-prompt", "  config:", "    personaPrefix: x",
        "- id: agent-instructions", "- id: skill-filesystem", "- id: mcp-forgeflow",
        "- id: session-telemetry-otel", "  config:", "    mode: DISABLED",
        *[line for tool in setup.DISABLED_MODULES for line in (f"- id: {tool}", "  disabled: true")],
    ])
    assert setup._composition_errors(valid) == []
    assert "missing row: mcp-forgeflow" in setup._composition_errors(valid.replace("- id: mcp-forgeflow", ""))
    broken = valid.replace("- id: tool-bash\n  disabled: true", "- id: tool-bash")
    assert "tool not disabled: tool-bash" in setup._composition_errors(broken)
    assert "dump-config returned no composition" in setup._composition_errors("")


@pytest.mark.parametrize('tool', DISABLED_MODULES)
def test_composition_checks_each_exact_tool_registration(tool):
    text = "\n".join([
        "- id: system-prompt", "  config:", "    personaPrefix: x",
        "- id: agent-instructions", "- id: skill-filesystem", "- id: mcp-forgeflow",
        "- id: session-telemetry-otel", "  config:", "    mode: DISABLED",
        *[line for item in DISABLED_MODULES for line in (f"- id: {item}", "  disabled: true")],
    ])
    text = text.replace(f"- id: {tool}\n  disabled: true", f"- id: {tool}\n  disabled: false")
    assert setup._composition_errors(text) == [f"tool not disabled: {tool}"]


def test_composition_rejects_feedback_telemetry_even_without_ui(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, 'PATCH', tmp_path / 'patch.yml')
    setup._write_patch()
    text = setup.PATCH.read_text()
    assert 'session telemetry upload not disabled' not in setup._composition_errors(text)
    assert 'session telemetry upload not disabled' in setup._composition_errors(text.replace('mode: DISABLED', 'mode: FEEDBACK_ONLY'))
