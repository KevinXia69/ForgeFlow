"""Tests for the permission-split-v1 instruction projection."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from forgeflow.permission_profile import (
    READ_KEY_PATH,
    WRITE_KEY_PATH,
    load_instruction,
    profile_instruction,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASKS = (
    PROJECT_ROOT / "vendor/erp-bench/tasks/2280_medium_repair_plan_medium/instruction.md",
    PROJECT_ROOT / "vendor/erp-bench/tasks/2299_hard_repair_plan_hard/instruction.md",
)
TASK_IDS = [path.parent.name for path in TASKS]

KEY_BULLET = f"- **API Key:** stored in `{WRITE_KEY_PATH}`"
ACCESS_INTRO = (
    "You can write and execute Python scripts using the `odoo-client-lib` library "
    "to interact with Odoo via the JSON-2 API:"
)
PASSWORD_OPEN = f"database='bench', password=open('{WRITE_KEY_PATH}').read().strip()"
PASSAGES = (KEY_BULLET, ACCESS_INTRO, PASSWORD_OPEN)
WRITE_PASSWORD_CALL = f"password=open('{WRITE_KEY_PATH}')"

READ_BULLET = f"- **Read-only API Key:** stored in `{READ_KEY_PATH}`"
READ_INTRO = (
    "You can execute read-only Python scripts using the `odoo-client-lib` library "
    "to read Odoo via the JSON-2 API:"
)
READ_PASSWORD_OPEN = f"database='bench', password=open('{READ_KEY_PATH}').read().strip()"
WRITE_WARNING = (
    "- **Write access:** this credential is read-only; direct Odoo writes are rejected. "
    "Apply ERP changes only through `execute_recovery` after `prepare_recovery`."
)


def _apply_diff(original: str, diff: str) -> str:
    """Apply a keepends unified diff produced by ``difflib.unified_diff``."""
    source = original.splitlines(keepends=True)
    result: list[str] = []
    cursor = 0
    for line in diff.splitlines(keepends=True):
        if line.startswith("@@"):
            start = int(line.split()[1][1:].split(",")[0])
            result.extend(source[cursor:start - 1])
            cursor = start - 1
        elif line.startswith(("---", "+++")):
            continue
        elif line.startswith("+"):
            result.append(line[1:])
        elif line.startswith("-"):
            cursor += 1
        elif line.startswith(" "):
            result.append(line[1:])
            cursor += 1
    result.extend(source[cursor:])
    return "".join(result)


@pytest.mark.parametrize("path", TASKS, ids=TASK_IDS)
def test_projection_targets_pinned_instructions(path: Path) -> None:
    original = load_instruction(path)
    assert all(original.count(passage) == 1 for passage in PASSAGES)
    projected, record = profile_instruction(original)
    assert record["profile"] == "permission-split-v1"
    assert READ_KEY_PATH in projected
    assert READ_BULLET in projected
    assert READ_INTRO in projected
    assert READ_PASSWORD_OPEN in projected
    assert WRITE_PASSWORD_CALL not in projected
    assert projected.count(WRITE_WARNING) == 1


@pytest.mark.parametrize("path", TASKS, ids=TASK_IDS)
def test_record_hashes_and_diff_reconstruct_projection(path: Path) -> None:
    original = load_instruction(path)
    projected, record = profile_instruction(original)
    expected_lines = [original.count("\n", 0, original.index(passage)) + 1 for passage in PASSAGES]
    changed_lines = record["changed_lines"]
    assert isinstance(changed_lines, list)
    assert len(changed_lines) == 3
    assert changed_lines == sorted(changed_lines) == expected_lines
    assert record["source_sha256"] == hashlib.sha256(original.encode("utf-8")).hexdigest()
    assert record["execution_sha256"] == hashlib.sha256(projected.encode("utf-8")).hexdigest()
    diff = record["diff"]
    assert isinstance(diff, str)
    assert diff.splitlines()[:2] == [
        "--- original/instruction.md",
        "+++ permission-split-v1/instruction.md",
    ]
    assert record["diff_sha256"] == hashlib.sha256(diff.encode("utf-8")).hexdigest()
    assert _apply_diff(original, diff) == projected


@pytest.mark.parametrize("path", TASKS, ids=TASK_IDS)
def test_projected_output_cannot_be_projected_again(path: Path) -> None:
    projected, _ = profile_instruction(load_instruction(path))
    with pytest.raises(ValueError, match="already_projected"):
        profile_instruction(projected)


@pytest.mark.parametrize("index", range(len(PASSAGES)))
def test_missing_passage_reports_its_index(index: int) -> None:
    original = load_instruction(TASKS[0])
    damaged = original.replace(PASSAGES[index], "", 1)
    with pytest.raises(ValueError, match=rf"passage {index}\b"):
        profile_instruction(damaged)


@pytest.mark.parametrize("index", range(len(PASSAGES)))
def test_duplicated_passage_reports_its_index(index: int) -> None:
    original = load_instruction(TASKS[0])
    damaged = original.replace(PASSAGES[index], PASSAGES[index] * 2, 1)
    with pytest.raises(ValueError, match=rf"passage {index}\b"):
        profile_instruction(damaged)


@pytest.mark.parametrize("path", TASKS, ids=TASK_IDS)
def test_projection_keeps_every_other_line_byte_identical(path: Path) -> None:
    original = load_instruction(path)
    projected, record = profile_instruction(original)
    changed_lines = record["changed_lines"]
    assert isinstance(changed_lines, list)
    source_lines = original.splitlines(keepends=True)
    projected_lines = projected.splitlines(keepends=True)
    # The first replacement turns one line into two, shifting later lines by one.
    dropped_source = {line - 1 for line in changed_lines}
    dropped_projected = {changed_lines[0] - 1, changed_lines[0]} | set(changed_lines[1:])
    assert [line for i, line in enumerate(source_lines) if i not in dropped_source] == [
        line for i, line in enumerate(projected_lines) if i not in dropped_projected
    ]


def test_load_instruction_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "instruction.md"
    target.write_text("content\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_instruction(link)


def test_load_instruction_rejects_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "instruction.md"
    path.write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(ValueError, match="256 KiB"):
        load_instruction(path)
