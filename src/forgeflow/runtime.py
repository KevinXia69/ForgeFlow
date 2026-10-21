"""Read workspace-local Docker connection settings without changing global state."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

from .config import ConfigError

DOCKER_CONFIG_NAMES = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")


def docker_environment(
    workspace_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a process-environment copy with explicit local Docker defaults.

    Only the three Docker connection/configuration keys are imported from the
    selected workspace's ``.env``. Existing process values, including empty
    strings, win. Dotenv interpolation and parent-directory discovery are
    disabled. This does not select a Docker context or modify ``os.environ``.

    The returned environment retains the caller's original process variables;
    do not log it wholesale. Callers with stricter child-process policies should
    apply their own allowlist after calling this function.
    """
    environment = dict(os.environ if environ is None else environ)
    env_path = Path(workspace_root).resolve() / ".env"
    try:
        file_values = (
            dotenv_values(env_path, interpolate=False) if env_path.is_file() else {}
        )
    except (OSError, UnicodeError):
        raise ConfigError("The workspace Docker configuration could not be read.") from None
    for name in DOCKER_CONFIG_NAMES:
        value = file_values.get(name)
        if name not in environment and value is not None:
            environment[name] = value
    return environment
