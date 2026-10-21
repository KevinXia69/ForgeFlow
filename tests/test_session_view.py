from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgeflow import session_view
from forgeflow.session_view import list_session_views, read_session_view

REPO = Path(__file__).resolve().parents[1]
REAL_CACHE = REPO / ".runtime/dsh-home/storages/session_projcache/sessions"

SID_A = "session-11111111-1111-4111-8111-111111111111"
SID_B = "session-22222222-2222-4222-8222-222222222222"
SID_C = "session-33333333-3333-4333-8333-333333333333"
SID_D = "session-44444444-4444-4444-8444-444444444444"


def _row(value: object, version: int, seq: int) -> dict[str, object]:
    return {"ver": version, "seq": seq, "val": value}


def valid_document(
    *,
    title: str | None = "供应商断供复盘",
    version: int = 7,
    format_version: int | None = 4,
    seq: int = 40,
    title_version: int = 1,
    token_version: int = 2,
    pressure_version: int = 5,
    turn_version: int = 2,
    rows: tuple[str, ...] = ("title", "tokenUsage", "contextPressure", "turnBoundary"),
    created_at: int = 1760000000000,
) -> dict[str, object]:
    stored: dict[str, object] = {}
    if "title" in rows:
        stored["title"] = _row(title, title_version, seq)
    if "tokenUsage" in rows:
        stored["tokenUsage"] = _row(
            {
                "totals": {
                    "uncachedInputTokens": 1200,
                    "outputTokens": 340,
                    "cacheReadTokens": 700,
                    "cacheWriteTokens": 80,
                },
                "last": {
                    "turn": 3,
                    "step": 5,
                    "buckets": {
                        "uncachedInputTokens": 10,
                        "outputTokens": 20,
                        "cacheReadTokens": 30,
                        "cacheWriteTokens": 40,
                    },
                },
            },
            token_version,
            seq,
        )
    if "contextPressure" in rows:
        stored["contextPressure"] = _row(
            {
                "surfaceTokens": 1800,
                "contextWindow": 4000,
                "pressureTokens": 2500,
                "sampledSurfaceTokens": 1700,
            },
            pressure_version,
            seq,
        )
    if "turnBoundary" in rows:
        stored["turnBoundary"] = _row(
            {
                "openTurnStartSeq": None,
                "lastStepStartSeq": 39,
                "lastStepBoundary": {"kind": "end", "seq": 39},
                "lastTurn": 3,
            },
            turn_version,
            seq,
        )
    identity: dict[str, object] = {
        "createdAt": created_at,
        "cwd": "/tmp/forgeflow-workspace",
        "isSeeded": False,
        "inheritedEventCount": 0,
    }
    if format_version is not None:
        identity["formatVersion"] = format_version
    return {"version": version, "record": {"identity": identity, "rows": stored}}


