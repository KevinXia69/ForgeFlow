"""Project pinned ERP-Bench instructions onto the permission-split profile.

P1 gives the evaluated agent a read-only Odoo credential at
``/etc/odoo/api_key.read`` while the write key stays with the controlled
executor. The pinned task instructions still point at the write key and show
direct-write examples, so this module derives the replacement instruction and
records enough provenance (hashes and a unified diff) to keep the derived task
auditable and separate from historical scores.

The projection is deliberately exact: every passage must occur exactly once, so
a vendor instruction edit fails loudly instead of drifting silently.
"""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

READ_KEY_PATH = "/etc/odoo/api_key.read"
WRITE_KEY_PATH = "/etc/odoo/api_key"

PROFILE = "permission-split-v1"
MAX_INSTRUCTION_BYTES = 256 * 1024
_DIFF_FROM = "original/instruction.md"
_DIFF_TO = f"{PROFILE}/instruction.md"

_KEY_BULLET = f"- **API Key:** stored in `{WRITE_KEY_PATH}`"
_ACCESS_INTRO = (
    "You can write and execute Python scripts using the `odoo-client-lib` library "
    "to interact with Odoo via the JSON-2 API:"
)
_PASSWORD_OPEN = f"database='bench', password=open('{WRITE_KEY_PATH}').read().strip()"
_READ_BULLET = (
    f"- **Read-only API Key:** stored in `{READ_KEY_PATH}`\n"
    "- **Write access:** this credential is read-only; direct Odoo writes are rejected. "
    "Apply ERP changes only through `execute_recovery` after `prepare_recovery`."
)
_READ_INTRO = (
    "You can execute read-only Python scripts using the `odoo-client-lib` library "
    "to read Odoo via the JSON-2 API:"
)
_READ_PASSWORD_OPEN = f"database='bench', password=open('{READ_KEY_PATH}').read().strip()"

_REPLACEMENTS = (
    (_KEY_BULLET, _READ_BULLET),
    (_ACCESS_INTRO, _READ_INTRO),
    (_PASSWORD_OPEN, _READ_PASSWORD_OPEN),
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def profile_instruction(text: str) -> tuple[str, dict[str, object]]:
    """Return the permission-split instruction and its provenance record.

    Raises ``ValueError("already_projected")`` when the read-only key path is
    already present, and a ``ValueError`` naming the passage index when a
    passage is missing or ambiguous.
    """
    if READ_KEY_PATH in text:
        raise ValueError("already_projected")

    offsets: list[int] = []
    for index, (passage, _) in enumerate(_REPLACEMENTS):
        found = text.count(passage)
        if found != 1:
            raise ValueError(f"passage {index}: expected exactly one occurrence, found {found}")
        offsets.append(text.index(passage))

    projected = text
    for passage, replacement in _REPLACEMENTS:
        projected = projected.replace(passage, replacement, 1)

    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            projected.splitlines(keepends=True),
            fromfile=_DIFF_FROM,
            tofile=_DIFF_TO,
        )
    )
    record: dict[str, object] = {
        "profile": PROFILE,
        "changed_lines": sorted(text.count("\n", 0, offset) + 1 for offset in offsets),
        "source_sha256": _sha256(text),
        "execution_sha256": _sha256(projected),
        "diff": diff,
        "diff_sha256": _sha256(diff),
    }
    return projected, record


def load_instruction(path: str | Path) -> str:
    """Read a UTF-8 instruction after rejecting symlinks and oversize files."""
    source = Path(path)
    if source.is_symlink():
        raise ValueError(f"refusing symlink: {source}")
    data = source.read_bytes()
    if len(data) > MAX_INSTRUCTION_BYTES:
        raise ValueError(
            f"instruction exceeds 256 KiB ({MAX_INSTRUCTION_BYTES} bytes): {source}"
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"instruction is not valid UTF-8: {source}") from exc
