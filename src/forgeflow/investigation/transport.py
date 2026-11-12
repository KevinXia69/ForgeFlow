"""Shared byte accounting for source pages, executor frames and CLI stdout."""

from __future__ import annotations

import json

from forgeflow.executor.protocol import MAX_FRAME_BYTES

# Reserve room for state, issues, observation IDs and both wire encodings.
# Final admission still measures the actual service response, not an estimate.
PAGE_BYTES = 48 * 1024
PAGINATION_VERSION = 2


def cli_response(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n"


def response_sizes(value):
    envelope = {"schema_version": 1, "ok": True, "op": "investigation", "investigation": value}
    wire = json.dumps(envelope, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False) + "\n"
    return {"executor_frame": len(wire.encode("utf-8")),
            "python_stdout": len(cli_response(value).encode("utf-8"))}


def response_fits(value):
    return all(size <= MAX_FRAME_BYTES for size in response_sizes(value).values())


def page_size(records, cursor):
    # Service IDs are SHA-256 hex. Include them even for baseline/source-only
    # consumers so every arm sees the same page boundaries.
    value = {"records": [{**record, "observation_id": "0" * 64} for record in records],
             "complete": cursor is None, "next_cursor": cursor}
    return len(cli_response(value).encode("utf-8"))
