"""Host-controlled composition for an evidence-only assistant process."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import yaml

DISABLED_TOOLS = (
    "tool-bash", "tool-pwsh", "tool-fs", "tool-fs-search", "tool-skill",
    "tool-working-directory", "tool-jobs", "tool-subagent-control",
    "tool-subagent-list-agents", "tool-subagent", "tool-subagent-fork",
    "tool-workflow", "tool-todo", "tool-goal", "plan-mode", "mcp-resources",
    "tool-plugin-manager",
)
DISABLED_UPLOAD_MODULES = ("session-log-deepseek", "command-feedback")
DISABLED_MODULES = (*DISABLED_TOOLS, *DISABLED_UPLOAD_MODULES)
ALLOWED_CONFIG = {
    "agent-default-model", "llm-pi-ai", "system-prompt", "agent-instructions",
    "skill-filesystem", "session-telemetry-otel", "web", "web-search-deepseek",
    "web-fetch-http", "tool-fs", *DISABLED_MODULES,
}


def scoped_image_root(workspace: Path, evidence: Path) -> Path:
    # Evidence filenames are <principal scope>-<permissions revision>. Preserve
    # owner image access when their business evidence changes.
    owner_scope = evidence.stem.split("-", 1)[0]
    owner = hashlib.sha256(owner_scope.encode()).hexdigest()
    return workspace.resolve() / "uploads" / owner


def scoped_patch(source: Path, evidence: Path, *, image_root: Path | None = None,
                 image_tool_module: Path | None = None) -> Path:
    """Fail closed on unreviewed compositions; bind MCP to a host snapshot."""
    try:
        rows = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        raise ValueError("invalid_assistant_composition") from None
    if not isinstance(rows, list):
        raise ValueError("invalid_assistant_composition")
    found = False
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid_assistant_composition")
        if "insert" in row:
            inserted = row["insert"]
            if not isinstance(inserted, list) or len(inserted) != 1:
                raise ValueError("unreviewed_assistant_plugin")
            mcp = inserted[0]
            if not isinstance(mcp, dict):
                raise ValueError("invalid_assistant_composition")
            if mcp.get("id") != "mcp-forgeflow" or mcp.get("name") != "@deepseek-ai/dsh-mcp-client":
                raise ValueError("unreviewed_assistant_plugin")
            config = mcp.get("config", {})
            if not isinstance(config, dict):
                raise ValueError("invalid_assistant_composition")
            if config.get("serverName") != "forgeflow" or config.get("transport") != "stdio":
                raise ValueError("invalid_assistant_transport")
            config["env"] = {"FORGEFLOW_ASSISTANT_EVIDENCE": str(evidence)}
            # Leave room for the knowledge gateway's 90s retrieval budget,
            # while staying within the harness's 180s turn budget.
            config["toolCallTimeoutMs"] = 100_000
            found = True
        elif row.get("id") not in ALLOWED_CONFIG:
            raise ValueError("unreviewed_assistant_plugin")
    if not found:
        raise ValueError("missing_scoped_evidence_tool")
    # Last patch wins: a stale local profile cannot re-enable broad tools.
    rows.extend({"id": name, "disabled": True} for name in DISABLED_MODULES)
    # Keep the service required by SDK consumers but never construct an OTLP
    # exporter. UI removal alone does not disable feedback-triggered uploads.
    rows.append({"id": "session-telemetry-otel", "disabled": False,
                 "config": {"mode": "DISABLED"}})
    if image_root is not None and image_tool_module is not None:
        rows.append({"insert": [{
            "id": "forgeflow-image-guard",
            "name": Path(__file__).with_name("dsh_image_guard.mjs").resolve().as_uri(),
            "config": {"root": str(image_root.resolve()),
                       "toolModule": image_tool_module.resolve().as_uri()},
        }]})
    target = evidence.with_suffix(".patch.json")
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".patch-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