def write_document(
    root: Path, session_id: str, payload: object, *, name: str | None = None
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name or session_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_valid_document_projects_recorded_values(tmp_path):
    path = write_document(tmp_path, SID_A, valid_document())

    view = read_session_view(tmp_path, SID_A)

    assert view is not None
    assert view["schema_version"] == 1
    assert view["kind"] == "forgeflow.session_view"
    assert view["session_id"] == SID_A
    assert view["title"] == "供应商断供复盘"
    assert view["sequence"] == 40
    assert view["created_at"] == "2025-10-09T08:53:20Z"
    assert view["updated_at"] is None
    assert view["last_turn"] == 3
    assert view["last_step"] == {"kind": "end", "seq": 39}
    assert view["open_turn_start_seq"] is None
    assert view["tokens"] == {
        "uncached_input": 1200,
        "output": 340,
        "cache_read": 700,
        "cache_write": 80,
        "total": 2320,
    }
    assert view["context"] == {
        "surface_tokens": 1800,
        "pressure_tokens": 2500,
        "window_tokens": 4000,
        "sampled_surface_tokens": 1700,
        "pressure_ratio": 0.625,
    }
    assert view["format"] == {
        "cache_version": 7,
        "event_format_version": 4,
        "supported": True,
        "current": True,
        "row_versions": {
            "title": 1,
            "tokenUsage": 2,
            "contextPressure": 5,
            "turnBoundary": 2,
        },
        "issues": [],
    }
    assert view["status"] == "ok"
    assert view["missing"] == []
    assert view["source"] == {"file": f"{SID_A}.json", "bytes": path.stat().st_size}


def test_missing_rows_stay_unknown_and_are_named(tmp_path):
    write_document(tmp_path, SID_A, valid_document(rows=("title",)))

    view = read_session_view(tmp_path, SID_A)

    assert view is not None
    assert view["title"] == "供应商断供复盘"
    assert view["tokens"]["uncached_input"] is None
    assert view["tokens"]["total"] is None
    assert view["context"]["window_tokens"] is None
    assert view["last_turn"] is None
    assert view["status"] == "degraded"
    issues = view["format"]["issues"]
    for key in ("tokenUsage", "contextPressure", "turnBoundary"):
        assert f"row_missing:{key}" in issues
    assert "row_missing:title" not in issues
    assert "tokens.total" in view["missing"]
    assert "context.window_tokens" in view["missing"]


def test_malformed_json_is_skipped_without_crashing(tmp_path):
    (tmp_path / f"{SID_A}.json").write_text('{"version": 7, "record": ', encoding="utf-8")
    (tmp_path / f"{SID_C}.json").write_text("[]", encoding="utf-8")
    write_document(tmp_path, SID_B, valid_document(seq=12))

    assert read_session_view(tmp_path, SID_A) is None
    assert read_session_view(tmp_path, SID_C) is None
    assert [view["session_id"] for view in list_session_views(tmp_path)] == [SID_B]


@pytest.mark.parametrize(
    ("version", "format_version", "expected_issue"),
    [
        (999, 4, "unsupported_cache_version:999"),
        (7, 3, "unsupported_event_format_version:3"),
        (7, None, "event_format_version_missing"),
    ],
)
def test_unknown_version_projects_no_values(tmp_path, version, format_version, expected_issue):
    write_document(
        tmp_path,
        SID_A,
        valid_document(version=version, format_version=format_version),
    )

    view = read_session_view(tmp_path, SID_A)

    assert view is not None
    assert view["format"]["supported"] is False
    assert view["format"]["current"] is False
    assert expected_issue in view["format"]["issues"]
    assert view["status"] == "unsupported"
    assert view["title"] is None
    assert view["sequence"] is None
    assert view["created_at"] is None
    assert view["tokens"]["total"] is None
    assert view["context"]["window_tokens"] is None
    assert "tokens.total" in view["missing"]


def test_compatible_older_cache_version_is_readable_and_flagged(tmp_path):
    write_document(tmp_path, SID_A, valid_document(version=6))

    view = read_session_view(tmp_path, SID_A)

    assert view is not None
    assert view["format"]["supported"] is True
    assert view["format"]["current"] is False
    assert view["status"] == "ok"
    assert "compatible_older_cache_version:6" in view["format"]["issues"]
    assert view["title"] == "供应商断供复盘"
    assert view["tokens"]["total"] == 2320


def test_row_state_version_mismatch_discards_only_that_row(tmp_path):
    write_document(tmp_path, SID_A, valid_document(token_version=1))

    view = read_session_view(tmp_path, SID_A)

    assert view is not None
    assert view["title"] == "供应商断供复盘"
    assert view["tokens"] == {
        "uncached_input": None,
        "output": None,
        "cache_read": None,
        "cache_write": None,
        "total": None,
    }
    assert "row_version_mismatch:tokenUsage" in view["format"]["issues"]
    assert view["status"] == "degraded"
    assert view["context"]["window_tokens"] == 4000


def test_recorded_update_timestamp_is_projected(tmp_path):
    payload = valid_document()
    payload["record"]["rows"]["sessionListMetadata"] = _row(
        {"blank": False, "lastPromptAt": 1760000001000}, 1, 41
    )
    write_document(tmp_path, SID_A, payload)

    view = read_session_view(tmp_path, SID_A)

    assert view is not None
    assert view["updated_at"] == "2025-10-09T08:53:21Z"
    assert view["sequence"] == 41


def test_symlinked_document_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    target = write_document(outside, SID_A, valid_document())
    root = tmp_path / "cache"
    root.mkdir()
    link = root / f"{SID_A}.json"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not supported on this platform")

    assert read_session_view(root, SID_A) is None
    assert list_session_views(root) == []


def test_symlinked_root_is_rejected(tmp_path):
    real = tmp_path / "real"
    write_document(real, SID_A, valid_document())
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are not supported on this platform")

    assert read_session_view(alias, SID_A) is None
    assert list_session_views(alias) == []


def test_ordering_is_deterministic_and_limit_is_applied(tmp_path):
    write_document(tmp_path, SID_B, valid_document(title="Beta", seq=30))
    write_document(tmp_path, SID_A, valid_document(title="Alpha", seq=10))
    write_document(tmp_path, SID_C, valid_document(title="alpha", seq=30))
    write_document(tmp_path, SID_D, valid_document(version=999))

    views = list_session_views(tmp_path)
    assert [view["session_id"] for view in views] == [SID_C, SID_B, SID_A, SID_D]
    assert list_session_views(tmp_path) == views

    assert [view["session_id"] for view in list_session_views(tmp_path, limit=2)] == [
        SID_C,
        SID_B,
    ]
    assert list_session_views(tmp_path, limit=0) == []
    with pytest.raises(ValueError):
        list_session_views(tmp_path, limit=-1)
    with pytest.raises(TypeError):
        list_session_views(tmp_path, limit=True)  # type: ignore[arg-type]


def test_missing_root_and_unsafe_session_ids_are_rejected(tmp_path):
    missing = tmp_path / "absent"
    assert list_session_views(missing) == []
    assert read_session_view(missing, SID_A) is None

    write_document(tmp_path, SID_A, valid_document())
    for session_id in ("..", "../escape", "a/b", "", ".hidden", "x" * 300, "session id"):
        assert read_session_view(tmp_path, session_id) is None
    assert read_session_view(tmp_path, SID_A) is not None


def test_oversized_document_is_rejected(tmp_path, monkeypatch):
    write_document(tmp_path, SID_A, valid_document())
    monkeypatch.setattr(session_view, "MAX_DOC_BYTES", 64)

    assert read_session_view(tmp_path, SID_A) is None
    assert list_session_views(tmp_path) == []


def test_document_without_rows_is_degraded(tmp_path):
    payload = {
        "version": 7,
        "record": {
            "identity": {"formatVersion": 4, "createdAt": 1760000000000},
        },
    }
    write_document(tmp_path, SID_A, payload)

    view = read_session_view(tmp_path, SID_A)

    assert view is not None
    assert view["status"] == "degraded"
    assert "rows_missing" in view["format"]["issues"]
    assert view["format"]["row_versions"] == {}
    assert view["title"] is None
    assert view["missing"]


def test_non_object_record_is_flagged(tmp_path):
    write_document(tmp_path, SID_A, {"version": 7, "record": []})

    view = read_session_view(tmp_path, SID_A)

    assert view is not None
    assert view["status"] == "unsupported"
    assert "record_missing" in view["format"]["issues"]


def test_reader_never_writes_to_the_cache(tmp_path):
    write_document(tmp_path, SID_A, valid_document())
    write_document(tmp_path, SID_B, valid_document(seq=10))
    before = {path.name: path.stat().st_mtime_ns for path in tmp_path.iterdir()}

    list_session_views(tmp_path)
    read_session_view(tmp_path, SID_A)

    after = {path.name: path.stat().st_mtime_ns for path in tmp_path.iterdir()}
    assert after == before


def test_real_projection_cache_is_readable_without_modification():
    if _is_symlink(REAL_CACHE) or not REAL_CACHE.is_dir():
        pytest.skip("real DSH projection cache is not present")
    try:
        documents = sorted(
            path
            for path in REAL_CACHE.iterdir()
            if path.suffix == ".json" and path.is_file() and not _is_symlink(path)
        )
    except OSError:
        pytest.skip("real DSH projection cache is not readable")
    if not documents:
        pytest.skip("real DSH projection cache contains no documents")

    before = {path.name: path.stat().st_mtime_ns for path in documents}
    views = list_session_views(REAL_CACHE)

    # The fixture is not required to keep any exact count; only the documents
    # present at read time must be projected, one view each.
    assert len(views) == len(documents)
    assert {view["session_id"] for view in views} == {path.stem for path in documents}
    assert all(view["format"]["event_format_version"] == 4 for view in views)
    assert all(view["format"]["supported"] for view in views)
    assert all(view["status"] in ("ok", "degraded") for view in views)
    assert all("/" not in view["source"]["file"] for view in views)

    limited = list_session_views(REAL_CACHE, limit=3)
    assert [view["session_id"] for view in limited] == [view["session_id"] for view in views[:3]]
    first = read_session_view(REAL_CACHE, views[0]["session_id"])
    assert first is not None
    assert first["session_id"] == views[0]["session_id"]

    after = {path.name: path.stat().st_mtime_ns for path in documents}
    assert after == before


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True


def test_turn_outline_and_stats_are_bounded_optional_projections() -> None:
    outline = session_view._turn_outline({"turns": [
        {"turn": 1, "seq": 7, "prompt": "p" * 500, "response": "r" * 500},
        "bad",
    ]})
    assert outline == [{"turn": 1, "seq": 7, "prompt": "p" * 120, "response": "r" * 240}]
    assert session_view._turn_outline(None) is None
    stats = session_view._stats({"turns": 2, "steps": 3, "llmMs": 4.5, "toolMs": 5})
    assert stats == {
        "turns": 2, "steps": 3, "llmMs": None, "toolMs": 5,
        "ttftMs": None, "decodeMs": None, "decodeTokens": None,
    }
