"""Copy only owner-bound plan evidence from the privileged executor.

The API never receives filesystem access to the executor's store. Temporary
copies retain the existing semantic manifest checks in LocalProductExecutor.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .executor import ProductExecutionError

CHUNK_BYTES = 32 * 1024
MAX_BYTES = 16 * 1024 * 1024
REQUIRED = ("prepared.json", "manifest.json", "change-set.json", "plan.json",
            "request.json", "snapshot.json")
OPTIONAL = ("blueprint.json", "product-constraints.json", "product-costs.json")


def _fetch(executor, case, plan_id, name):
    data = bytearray()
    expected = None
    while True:
        response = executor._product(case, "artifact", {
            "owner_sha256": case["_owner_sha256"], "plan_id": plan_id,
            "name": name, "offset": len(data),
        })
        row = response.get("artifact") if isinstance(response, dict) else None
        if not isinstance(row, dict) or set(row) != {
            "name", "offset", "total_bytes", "sha256", "data_base64", "eof",
        }:
            raise ProductExecutionError("local_artifact_unavailable")
        size, sha = row["total_bytes"], row["sha256"]
        if (row["name"] != name or type(row["offset"]) is not int
                or row["offset"] != len(data) or type(size) is not int
                or not 0 <= size <= MAX_BYTES or size < len(data)
                or not isinstance(sha, str) or re.fullmatch(r"[a-f0-9]{64}", sha) is None
                or type(row["eof"]) is not bool
                or not isinstance(row["data_base64"], str)
                or len(row["data_base64"]) > 4 * ((CHUNK_BYTES + 2) // 3)):
            raise ProductExecutionError("local_artifact_unavailable")
        if expected is None:
            expected = (size, sha)
        elif expected != (size, sha):
            raise ProductExecutionError("local_artifact_unavailable")
        try:
            chunk = base64.b64decode(row["data_base64"], validate=True)
        except (ValueError, binascii.Error):
            raise ProductExecutionError("local_artifact_unavailable") from None
        if (len(chunk) != min(CHUNK_BYTES, size - len(data))
                or row["eof"] != (len(data) + len(chunk) == size)):
            raise ProductExecutionError("local_artifact_unavailable")
        data.extend(chunk)
        if row["eof"]:
            if hashlib.sha256(data).hexdigest() != sha:
                raise ProductExecutionError("local_artifact_unavailable")
            return bytes(data)


@contextmanager
def prepared_directory(executor, case, plan_id):
    if not executor.artifact_rpc:
        yield executor.store / plan_id
        return
    with tempfile.TemporaryDirectory(prefix="forgeflow-plan-") as temporary:
        directory = Path(temporary)
        for name in (*REQUIRED, *OPTIONAL):
            try:
                data = _fetch(executor, case, plan_id, name)
            except ProductExecutionError as error:
                if name in OPTIONAL and error.code == "artifact_missing":
                    continue
                raise
            (directory / name).write_bytes(data)
        yield directory
