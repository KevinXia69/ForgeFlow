#!/usr/bin/env python3
"""Prepare the optional local DSH headless harness.

This never downloads packages and never writes a model key. It reuses an
installed DSH runtime (set FORGEFLOW_DSH_CLI, or use the local .runtime copy),
creates an isolated DSH home/profile, writes the Nebius OpenAI-compatible route,
and rebuilds the read-only assistant workspace.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".runtime"
CLI_CANDIDATES = (
    RUNTIME / "dsh-runtime" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js",
    Path("/tmp/dsh-asar/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js"),
)
PATCH = RUNTIME / "dsh-nebius.yml"
HOME = RUNTIME / "dsh-home"
WORKSPACE = RUNTIME / "assistant-workspace"
ENV_FILE = RUNTIME / "dsh.env"

# The assistant workspace is its own instruction root.  DSH otherwise walks up
# to the repository's .git and loads the engineering-workflow AGENTS.md too.
AGENT_INSTRUCTIONS_MARKER = ".dsh-assistant-root"

# The persona replaces the coding-agent opener from the DSH headless/web
# bundles.  It is plain deployment text: no {{variables}}, so prompt assembly
# cannot fail on an unavailable fact.
PERSONA_PREFIX = """\
You are ForgeFlow's production-exception assistant: a calm, plain-spoken
colleague who helps a manufacturing operations team understand what happened to
their orders and where recovery stands. You are not a coding agent.

Assume the reader knows nothing about this case or ForgeFlow. In the first
sentence, say what the case is and what happened; then say what it means and
what the reader needs to do. Answer in the user's language. Use two or three
short paragraphs, one idea each, and at most two decision-relevant numbers.
Explain a technical term only when it changes the answer. Never open with an ID, hash, field name, heading tree,
table or checklist. Do not use stock AI phrases such as "firstly", "moreover",
"in conclusion", "it is worth noting" or "as an AI".
"""

PERSONA_SUFFIX = """\
This chat is read-only. You cannot approve, apply, execute or promise an ERP
change, and you must not ask the user to approve anything in chat. When
approval is the next step, point to the Cases page.

Treat evidence as binding. Cite only records you actually observed through the
provided case and run tools or files in this workspace. Clearly separate four
states in plain words: planned, applied, verified, and unknown. Never imply
that an approval, ERP write, readback or verification happened unless the case
record shows it. If something is missing, say it is not verified and name what
would verify it. Put evidence behind "view evidence", not in the main answer.
End with at most one natural next question.
"""

HOME_AGENTS = """\
# ForgeFlow home instructions

You are the ForgeFlow production-exception assistant for a manufacturing
operations team. Answer in the user's language, in plain words, and keep the
reply short enough to read once.

- Assume the reader knows nothing about the case. First say what the case is
  and what happened, then what it means and what to do next. Use two or three
  short paragraphs; add IDs, tables or code paths only when the user asks.
- Clearly separate planned, applied, verified and unknown. Never use an ID,
  hash or internal field name as the opening.
- Chat is read-only. Never approve, apply or execute anything from chat, and
  never ask for approval here. Point to the Cases page when approval is next.
- Never say an approval, ERP write, readback or verification happened unless
  the case record shows it. Cite only evidence you actually observed.
- Use the `mcp__forgeflow__*` case and run tools for case evidence. Do not read
  `.env`, `.runtime`, `runs` or `vendor`, and never print credentials.
