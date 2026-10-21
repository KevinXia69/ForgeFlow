"""Read-only local readiness checks; these do not contact a model provider."""

from __future__ import annotations

import json
import math
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from .config import ConfigError, load_config
from .runtime import docker_environment

REQUIRED_PYTHON = (3, 12)
TOOLS = ("docker", "colima", "node", "npm", "uv", "harbor")
DOCKER_INFO_FORMAT = (
    '{"server_version":{{json .ServerVersion}},'
    '"architecture":{{json .Architecture}},'
    '"operating_system":{{json .OperatingSystem}},'
    '"cpus":{{json .NCPU}},'
    '"memory_bytes":{{json .MemTotal}}}'
)


def _docker_info(
    docker_path: str | None,
    timeout_seconds: float,
    environment: Mapping[str, str],
) -> dict[str, object]:
    if docker_path is None:
        return {"status": "missing", "available": False}
    try:
        result = subprocess.run(
            [docker_path, "info", "--format", DOCKER_INFO_FORMAT],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "available": False,
            "message": "Docker info exceeded the diagnostic timeout.",
        }
    except OSError:
        return {
            "status": "unavailable",
            "available": False,
            "message": "Docker info could not be executed.",
        }
    if result.returncode:
        # Docker stderr can include connection URLs and host configuration.
        # Keep it out of public diagnostics rather than attempting redaction.
        return {
            "status": "unavailable",
            "available": False,
            "returncode": result.returncode,
            "message": "Docker info failed; the daemon may not be running.",
        }
    try:
        info = json.loads(result.stdout)
        if not isinstance(info, dict):
            raise ValueError
        safe_info = {
            name: info.get(name)
            for name in (
                "server_version",
                "architecture",
                "operating_system",
                "cpus",
                "memory_bytes",
            )
        }
        if not all(
            isinstance(safe_info[name], str)
            for name in ("server_version", "architecture", "operating_system")
        ) or not all(
            isinstance(safe_info[name], int)
            and not isinstance(safe_info[name], bool)
            and safe_info[name] > 0
            for name in ("cpus", "memory_bytes")
        ):
            raise ValueError
    except (ValueError, TypeError):
        return {
            "status": "invalid_response",
            "available": False,
            "message": "Docker info did not return the expected diagnostic fields.",
        }
    return {"status": "available", "available": True, **safe_info}


def run_doctor(
    workspace_root: str | Path,
    *,
    timeout_seconds: float = 5.0,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return local configuration and runtime facts without changing state.

    Docker receives one bounded ``info`` request. No provider API, installation,
    container startup, context switch, or configuration write is performed.
    A valid credential's presence is configuration evidence, not proof that
    authentication, the selected model, or an end-to-end task works.
    """
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
        raise ValueError("Diagnostic timeout must be greater than 0 and at most 30 seconds.")
    root = Path(workspace_root).resolve()
    try:
        config = load_config(root, environ=environ)
        config_status = {
            "status": "configured" if not config.missing_for_model_run else "incomplete",
            **config.public_dict(),
            "provider_call_performed": False,
        }
    except ConfigError as error:
        config_status = {
            "status": "invalid",
            "key_present": None,
            "message": str(error),
            "provider_call_performed": False,
        }
    tools = {
        name: {"available": (path := shutil.which(name)) is not None, "path": path}
        for name in TOOLS
    }
    try:
        docker_env = docker_environment(root, environ=environ)
        docker = _docker_info(tools["docker"]["path"], timeout_seconds, docker_env)
    except ConfigError as error:
        docker = {
            "status": "configuration_error",
            "available": False,
            "message": str(error),
        }
    return {
        "workspace_root": str(root),
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "supported": sys.version_info[:2] >= REQUIRED_PYTHON,
            "minimum": ".".join(map(str, REQUIRED_PYTHON)),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "architecture": platform.machine(),
        },
        "config": config_status,
        "tools": tools,
        "docker": docker,
        "benchmark_run_performed": False,
    }
