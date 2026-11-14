"""Host-controlled composition for an evidence-only assistant process."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
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
                 image_tool_module: Path | None = None,
                 reasoning_effort: str | None = None, model_settings=None) -> Path:
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
    if model_settings is not None:
        provider, profile = _model_profile(model_settings)
        # Do not merge the caller's backend with deployment-specific defaults
        # (e.g. a native Google catalog model must not inherit a REST api field).
        for row in rows:
            if row.get("id") == "agent-default-model":
                row["config"] = {"provider": provider, "model": model_settings.model}
            elif row.get("id") == "llm-pi-ai":
                row.setdefault("config", {})["providers"] = {}
        rows.extend([
            {"id": "agent-default-model", "config": {"provider": provider, "model": model_settings.model}},
            {"id": "llm-pi-ai", "config": {"providers": {provider: profile}}},
        ])
    if reasoning_effort is not None:
        # CLI and SDK both consume this owner-bound composition. Preserve an
        # explicit effort without adding capabilities or clamping it: the
        # installed adapter rejects unknown/unsupported levels. None leaves
        # the shared deployment profile's default untouched.
        provider = None
        profiles = []
        for row in rows:
            if row.get("id") == "agent-default-model":
                config = row.get("config", {})
                if not isinstance(config, dict):
                    raise ValueError("invalid_assistant_composition")
                if "provider" in config:
                    provider = config["provider"]
        for row in rows:
            if row.get("id") == "llm-pi-ai":
                config = row.get("config", {})
                if not isinstance(config, dict) or not isinstance(config.get("providers", {}), dict):
                    raise ValueError("invalid_assistant_composition")
                profile = config.get("providers", {}).get(provider) if isinstance(provider, str) else None
                if isinstance(profile, dict):
                    profiles.append(profile)
        if not profiles:
            raise ValueError("missing_assistant_reasoning_provider")
        profiles[-1]["reasoning"] = reasoning_effort
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
    target = (evidence.with_name(f"{evidence.stem}.byok-{uuid.uuid4().hex}.patch.json")
              if model_settings is not None else evidence.with_suffix(".patch.json"))
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".patch-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def _model_profile(settings) -> tuple[str, dict]:
    """Render only non-secret connection metadata from validated settings."""
    from .model_settings import ModelSettings

    if not isinstance(settings, ModelSettings):
        raise ValueError("invalid_assistant_model_settings")
    provider = getattr(settings, "provider", "openai")
    api = getattr(settings, "api", "openai-completions")
    if api not in {"openai-completions", "openai-responses", "anthropic-messages", "google-generative-ai"}:
        raise ValueError("invalid_assistant_model_api")
    if api == "google-generative-ai" and provider != "google":
        raise ValueError("invalid_assistant_google_provider")
    if not isinstance(provider, str) or not provider or not settings.api_key:
        raise ValueError("invalid_assistant_model_settings")
    profile = {"apiKeyEnv": "FORGEFLOW_CHAT_API_KEY", "baseURL": settings.base_url,
               "timeoutMs": 150000}
    if api != "google-generative-ai":
        profile.update(api=api, models=[{"id": settings.model,
            "input": ["text", "image"] if getattr(settings, "image_input", True) else ["text"],
            "maxTokens": 8192}])
    if provider == "deepseek" and settings.model in {"deepseek-flash", "deepseek-v4-pro"}:
        profile["reasoning"] = "max"
    return provider, profile


def bind_model_harness(harness, settings):
    """Clone an already-authorized scope; keys exist only in this object's memory."""
    from .dsh_environment import share_scoped_patch
    from .dsh_harness import DshUnavailable

    if not hasattr(harness, "evidence_path") or not hasattr(harness, "_composition_source"):
        raise DshUnavailable("dsh_model_requires_evidence_scope")
    patch = None
    try:
        provider, _ = _model_profile(settings)
        patch = scoped_patch(harness._composition_source, harness.evidence_path,
            image_root=harness.image_root,
            image_tool_module=harness.cli.parent.parent.parent / "dsh-tool-fs/lib/index.js",
            model_settings=settings)
        share_scoped_patch(patch)
        options = {"queue_timeout": harness.queue_timeout} if hasattr(harness, "queue_timeout") else {}
        scoped = type(harness)(cli=harness.cli, patch=patch, home=harness.home,
            workspace=harness.workspace, node=harness.node, model=settings.model,
            timeout=harness.timeout, sessions_file=patch.with_suffix(".sessions.json"),
            knowledge_root=harness.knowledge_root, reasoning_effort=None, **options)
        scoped.provider = provider
        scoped.evidence_path, scoped.image_root = harness.evidence_path, harness.image_root
        if hasattr(harness, "evidence_scope_path"):
            scoped.evidence_scope_path = harness.evidence_scope_path
        scoped._composition_source = harness._composition_source
        scoped._session_namespace = uuid.uuid4().hex
        scoped._model_api_key = settings.api_key
        scoped._owned_model_files = (patch, scoped.sessions_file, scoped.sessions_file.with_suffix(".tmp"))
        return scoped
    except (OSError, ValueError):
        if patch is not None:
            patch.unlink(missing_ok=True)
        raise DshUnavailable("dsh_model_binding_unavailable") from None


def close_model_harness(harness) -> None:
    if hasattr(harness, "_owned_model_files"):
        harness._model_api_key = None
        harness._model_closed = True
        if hasattr(harness, "_configured"):
            harness._configured.clear()
        if hasattr(harness, "_sessions"):
            harness._sessions.clear()
        for path in harness._owned_model_files:
            path.unlink(missing_ok=True)