- If evidence is missing, say it is not verified and name what would verify it.
- End with at most one natural next question.
"""


def _find_cli() -> Path | None:
    configured = os.environ.get("FORGEFLOW_DSH_CLI", "").strip()
    if configured and Path(configured).is_file():
        return Path(configured)
    for candidate in CLI_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _yaml_block(text: str, indent: int) -> str:
    """Render text as a YAML literal block scalar body at ``indent`` spaces."""
    pad = " " * indent
    lines = text.rstrip("\n").splitlines()
    return "".join((f"{pad}{line}" if line else "") + "\n" for line in lines)


def _write_patch() -> None:
    dotenv: dict[str, str] = {}
    base_url = os.environ.get("NEBIUS_BASE_URL") or dotenv.get("NEBIUS_BASE_URL") or "https://api.tokenfactory.nebius.com/v1"
    model = os.environ.get("NEBIUS_MODEL") or dotenv.get("NEBIUS_MODEL") or "nvidia/Nemotron-3.5-Super-VL"
    PATCH.parent.mkdir(parents=True, exist_ok=True)
    PATCH.write_text(
        "- id: agent-default-model\n"
        "  config:\n"
        "    provider: nebius\n"
        f"    model: {model}\n"
        "- id: llm-pi-ai\n"
        "  config:\n"
        "    providers:\n"
        "      nebius:\n"
        "        displayName: Nebius Token Factory\n"
        "        api: openai-completions\n"
        f"        baseURL: {base_url}\n"
        "        apiKeyEnv: NEBIUS_API_KEY\n"
        "        models:\n"
        f"          - id: {model}\n"
        "            name: Nemotron 3.5 Super VL\n"
        "            contextWindow: 262144\n"
        "            defaultMaxTokens: 8192\n"
        # Replace the bundle's coding-agent persona with the read-only
        # ForgeFlow production-exception voice.
        "- id: system-prompt\n"
        "  config:\n"
        "    personaPrefix: |\n"
        f"{_yaml_block(PERSONA_PREFIX, 6)}"
        "    personaSuffix: |\n"
        f"{_yaml_block(PERSONA_SUFFIX, 6)}"
        # Stop project-instruction discovery at the assistant workspace so the
        # read-only copy of AGENTS.md is the only project instruction file.
        "- id: agent-instructions\n"
        "  disabled: false\n"
        "  config:\n"
        "    maxBytes: 65536\n"
        "    projectRootMarkers:\n"
        "      - .git\n"
        f"      - {AGENT_INSTRUCTIONS_MARKER}\n"
        "- id: skill-filesystem\n"
        "  disabled: false\n"
        # ForgeFlow's assistant is evidence-scoped.  DSH read-only blocks
        # writes but not reads of .env/.runtime by the same uid, so disable
        # all observed native tool registrars, including delegation/jobs and
        # plan/resource tools, while retaining their non-tool host services.
        + "".join(f"- id: {tool}\n  disabled: true\n" for tool in DISABLED_MODULES)
        + "- id: session-telemetry-otel\n  disabled: false\n  config:\n    mode: DISABLED\n"
        +
        "- insert:\n"
        "    - id: mcp-forgeflow\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: forgeflow\n"
        "        transport: stdio\n"
        f"        command: {json.dumps(sys.executable)}\n"
        f"        args: [{json.dumps(str(ROOT / 'scripts' / 'assistant_mcp.py'))}]\n"
        f"        cwd: {json.dumps(str(ROOT))}\n"
        "        env: {}\n",
        encoding="utf-8",
    )


def _write_home_agents() -> None:
    """Install the $DSH_HOME-level role/style/read-only instruction.

    Always restores the same canonical text, so repeated setup runs are
    idempotent and stale local edits cannot widen the assistant's role.
    """
    path = HOME / "AGENTS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text(encoding="utf-8") == HOME_AGENTS:
        return
    path.write_text(HOME_AGENTS, encoding="utf-8")


REQUIRED_ROWS = ("agent-instructions", "mcp-forgeflow", "skill-filesystem", "system-prompt")
DISABLED_TOOLS = (
    "tool-bash", "tool-pwsh", "tool-fs", "tool-fs-search", "tool-web", "tool-skill",
    "tool-working-directory", "tool-jobs", "tool-subagent-control",
    "tool-subagent-list-agents", "tool-subagent", "tool-subagent-fork",
    "tool-workflow", "tool-todo", "tool-goal", "plan-mode", "mcp-resources",
    "tool-plugin-manager",
)
DISABLED_UPLOAD_MODULES = ("session-log-deepseek", "command-feedback")
DISABLED_MODULES = (*DISABLED_TOOLS, *DISABLED_UPLOAD_MODULES)


def _module_available(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


def _composition_errors(config_text: str) -> list[str]:
    """Validate the resolved DSH graph instead of trusting a zero exit."""
    errors: list[str] = []
    if not isinstance(config_text, str) or not config_text.strip():
        return ["dump-config returned no composition"]
    for row in REQUIRED_ROWS:
        if f"- id: {row}" not in config_text:
            errors.append(f"missing row: {row}")
    for tool in DISABLED_MODULES:
        start = config_text.find(f"- id: {tool}\n")
        if start < 0:
            errors.append(f"missing disabled tool row: {tool}")
            continue
        next_row = config_text.find("\n- id:", start + 1)
        block = config_text[start: next_row if next_row >= 0 else None]
        if "disabled: true" not in block:
            errors.append(f"tool not disabled: {tool}")
    start = config_text.find("- id: session-telemetry-otel\n")
    end = config_text.find("\n- id:", start + 1) if start >= 0 else -1
    block = config_text[start:end if end >= 0 else None] if start >= 0 else ""
    if "mode: DISABLED" not in block:
        errors.append("session telemetry upload not disabled")
    if "personaPrefix" not in config_text:
        errors.append("missing ForgeFlow persona")
    return errors


def main() -> int:
    cli = _find_cli()
    if cli is None:
        print("DSH runtime not found. Set FORGEFLOW_DSH_CLI to @deepseek-ai/dsh/lib/bin.js.", file=sys.stderr)
        return 2
    node = os.environ.get("FORGEFLOW_DSH_NODE") or shutil.which("node") or "node"
    HOME.mkdir(parents=True, exist_ok=True)
    _write_patch()
    probe = subprocess.run(
        [node, str(cli), "--profile", "headless", "--patch", str(PATCH), "--dump-config"],
        env={**os.environ, "DSH_HOME": str(HOME)}, capture_output=True, text=True,
    )
    if probe.returncode != 0:
        print(probe.stderr[-500:] or "DSH profile bootstrap failed", file=sys.stderr)
        return 2
    errors = _composition_errors(probe.stdout)
    if errors:
        print("DSH composition rejected: " + "; ".join(errors), file=sys.stderr)
        return 2
    version_probe = subprocess.run(
        [node, str(cli), "--version"], env={**os.environ, "DSH_HOME": str(HOME)},
        capture_output=True, text=True,
    )
    version = version_probe.stdout.strip() if version_probe.returncode == 0 else "unknown"
    _write_home_agents()
    brand = subprocess.run(
        [sys.executable, str(ROOT / "scripts/brand_dsh_web.py")],
        cwd=ROOT, env={**os.environ, "FORGEFLOW_DSH_CLI": str(cli)},
    )
    if brand.returncode != 0:
        return brand.returncode
    sync = subprocess.run(
        [sys.executable, str(ROOT / "scripts/assistant_workspace.py"), "--sync"],
        cwd=ROOT, text=True,
    )
    if sync.returncode != 0:
        return sync.returncode
    # Skills live in the user-DSH root, not the disposable workspace copy, so
    # they survive a workspace rebuild and are discovered at rank 400.
    skills = subprocess.run(
        [sys.executable, str(ROOT / "scripts/sync_assistant_skills.py"),
         "--target", str(HOME / "skills")],
        cwd=ROOT, text=True,
    )
    if skills.returncode != 0:
        return skills.returncode
    values = {
        "FORGEFLOW_DSH_ENABLED": "1",
        "FORGEFLOW_DSH_NODE": node,
        "FORGEFLOW_DSH_CLI": str(cli),
        "FORGEFLOW_DSH_PATCH": str(PATCH),
        "FORGEFLOW_DSH_HOME": str(HOME),
        "FORGEFLOW_DSH_WORKSPACE": str(WORKSPACE),
        "FORGEFLOW_DSH_VERSION": version,
        # The SDK runtime is optional on review images; the API falls back to
        # the one-shot CLI when the package is absent.
        "FORGEFLOW_DSH_SDK": "1" if _module_available("deepseek_harness") else "0",
    }
    ENV_FILE.write_text(
        "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    print(f"DSH harness prepared: {cli}")
    print(f"environment: {ENV_FILE}")
    print("Start the API with NEBIUS_API_KEY in its environment; the key is never written here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
