"""Explicit process environment for the assistant, separate from API secrets."""

from collections.abc import Mapping
from pathlib import Path

MODEL_ENVIRONMENT = frozenset({
    "NEBIUS_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
})
PROCESS_ENVIRONMENT = frozenset({
    "PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC",
})


def runtime_directories(source: Mapping[str, str]) -> tuple[Path, Path]:
    """Initialize mutable state only after an enabled harness is configured."""
    home = Path(source.get("FORGEFLOW_DSH_HOME", ".runtime/dsh-home"))
    workspace = Path(source.get("FORGEFLOW_DSH_WORKSPACE", ".runtime/assistant-workspace"))
    home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    return home, workspace


def assistant_environment(source: Mapping[str, str], *, home: Path,
                          evidence: Path | str = "", model_api_key: str | None = None) -> dict[str, str]:
    """Never inherit API identity, ERP, Docker, loader, or broad storage settings."""
    # An optional user key never replaces the deployment's independent search
    # credential, and never exposes unused deployment model-provider keys.
    if model_api_key is None and source.get("FORGEFLOW_ASSISTANT_API_KEY_ENV") == "FORGEFLOW_CHAT_API_KEY":
        # The installed local profile can use the same fixed key slot. The
        # selector comes from deployment configuration, not a chat request.
        model_api_key = source.get("FORGEFLOW_CHAT_API_KEY", "")
    allowed = (MODEL_ENVIRONMENT if model_api_key is None else {"DEEPSEEK_API_KEY"})
    env = {name: value for name, value in source.items()
           if name in allowed | PROCESS_ENVIRONMENT}
    if model_api_key is not None:
        env["FORGEFLOW_CHAT_API_KEY"] = model_api_key
    # The pinned native loader otherwise copies .node files to /tmp, where
    # the read-only Compose deployment deliberately uses a noexec tmpfs.
    # Load the installed binaries directly, retaining the loader's ABI checks.
    env.update({"HOME": str(home), "DSH_HOME": str(home), "DSH_PERMISSION_MODE": "read-only",
                "NARB_DISABLE_NATIVE_CACHE": "1",
                "FORGEFLOW_ASSISTANT_EVIDENCE": str(evidence)})
    return env


def share_scoped_patch(patch: Path) -> None:
    """The configured setgid evidence directory gives the assistant read access."""
    import os

    if os.environ.get("FORGEFLOW_ASSISTANT_EVIDENCE_ROOT"):
        root = Path(os.environ["FORGEFLOW_ASSISTANT_EVIDENCE_ROOT"])
        if root.is_symlink() or patch.is_symlink() or patch.parent.resolve() != root.resolve():
            raise ValueError("invalid_assistant_composition")
        patch.chmod(0o640)
