#!/usr/bin/env python3
"""Install the pinned local assistant runtime; no model request or ERP write."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "dsh"
PATCH = RUNTIME / "base.json"


def _write_patch() -> None:
    from forgeflow.vps_runtime import dsh_patch

    PATCH.parent.mkdir(parents=True, exist_ok=True)
    value = dsh_patch({"python": sys.executable, "repo_root": str(ROOT)},
                      os.environ.get("NEBIUS_MODEL", "nvidia/Nemotron-3.5-Super-VL"),
                      os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1"))
    PATCH.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configure-only", action="store_true", help="Reuse an installed runtime")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("Use Linux or WSL2; the workbench and assistant launcher require Unix APIs.")
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node or not npm:
        parser.error("Install Node.js 24 and npm first.")
    version = subprocess.check_output([node, "--version"], text=True).strip()
    if int(version.lstrip("v").split(".")[0]) < 24:
        parser.error("The pinned assistant runtime requires Node.js 24 or newer.")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if not args.configure_only:
        for filename in ("package.json", "package-lock.json"):
            shutil.copyfile(ROOT / "deploy/vps/dsh" / filename, RUNTIME / filename)
        subprocess.run([npm, "ci", "--omit=dev", "--no-audit", "--no-fund"],
                       cwd=RUNTIME, check=True)
    cli = RUNTIME / "node_modules/@deepseek-ai/dsh/lib/bin.js"
    if not cli.is_file():
        parser.error("Pinned DSH CLI is missing; run setup without --configure-only.")
    _write_patch()
    for name in ("home", "workspace"):
        (RUNTIME / name).mkdir(exist_ok=True)
    values = {
        "FORGEFLOW_DSH_ENABLED": "1", "FORGEFLOW_DSH_SDK": "1",
        "FORGEFLOW_DSH_CLI": str(cli), "FORGEFLOW_DSH_PATCH": str(PATCH),
        "FORGEFLOW_DSH_NODE": node, "FORGEFLOW_DSH_HOME": str(RUNTIME / "home"),
        "FORGEFLOW_DSH_WORKSPACE": str(RUNTIME / "workspace"),
        "FORGEFLOW_DSH_MODEL": os.environ.get("NEBIUS_MODEL", "nvidia/Nemotron-3.5-Super-VL"),
    }
    (ROOT / ".runtime/dsh.env").write_text(
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items()), encoding="utf-8")
    print("Local DSH runtime configured. Set NEBIUS_API_KEY in .env or the process environment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
