#!/usr/bin/env python3
"""Build a read-only project workspace for the DSH assistant.

Only source, docs, tests and contracts are copied. Secrets, runtime state,
recorded private runs, upstream vendor snapshots and dependencies are excluded.
The repository ``AGENTS.md`` is layered: the read-only assistant section is
copied, while the engineering-workflow prefix (which tells an agent to develop
ForgeFlow) is not.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / ".runtime" / "assistant-workspace"
FILES = ("PROJECT_MEMORY.md", "README.md", "pyproject.toml", "LICENSE", "NOTICE")
DIRECTORIES = ("src", "tests", "contracts", "docs", "scripts", "web/src", "web-public/src")
ASSISTANT_MODE_MARKER = "## Read-only project assistant mode"
# Matches AGENT_INSTRUCTIONS_MARKER in setup_dsh_harness.py.  The patch tells
# DSH to stop project-instruction discovery at this file, so the full
# engineering-workflow AGENTS.md in the parent repository is not loaded.
ROOT_MARKER = ".dsh-assistant-root"
IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.log", "node_modules", "dist", ".git",
    "*.pdf", "*.png", "*.jpg", "*.jpeg", "*.webp", "*.avif", "*.mp4",
    ".env", ".env.*", "*.pem", "*.key",
)


def trim_agents(text: str) -> str:
    """Return only the read-only assistant part of the repository AGENTS.md."""
    marker = text.find(ASSISTANT_MODE_MARKER)
    if marker < 0:
        raise ValueError(f"AGENTS.md is missing the {ASSISTANT_MODE_MARKER!r} section")
    return text[marker + len(ASSISTANT_MODE_MARKER):].lstrip("\n")


def _copy_agents() -> int:
    source = ROOT / "AGENTS.md"
    if not source.is_file():
        return 0
    (TARGET / "AGENTS.md").write_text(
        trim_agents(source.read_text(encoding="utf-8")), encoding="utf-8"
    )
    return 1


def sync() -> None:
    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)
    (TARGET / ROOT_MARKER).write_text("", encoding="utf-8")
    count = 0
    for name in FILES:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, TARGET / name)
            count += 1
    count += _copy_agents()
    for relative in DIRECTORIES:
        source = ROOT / relative
        if not source.is_dir():
            continue
        destination = TARGET / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, ignore=IGNORE)
        count += sum(1 for path in destination.rglob("*") if path.is_file())
    print(f"assistant workspace: {TARGET} ({count} files)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync", action="store_true", help="rebuild the workspace")
    parser.parse_args()
    sync()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
