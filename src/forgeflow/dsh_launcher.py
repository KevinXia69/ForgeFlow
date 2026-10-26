"""Fixed low-privilege DSH launch target, invoked only as forgeflow-dsh.

sudo permits the API to enter this user, never root. CLI location, workspace,
home and evidence directory come from a root-owned configuration. No shell,
plugin-management command, arbitrary executable or caller-selected Node flag
is accepted by this entry point.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
import sys
from pathlib import Path

from forgeflow.vps_runtime import trusted_json


def scoped_file(path: Path, root: Path) -> Path:
    if (not path.is_absolute() or path.is_symlink() or path.parent != root
            or path.resolve() != path.absolute()):
        raise ValueError("invalid_assistant_scope")
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_size > 16 * 1024 * 1024
            or info.st_mode & 0o022 or info.st_uid == os.geteuid()):
        raise ValueError("invalid_assistant_scope")
    return path


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
                if patch is not None or Path(option) != evidence.with_suffix(".patch.json"):
                    raise ValueError("invalid_assistant_scope")
                patch = scoped_file(Path(option), Path(config["evidence_root"]))
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
        identity = pwd.getpwnam("forgeflow-dsh")
        if os.geteuid() != identity.pw_uid or os.geteuid() == 0:
            raise ValueError("assistant_identity_required")
        config = trusted_json(Path("/etc/forgeflow/dsh.json"))
        root = Path(config["evidence_root"])
        if root.is_symlink() or root.resolve() != root.absolute():
            raise ValueError("invalid_assistant_scope")
        evidence = scoped_file(Path(os.environ.get("FORGEFLOW_ASSISTANT_EVIDENCE", "")), root)
        arguments = launch_arguments(sys.argv[1:], config, evidence)
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "HOME": config["home"], "DSH_HOME": config["home"], "DSH_PERMISSION_MODE": "read-only",
            "FORGEFLOW_ASSISTANT_EVIDENCE": str(evidence),
            "NEBIUS_API_KEY": os.environ.get("NEBIUS_API_KEY", ""),
        }
        if not environment["NEBIUS_API_KEY"]:
            raise ValueError("assistant_model_key_required")
        os.umask(0o007)
        os.chdir(config["workspace"])
        os.execve(config["node"], arguments, environment)
    except (OSError, ValueError, KeyError):
        print("forgeflow assistant: isolated launch refused", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
