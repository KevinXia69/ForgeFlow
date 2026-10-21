"""Loopback-only, read-only viewer for recorded ForgeFlow experiment results."""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .runview import (
    RUN_ID,
    build_run_view,
    legacy_record,
)
from .ui_trace import MAX_TRACE_BYTES, parse_pi_events

ASSETS = Path(__file__).parent / "ui_assets"
MAX_JSON_BYTES = 4 * 1024 * 1024


def _local_file(path: Path, boundary: Path) -> bool:
    try:
        parts = path.relative_to(boundary).parts
    except ValueError:
        return False
    cursor = boundary
    if cursor.is_symlink():
        return False
    for part in parts:
        cursor /= part
        if cursor.is_symlink():
            return False
    return path.is_file() and path.resolve().is_relative_to(boundary.resolve())


def _json_file(path: Path, boundary: Path) -> dict[str, Any] | None:
    if not _local_file(path, boundary):
        return None
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _text(value: object, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _number(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _elapsed(started: object, finished: object) -> float | None:
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        duration = (
            datetime.fromisoformat(finished) - datetime.fromisoformat(started)
        ).total_seconds()
        return round(duration, 1) if duration >= 0 else None
    except (TypeError, ValueError):
        return None


def _run_order(record: dict[str, Any]) -> tuple[bool, datetime, str]:
    started = None
    if isinstance(record["started_at"], str):
        try:
            started = datetime.fromisoformat(record["started_at"])
            # The source field is started_at_utc, including older offsetless records.
            started = started.replace(tzinfo=UTC) if started.tzinfo is None else started.astimezone(UTC)
        except (ValueError, OverflowError):
            started = None
    return started is not None, started or datetime.min.replace(tzinfo=UTC), record["run_id"]


def _run_record(run_dir: Path, runs_root: Path) -> dict[str, Any] | None:
    """Serve the legacy flat fields from the shared, versioned RunView."""
    view = build_run_view(run_dir, runs_root)
    return legacy_record(view) if view is not None else None


def build_snapshot(workspace_root: str | Path) -> dict[str, Any]:
    """Expose selected result fields only; never read credentials or serve logs."""
    root = Path(workspace_root).resolve()
    runs_root = root / "runs"
    records: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    if runs_root.is_dir() and not runs_root.is_symlink():
        for run_dir in sorted(runs_root.iterdir(), reverse=True):
            if run_dir.is_dir() and run_dir.name != "model-probes":
                record = _run_record(run_dir, runs_root)
                if record:
                    records.append(record)
        probe_dir = runs_root / "model-probes"
        if probe_dir.is_dir() and not probe_dir.is_symlink():
            for path in sorted(probe_dir.glob("model-probe-*.json"), reverse=True):
                probe = _json_file(path, runs_root)
                if not probe or probe.get("kind") != "provider_tool_call_smoke_test":
                    continue
                config = probe.get("config")
                config = config if isinstance(config, dict) else {}
                probes.append({
                    "probe_id": path.stem,
                    "status": (
                        probe["status"]
                        if probe.get("status") in ("passed", "failed", "interrupted")
                        else "unknown"
                    ),
                    "started_at": _text(probe.get("started_at"), 50),
                    "model": _text(config.get("model")),
                    "tool_calls_executed": _number(probe.get("tool_calls_executed")),
                    "evidence": f"runs/model-probes/{path.name}",
                })
    records.sort(key=_run_order, reverse=True)
    measured_agents = [
        record for record in records
        if record["category"] == "agent_evaluation"
        and record["evaluation_valid"] is True
        and record["strict_success"] is not None
    ]
    controls = [record for record in records if record["category"] == "environment_control"]
    groups: dict[str, dict[str, Any]] = {}
    for record in measured_agents:
        guidance = record["execution_guidance"]
        conditions = {
            key: record[key] for key in (
                "task", "task_label", "data_profile", "data_profile_label", "model",
                "max_output_tokens", "output_policy_version", "thinking_mode",
                "context_window_tokens", "budget_seconds", "sampling_params",
                "planner_profile", "planner_label", "security_profile",
            )
        }
        conditions["execution_guidance"] = {
            key: guidance[key] for key in ("profile", "label", "version", "sha256")
        }
        conditions["max_output_tokens_source"] = (
            "provider_default" if record["max_output_tokens_source"] == "provider_default"
            else "recorded_limit" if record["max_output_tokens"] is not None else "unrecorded"
        )
        key = json.dumps(conditions, sort_keys=True, ensure_ascii=False)
        if key not in groups:
            groups[key] = {**conditions, "measured_agent_runs": 0, "strict_agent_successes": 0}
        groups[key]["measured_agent_runs"] += 1
        groups[key]["strict_agent_successes"] += record["strict_success"] is True
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "read_only_results",
        "runs": records,
        "probes": probes,
        "agent_groups": list(groups.values()),
        "counts": {
            "erp_runs": len(records),
            "measured_agent_runs": len(measured_agents),
            "strict_agent_successes": sum(
                record["strict_success"] is True for record in measured_agents
            ),
            "environment_controls": len(controls),
            "expected_environment_controls": sum(
                record["evaluation_valid"] is True
                and record["expected_control_outcome"] is True
                for record in controls
            ),
        },
    }


def run_detail(workspace_root: str | Path, run_id: str) -> dict[str, Any] | None:
    if not RUN_ID.fullmatch(run_id):
        return None
    runs_root = Path(workspace_root).resolve() / "runs"
    run_dir = runs_root / run_id
    record = _run_record(run_dir, runs_root)
    if record is None:
        return None
    record["trace"] = _tool_trace(run_dir, runs_root, record)
    record["rules"] = []
    record["rules_truncated"] = False
    evidence_path = record["evidence"]["reward"]
    if evidence_path:
        payload = _json_file(runs_root.parent / evidence_path, runs_root)
        rules = payload.get("rules") if payload else None
        groups = rules.get("by_dimension") if isinstance(rules, dict) else None
        if isinstance(groups, dict):
            rows = [
                (dimension, row)
                for dimension, group in groups.items()
                if isinstance(group, list)
                for row in group
                if isinstance(row, dict)
            ]
            record["rules_truncated"] = len(rows) > 500
            record["rules"] = [
                {
                    "dimension": _text(dimension, 80),
                    "rule": _text(row.get("expr") or row.get("rule"), 300),
                    "status": row.get("status") if row.get("status") in ("PASS", "FAIL", "NA") else "UNKNOWN",
                    "applicable": _boolean(row.get("applicable")),
                }
                for dimension, row in rows[:500]
            ]
    return record


def _tool_trace(
    run_dir: Path, runs_root: Path, record: dict[str, Any]
) -> dict[str, Any]:
    if record["agent"] != "pi":
        return {"status": "not_applicable", "steps": []}
    # Final manifests are written after the runner's credential redaction.
    # Do not expose live files before that export/redaction boundary.
    if record["state"] in ("running", "preflight") or not record["finished_at"]:
        return {"status": "waiting", "steps": []}
    trial_dirs = [
        path for path in (run_dir / "harbor/job").glob("*")
        if path.is_dir() and not path.is_symlink()
        and ((path / "config.json").is_file() or (path / "result.json").is_file()
             or (path / "verifier").is_dir())
    ]
    if len(trial_dirs) != 1:
        return {"status": "unavailable", "steps": []}
    trace_path = trial_dirs[0] / "agent/pi.txt"
    if not _local_file(trace_path, runs_root):
        return {"status": "unavailable", "steps": []}
    try:
        with trace_path.open("rb") as source:
            raw = source.read(MAX_TRACE_BYTES + 1)
    except OSError:
        return {"status": "unavailable", "steps": []}
    truncated = len(raw) > MAX_TRACE_BYTES
    if truncated:
        raw = raw[:MAX_TRACE_BYTES].rsplit(b"\n", 1)[0]
    projection = parse_pi_events(raw.decode("utf-8", errors="replace"), truncated=truncated)
    projection["evidence"] = str(trace_path.relative_to(runs_root.parent))
    return projection


def make_server(workspace_root: str | Path, port: int = 8765) -> ThreadingHTTPServer:
    root = Path(workspace_root).resolve()
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Port must be an integer between 0 and 65535.")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: object) -> None:
            self._send(
                status,
                json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:
            allowed_hosts = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            if self.headers.get("Host") not in allowed_hosts:
                self._json(403, {"error": "Loopback host required."})
                return
            path = unquote(urlsplit(self.path).path)
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            if path in assets:
                filename, content_type = assets[path]
                self._send(200, (ASSETS / filename).read_bytes(), content_type)
            elif path == "/api/runs":
                self._json(200, build_snapshot(root))
            elif path.startswith("/api/runview/"):
                run_id = path.removeprefix("/api/runview/")
                view = None
                if RUN_ID.fullmatch(run_id):
                    view = build_run_view(root / "runs" / run_id, root / "runs")
                self._json(200 if view else 404, view or {"error": "Run view not found."})
            elif path.startswith("/api/runs/"):
                detail = run_detail(root, path.removeprefix("/api/runs/"))
                self._json(200 if detail else 404, detail or {"error": "Run not found."})
            else:
                self._json(404, {"error": "Not found."})

        def do_POST(self) -> None:
            self._json(405, {"error": "This viewer is read-only."})

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ForgeFlow local read-only results viewer")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    with make_server(args.workspace, args.port) as server:
        print(f"ForgeFlow 只读运行台：http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
