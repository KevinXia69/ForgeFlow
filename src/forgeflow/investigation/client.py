"""Bounded CLI bridge for the Pi investigation extension; no direct ERP access."""
from __future__ import annotations

import argparse
import base64
import json
import os

from forgeflow.investigation.sources import SOURCE_ERROR_CODES, source_error_feedback
from forgeflow.investigation.transport import cli_response, response_fits
from forgeflow.recovery.client import ExecutorError, request

ACTIONS = ("query", "validate", "prepare", "status", "finish")


def call(socket_path: str, run_id: str, action: str, payload: dict, *, timeout=180):
    if action not in ACTIONS or not isinstance(payload, dict):
        raise ValueError("invalid_investigation_request")
    return request(socket_path, {"schema_version": 1, "op": "investigation",
                                "run_id": run_id, "action": action, "payload": payload},
                   timeout=timeout)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--payload-base64", required=True)
    args = parser.parse_args(argv)
    try:
        if len(args.payload_base64) > 80000:
            raise ValueError("payload_too_large")
        payload = json.loads(base64.b64decode(args.payload_base64, validate=True))
        result = call(os.environ.get("FORGEFLOW_EXECUTOR_SOCKET", ""),
                      os.environ.get("FORGEFLOW_RUN_ID", ""), args.action, payload)
        if not isinstance(result.get("investigation"), dict):
            raise ValueError("invalid_response")
        if not response_fits(result["investigation"]):
            print(json.dumps(source_error_feedback("query_response_too_large")))
            return 1
        print(cli_response(result["investigation"]), end="")
        return 0
    except ExecutorError as error:
        if (error.code == "invalid_field" and isinstance(error.reason, str)
                and error.reason in SOURCE_ERROR_CODES):
            print(json.dumps(source_error_feedback(error.reason)))
            return 1
        allowed = {"invalid_field", "unavailable", "internal_error", "bad_response",
                   "investigation_operations_disabled"}
        print(json.dumps({"status": "tool_error", "code": error.code if error.code in allowed
                          else "investigation_request_failed"}))
        return 1
    except (ValueError, TypeError, OSError):
        print(json.dumps({"status": "tool_error", "code": "invalid_investigation_request"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
