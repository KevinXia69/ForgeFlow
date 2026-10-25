"""Explicit process environment for the assistant, separate from API secrets."""

from collections.abc import Mapping
from pathlib import Path

MODEL_ENVIRONMENT = frozenset({
    "NEBIUS_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
})
PROCESS_ENVIRONMENT = frozenset({
    "PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC",
})


def assistant_environment(source: Mapping[str, str], *, home: Path,
                          evidence: Path | str = "") -> dict[str, str]:
    """Never inherit API identity, ERP, Docker, loader, or broad storage settings."""
    env = {name: value for name, value in source.items()
           if name in MODEL_ENVIRONMENT | PROCESS_ENVIRONMENT}
    env.update({"HOME": str(home), "DSH_HOME": str(home), "DSH_PERMISSION_MODE": "read-only",
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
