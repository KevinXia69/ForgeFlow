"""Read-only projection of DSH's persisted session projection cache.

DSH stores one checkpoint document per session under
``<dsh-home>/storages/session_projcache/sessions/<session-id>.json``.  Each
document contains the storage-domain version, the session-log identity the
checkpoint was folded from, and one ``{ver, seq, val}`` row per projection
unit.  This module projects those documents into a small typed view for the
ForgeFlow session list.  It never starts DSH, never decodes a session log, and
never writes to the cache.

The cache is derived, possibly stale data, so the projection follows four
honesty rules:

* A missing, stale, or incompatible value stays ``None`` and is named in
  ``missing``/``format["issues"]``.  It is never replaced by a default such as
  zero or an invented label.
* A document is projected only when both its storage-domain version and its
  session event-format version are understood.  An unknown version yields
  ``status="unsupported"`` and no projected values.
* A row's ``ver`` must match the projection unit's current ``stateVersion``.
  A mismatch is a discarded row, not a value to display.
* Symlinks are never followed, non-regular files are never opened, and a
  document larger than ``MAX_DOC_BYTES`` is rejected.  Every file is opened
  read-only with ``O_NOFOLLOW`` where the platform provides it.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

SCHEMA_VERSION = 1
KIND = "forgeflow.session_view"

#: Current `session_projcache` storage-domain version.  DSH also admits the
#: preceding structurally compatible versions 3-6 for a later checkpoint
#: rewrite, so listing those documents is safe; their rows are still guarded
#: individually by ``ROW_STATE_VERSIONS``.
CURRENT_CACHE_VERSION = 7
COMPATIBLE_CACHE_VERSIONS = frozenset({3, 4, 5, 6, 7})

#: Current session event-log format generation.  A checkpoint folded under any
#: other (or absent) generation cannot be proven compatible with this reader's
#: row semantics, so its values are not projected.
SESSION_FORMAT_VERSION = 4

#: Current projection-unit `stateVersion` per stored row.
ROW_STATE_VERSIONS: dict[str, int] = {
    "title": 1,
    "tokenUsage": 2,
    "contextPressure": 5,
    "turnBoundary": 2,
    "turnOutline": 2,
    "sessionStats": 1,
}

MAX_DOC_BYTES = 4 * 1024 * 1024
MAX_INT = 2**53 - 1
MAX_TITLE_CHARS = 300
MAX_TIMESTAMP_CHARS = 50

#: A session id becomes a filename below the cache root, so it must not carry
#: path separators, a leading dot, or any other traversal form.
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
STEP_KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}")

Status = Literal["ok", "degraded", "unsupported"]

#: Issues that mean a supported document could not supply every row.
_DEGRADING_ISSUES = (
    "record_missing",
    "rows_missing",
    "row_missing:",
    "row_version_mismatch:",
    "row_value_invalid:",
)


class TokenView(TypedDict):
    """Recorded token buckets and their conservative sum."""

    uncached_input: int | None
    output: int | None
    cache_read: int | None
    cache_write: int | None
    total: int | None


class ContextView(TypedDict):
    """Recorded context occupancy; the ratio is derived, never estimated."""

    surface_tokens: int | None
    pressure_tokens: int | None
    window_tokens: int | None
    sampled_surface_tokens: int | None
    pressure_ratio: float | None


class FormatView(TypedDict):
    """Version and per-row compatibility state for one document."""

    cache_version: int | None
    event_format_version: int | None
    supported: bool
    current: bool
    row_versions: dict[str, int]
    issues: list[str]


class LastStepView(TypedDict):
    """The last recorded step boundary."""

    kind: str | None
    seq: int | None


class SourceView(TypedDict):
    """Non-secret provenance for the projected document."""

    file: str
    bytes: int | None


class SessionView(TypedDict):
    """Typed read-only projection of one DSH session checkpoint."""

    schema_version: int
    kind: str
    session_id: str
    title: str | None
    sequence: int | None
    created_at: str | None
    updated_at: str | None
    last_turn: int | None
    last_step: LastStepView | None
    open_turn_start_seq: int | None
    turn_outline: list[dict[str, Any]] | None
    stats: dict[str, Any] | None
    tokens: TokenView
    context: ContextView
    format: FormatView
    status: Status
    missing: list[str]
    source: SourceView


def _count(value: object) -> int | None:
    """Accept only a non-negative JSON integer, never a bool or a float."""
    return value if type(value) is int and 0 <= value <= MAX_INT else None


def _text(value: object, limit: int = MAX_TITLE_CHARS) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] if cleaned else None


def _iso_ms(value: object) -> str | None:
    """Normalize a recorded epoch-millisecond timestamp to ISO-8601 UTC."""
    if type(value) is not int or not 0 <= value <= MAX_INT:
        return None
    try:
        moment = datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _timestamp(value: object) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or len(cleaned) > MAX_TIMESTAMP_CHARS:
            return None
        try:
            datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            return None
        return cleaned
    return _iso_ms(value)


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True


def _cache_root(root: str | Path) -> Path | None:
    """Accept a real directory; a symlinked root is not trusted."""
    path = Path(root)
    if _is_symlink(path):
        return None
    try:
        return path if path.is_dir() else None
    except OSError:
        return None


def _load_document(path: Path) -> tuple[dict[str, Any] | None, int | None]:
    """Read one bounded JSON object without following a final symlink.

    Returns ``(document, bytes_read)``; any unsafe, oversized, undecodable, or
    non-object document returns ``(None, ...)`` instead of raising.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None, None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None, info.st_size
        if info.st_size > MAX_DOC_BYTES:
            return None, info.st_size
        chunks: list[bytes] = []
        remaining = MAX_DOC_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_DOC_BYTES:
            return None, len(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None, len(data)
        document = json.loads(text)
    except (OSError, ValueError):
        return None, None
    finally:
        os.close(descriptor)
    if not isinstance(document, dict):
        return None, len(data)
    return document, len(data)


def _row(record: dict[str, Any], key: str) -> dict[str, Any] | None:
    rows = record.get("rows")
    if not isinstance(rows, dict):
        return None
    row = rows.get(key)
    return row if isinstance(row, dict) else None


def _usable_row(record: dict[str, Any], key: str, issues: list[str]) -> dict[str, Any] | None:
    """Return a row only when its unit state version is the current one."""
    row = _row(record, key)
    if row is None:
        issues.append(f"row_missing:{key}")
        return None
    if _count(row.get("ver")) != ROW_STATE_VERSIONS[key]:
        issues.append(f"row_version_mismatch:{key}")
        return None
    return row


def _mapping_row(record: dict[str, Any], key: str, issues: list[str]) -> dict[str, Any] | None:
    row = _usable_row(record, key, issues)
    if row is None:
        return None
    value = row.get("val")
    if not isinstance(value, dict):
        issues.append(f"row_value_invalid:{key}")
        return None
    return value


def _sequence(record: dict[str, Any]) -> int | None:
    """The record watermark: the newest valid row sequence."""
    rows = record.get("rows")
    if not isinstance(rows, dict):
        return None
    sequences = [
        sequence
        for row in rows.values()
        if isinstance(row, dict)
        for sequence in (_count(row.get("seq")),)
        if sequence is not None
    ]
    return max(sequences) if sequences else None


def _row_versions(record: dict[str, Any]) -> dict[str, int]:
    versions: dict[str, int] = {}
    for key in ROW_STATE_VERSIONS:
        row = _row(record, key)
        version = _count(row.get("ver")) if row is not None else None
        if version is not None:
            versions[key] = version
    return versions


def _tokens(state: dict[str, Any] | None) -> TokenView:
    totals = state.get("totals") if isinstance(state, dict) else None
    totals = totals if isinstance(totals, dict) else {}
    buckets = (
        _count(totals.get("uncachedInputTokens")),
        _count(totals.get("outputTokens")),
        _count(totals.get("cacheReadTokens")),
        _count(totals.get("cacheWriteTokens")),
    )
    total = None
    if all(bucket is not None for bucket in buckets):
        total = sum(bucket for bucket in buckets if bucket is not None)
    return {
        "uncached_input": buckets[0],
        "output": buckets[1],
        "cache_read": buckets[2],
        "cache_write": buckets[3],
        "total": total,
    }


def _context(state: dict[str, Any] | None) -> ContextView:
    state = state if isinstance(state, dict) else {}
    surface = _count(state.get("surfaceTokens"))
    pressure = _count(state.get("pressureTokens"))
    window = _count(state.get("contextWindow"))
    if window == 0:
        window = None
    sampled = _count(state.get("sampledSurfaceTokens"))
    ratio = round(pressure / window, 4) if pressure is not None and window is not None else None
    return {
        "surface_tokens": surface,
        "pressure_tokens": pressure,
        "window_tokens": window,
        "sampled_surface_tokens": sampled,
        "pressure_ratio": ratio,
    }


def _turn(
    state: dict[str, Any] | None,
) -> tuple[int | None, LastStepView | None, int | None]:
    state = state if isinstance(state, dict) else {}
    last_turn = _count(state.get("lastTurn"))
    open_start = _count(state.get("openTurnStartSeq"))
    boundary = state.get("lastStepBoundary")
    last_step: LastStepView | None = None
    if isinstance(boundary, dict):
        kind = boundary.get("kind")
        kind = kind if isinstance(kind, str) and STEP_KIND.fullmatch(kind) else None
        last_step = {"kind": kind, "seq": _count(boundary.get("seq"))}
    return last_turn, last_step, open_start


def _updated_at(document: dict[str, Any], record: dict[str, Any]) -> str | None:
    """Read an optional update timestamp; current DSH documents omit it."""
    for key in ("updatedAt", "updated_at"):
        for holder in (document, record):
            stamp = _timestamp(holder.get(key))
            if stamp is not None:
                return stamp
    for row_key, field in (
        ("sessionListMetadata", "lastPromptAt"),
        ("timeContext", "lastMessageTime"),
    ):
        row = _row(record, row_key)
        value = row.get("val") if row is not None else None
        if isinstance(value, dict):
            stamp = _timestamp(value.get(field))
            if stamp is not None:
                return stamp
    return None


def _empty_tokens() -> TokenView:
    return {
        "uncached_input": None,
        "output": None,
        "cache_read": None,
        "cache_write": None,
        "total": None,
    }


def _empty_context() -> ContextView:
    return {
        "surface_tokens": None,
        "pressure_tokens": None,
        "window_tokens": None,
        "sampled_surface_tokens": None,
        "pressure_ratio": None,
    }


def _missing(view: SessionView) -> list[str]:
    """Name every projected value that stayed unknown."""
    values: list[tuple[str, object]] = [
        ("title", view["title"]),
        ("sequence", view["sequence"]),
        ("created_at", view["created_at"]),
        ("last_turn", view["last_turn"]),
        ("tokens.uncached_input", view["tokens"]["uncached_input"]),
        ("tokens.output", view["tokens"]["output"]),
        ("tokens.cache_read", view["tokens"]["cache_read"]),
        ("tokens.cache_write", view["tokens"]["cache_write"]),
        ("tokens.total", view["tokens"]["total"]),
        ("context.surface_tokens", view["context"]["surface_tokens"]),
        ("context.pressure_tokens", view["context"]["pressure_tokens"]),
        ("context.window_tokens", view["context"]["window_tokens"]),
        ("context.sampled_surface_tokens", view["context"]["sampled_surface_tokens"]),
        ("context.pressure_ratio", view["context"]["pressure_ratio"]),
    ]
    return [name for name, value in values if value is None]


def _turn_outline(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, dict) or not isinstance(value.get("turns"), list):
        return None
    turns: list[dict[str, Any]] = []
    for turn in value["turns"][:200]:
        if not isinstance(turn, dict):
            continue
        turns.append({
            "turn": _count(turn.get("turn")),
            "seq": _count(turn.get("seq")),
            "prompt": _text(turn.get("prompt"), 120),
            "response": _text(turn.get("response"), 240),
        })
    return turns


def _stats(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    keys = ("turns", "steps", "llmMs", "toolMs", "ttftMs", "decodeMs", "decodeTokens")
    return {key: _count(value.get(key)) for key in keys}


def _project(
    session_id: str,
    document: dict[str, Any],
    filename: str,
    size: int,
) -> SessionView:
    issues: list[str] = []
    raw_record = document.get("record")
    record = raw_record if isinstance(raw_record, dict) else {}
    if not isinstance(raw_record, dict):
        issues.append("record_missing")
    elif not isinstance(record.get("rows"), dict):
        issues.append("rows_missing")

    identity = record.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    cache_version = _count(document.get("version"))
    format_version = _count(identity.get("formatVersion"))

    if cache_version is None:
        issues.append("cache_version_missing")
    elif cache_version not in COMPATIBLE_CACHE_VERSIONS:
        issues.append(f"unsupported_cache_version:{cache_version}")
    elif cache_version != CURRENT_CACHE_VERSION:
        issues.append(f"compatible_older_cache_version:{cache_version}")

    if format_version is None:
        issues.append("event_format_version_missing")
    elif format_version != SESSION_FORMAT_VERSION:
        issues.append(f"unsupported_event_format_version:{format_version}")

    supported = (
        cache_version in COMPATIBLE_CACHE_VERSIONS and format_version == SESSION_FORMAT_VERSION
    )
    current = cache_version == CURRENT_CACHE_VERSION and format_version == SESSION_FORMAT_VERSION

    if supported:
        title_row = _usable_row(record, "title", issues)
        title = _text(title_row.get("val")) if title_row is not None else None
        tokens = _tokens(_mapping_row(record, "tokenUsage", issues))
        context = _context(_mapping_row(record, "contextPressure", issues))
        last_turn, last_step, open_start = _turn(_mapping_row(record, "turnBoundary", issues))
        rows = record.get("rows") if isinstance(record.get("rows"), dict) else {}
        outline_row = _usable_row(record, "turnOutline", issues) if "turnOutline" in rows else None
        stats_row = _usable_row(record, "sessionStats", issues) if "sessionStats" in rows else None
        turn_outline = _turn_outline(outline_row.get("val")) if outline_row is not None else None
        stats = _stats(stats_row.get("val")) if stats_row is not None else None
        sequence = _sequence(record)
        created_at = _iso_ms(identity.get("createdAt"))
        updated_at = _updated_at(document, record)
    else:
        title = None
        tokens = _empty_tokens()
        context = _empty_context()
        last_turn, last_step, open_start = None, None, None
        turn_outline = None
        stats = None
        sequence = None
        created_at = None
        updated_at = None

    if not supported:
        status: Status = "unsupported"
    elif any(issue.startswith(_DEGRADING_ISSUES) for issue in issues):
        status = "degraded"
    else:
        status = "ok"

    view = SessionView(
        schema_version=SCHEMA_VERSION,
        kind=KIND,
        session_id=session_id,
        title=title,
        sequence=sequence,
        created_at=created_at,
        updated_at=updated_at,
        last_turn=last_turn,
        last_step=last_step,
        open_turn_start_seq=open_start,
        turn_outline=turn_outline,
        stats=stats,
        tokens=tokens,
        context=context,
        format={
            "cache_version": cache_version,
            "event_format_version": format_version,
            "supported": supported,
            "current": current,
            "row_versions": _row_versions(record),
            "issues": issues,
        },
        status=status,
        missing=[],
        source=SourceView(file=filename, bytes=size),
    )
    view["missing"] = _missing(view)
    return view


def _sort_key(view: SessionView) -> tuple[bool, int, str, str]:
    """Newest recorded watermark first; unknown watermarks last."""
    sequence = view["sequence"]
    return (
        sequence is None,
        -(sequence or 0),
        (view["title"] or "").casefold(),
        view["session_id"],
    )


def list_session_views(root: str | Path, limit: int | None = None) -> list[SessionView]:
    """Project every cache document below ``root`` in a deterministic order.

    ``root`` is the ``.../session_projcache/sessions`` directory itself.  A
    missing or symlinked root, unreadable entries, symlinks, oversized files,
    and malformed JSON are skipped rather than raised.  ``limit`` is applied
    after sorting; ``0`` returns an empty list.
    """
    if limit is not None and type(limit) is not int:
        raise TypeError("limit must be an int or None")
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")

    base = _cache_root(root)
    if base is None:
        return []
    try:
        entries = sorted(base.iterdir(), key=lambda path: path.name)
    except OSError:
        return []

    views: list[SessionView] = []
    for path in entries:
        if path.suffix != ".json" or not SESSION_ID.fullmatch(path.stem):
            continue
        if _is_symlink(path):
            continue
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        document, size = _load_document(path)
        if document is None or size is None:
            continue
        views.append(_project(path.stem, document, path.name, size))

    views.sort(key=_sort_key)
    return views[:limit] if limit is not None else views


def read_session_view(root: str | Path, session_id: str) -> SessionView | None:
    """Project one session, or return ``None`` when it is absent or unsafe.

    A structurally readable document with an unknown version still returns a
    view (``status="unsupported"``); a missing, symlinked, oversized, or
    non-JSON document returns ``None``.
    """
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        return None
    base = _cache_root(root)
    if base is None:
        return None
    path = base / f"{session_id}.json"
    if _is_symlink(path):
        return None
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    document, size = _load_document(path)
    if document is None or size is None:
        return None
    return _project(session_id, document, path.name, size)
