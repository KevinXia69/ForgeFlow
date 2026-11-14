"""Fixed low-privilege DSH launch target for the configured assistant identity.

sudo permits the API to enter this user, never root. CLI location, workspace,
home and evidence directory come from a root-owned configuration. No shell,
plugin-management command, arbitrary executable or caller-selected Node flag
is accepted by this entry point.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import stat
import sys
from pathlib import Path

from forgeflow.vps_runtime import API_USER, DSH_USER, assistant_config_path, trusted_json


def scoped_file(path: Path, root: Path, *, owner_uid: int | None = None) -> Path:
    if (not path.is_absolute() or path.is_symlink() or path.parent != root
            or path.resolve() != path.absolute()):
        raise ValueError("invalid_assistant_scope")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024
            or info.st_mode & 0o022 or info.st_uid == os.geteuid()
            or (owner_uid is not None and info.st_uid != owner_uid)):
        raise ValueError("invalid_assistant_scope")
    return path


_ALLOWED_KEY_ENVS = {"NEBIUS_API_KEY", "DEEPSEEK_API_KEY"}


def assistant_key_env(patch_path: Path, config: dict | None = None) -> str:
    """Read the selected provider key name from trusted config or the DSH patch."""
    configured = (config or {}).get("api_key_env")
    try:
        patches = json.loads(patch_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return configured if configured in _ALLOWED_KEY_ENVS else "DEEPSEEK_API_KEY"
    if not isinstance(patches, list):
        return configured if configured in _ALLOWED_KEY_ENVS else "DEEPSEEK_API_KEY"
    selected = None
    selected_key = None
    for patch in patches:
        if isinstance(patch, dict) and patch.get("id") == "agent-default-model":
            values = patch.get("config")
            if isinstance(values, dict) and "provider" in values:
                selected = values["provider"]
    for patch in patches:
        if not isinstance(patch, dict) or patch.get("id") != "llm-pi-ai":
            continue
        values = patch.get("config")
        providers = values.get("providers") if isinstance(values, dict) else None
        provider = providers.get(selected) if isinstance(providers, dict) and isinstance(selected, str) else None
        if isinstance(provider, dict) and "apiKeyEnv" in provider:
            selected_key = provider["apiKeyEnv"]
    # Only an effective, owner-bound BYOK route overrides the deployment key.
    if selected_key == "FORGEFLOW_CHAT_API_KEY":
        return selected_key
    if configured in _ALLOWED_KEY_ENVS:
        return configured
    if selected_key in _ALLOWED_KEY_ENVS:
        return selected_key
    for patch in patches:
        if not isinstance(patch, dict) or patch.get("id") != "llm-pi-ai":
            continue
        providers = (patch.get("config") or {}).get("providers")
        if not isinstance(providers, dict):
            continue
        for provider in providers.values():
            if not isinstance(provider, dict):
                continue
            key_env = provider.get("apiKeyEnv")
            if key_env in _ALLOWED_KEY_ENVS:
                return key_env
    return "DEEPSEEK_API_KEY"


def _uses_deepseek_search(patch_path: Path) -> bool:
    try:
        rows = json.loads(patch_path.read_text(encoding="utf-8"))
        enabled = False
        for row in rows:
            if isinstance(row, dict) and row.get("id") == "web-search-deepseek":
                enabled = not row.get("disabled", False)
        return enabled
    except (OSError, ValueError, TypeError):
        return False


def assistant_launch_environment(config: dict, evidence: Path, patch_path: Path,
                                 source: dict[str, str] | None = None) -> dict[str, str]:
    """Pass only the credential selected by the patch into the model process."""
    source = os.environ if source is None else source
    key_env = assistant_key_env(patch_path, config)
    api_key = source.get(key_env, "")
    if not api_key:
        raise ValueError("assistant_model_key_required")
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
        "HOME": config["home"], "DSH_HOME": config["home"],
        "DSH_PERMISSION_MODE": "read-only",
        # Use installed native binaries; never dlopen a copy from noexec /tmp.
        "NARB_DISABLE_NATIVE_CACHE": "1",
        "FORGEFLOW_ASSISTANT_EVIDENCE": str(evidence),
        key_env: api_key,
    }
    if _uses_deepseek_search(patch_path) and source.get("DEEPSEEK_API_KEY"):
        environment["DEEPSEEK_API_KEY"] = source["DEEPSEEK_API_KEY"]
    return environment


def launch_arguments(argv: list[str], config: dict, evidence: Path) -> list[str]:
    if not argv or argv[0] != config["cli"]:
        raise ValueError("invalid_assistant_command")
    arguments = argv[1:]
    profile = patch = None
    cursor = 0
    while cursor < len(arguments):
        value = arguments[cursor]
        if value == "--":
            if profile != "headless" or len(arguments[cursor + 1:]) != 1:
                raise ValueError("invalid_assistant_command")
            break
        if value in {"--profile", "--patch", "--session-id"}:
            if cursor + 1 >= len(arguments):
                raise ValueError("invalid_assistant_command")
            option = arguments[cursor + 1]
            if value == "--profile":
                if profile is not None or option not in {"sdk", "headless"}:
                    raise ValueError("invalid_assistant_command")
                profile = option
            elif value == "--patch":
                candidate = Path(option)
                default = candidate == evidence.with_suffix(".patch.json")
                byok = (candidate.parent == evidence.parent and re.fullmatch(
                    re.escape(evidence.stem) + r"\.byok-[0-9a-f]{32}\.patch\.json", candidate.name))
                if patch is not None or not (default or byok):
                    raise ValueError("invalid_assistant_scope")
                patch = scoped_file(Path(option), Path(config["evidence_root"]),
                                    owner_uid=config.get("api_uid"))
            elif re.fullmatch(r"[A-Za-z0-9._-]{1,200}", option) is None:
                raise ValueError("invalid_assistant_command")
            cursor += 2
        elif value == "--json":
            cursor += 1
        else:
            raise ValueError("invalid_assistant_command")
    if profile is None or patch is None:
        raise ValueError("invalid_assistant_command")
    return [config["node"], config["cli"], *arguments]


def main() -> int:
    try:
        identity = pwd.getpwuid(os.geteuid())
        if os.geteuid() == 0:
            raise ValueError("assistant_identity_required")
        config = trusted_json(assistant_config_path(identity.pw_name))
        if config.get("dsh_user", DSH_USER) != identity.pw_name:
            raise ValueError("assistant_identity_required")
        api = pwd.getpwnam(config.get("api_user", API_USER))
        if api.pw_uid in (0, identity.pw_uid):
            raise ValueError("assistant_identity_required")
        config["api_uid"] = api.pw_uid
        root = Path(config["evidence_root"])
        if root.is_symlink() or root.resolve() != root.absolute():
            raise ValueError("invalid_assistant_scope")
        evidence = scoped_file(Path(os.environ.get("FORGEFLOW_ASSISTANT_EVIDENCE", "")), root,
                               owner_uid=api.pw_uid)
        arguments = launch_arguments(sys.argv[1:], config, evidence)
        environment = assistant_launch_environment(
            config, evidence, Path(arguments[arguments.index("--patch") + 1]))
        os.umask(0o007)
        os.chdir(config["workspace"])
        os.execve(config["node"], arguments, environment)
    except (OSError, ValueError, KeyError):
        print("forgeflow assistant: isolated launch refused", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
