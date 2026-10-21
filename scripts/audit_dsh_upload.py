#!/usr/bin/env python3
"""Audit installed DSH upload modules without reading user logs or credentials.

Uses the existing runtime locator and generated business patch. Only resolved
module flags, package versions and source hashes are retained; no configuration
values, endpoints, request bodies, headers or session files are exported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import setup_dsh_harness as setup
import yaml

SOURCE_CHECKS = {
    "dsh-session-log-deepseek": (
        'enabled: z.boolean().default(true)',
        'register("dsh_session_log"',
        'if (!config.enabled.get()) return void 0;',
    ),
    "dsh-session-telemetry-otel": (
        'SessionTelemetryMode.FEEDBACK_ONLY',
        'mode === SessionTelemetryMode.DISABLED',
        'this.provider = void 0;',
        'ctx.on("feedback/committed"',
        'includeHistory: true',
    ),
    "dsh-command-feedback": (
        'ctx.plugin(SessionFeedbackService)',
        'name: "feedback"',
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Report exists; choose a new path to preserve evidence.")
    cli = setup._find_cli()
    if cli is None:
        raise SystemExit("Installed runtime not found.")
    packages = cli.parent.parent.parent
    source_facts = []
    for name, checks in SOURCE_CHECKS.items():
        package = packages / name
        metadata = json.loads((package / "package.json").read_text())
        source = (package / "lib/index.js").read_text()
        source_facts.append({
            "package": name, "version": metadata.get("version"),
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "reviewed_trigger_guards_present": all(check in source for check in checks),
        })
    with tempfile.TemporaryDirectory(prefix="forgeflow-upload-audit-") as temporary:
        setup.PATCH = Path(temporary) / "business.yml"
        setup._write_patch()
        result = subprocess.run(
            [os.environ.get("FORGEFLOW_DSH_NODE", "node"), str(cli),
             "--profile", "sdk", "--patch", str(setup.PATCH), "--dump-config"],
            env={**os.environ, "DSH_HOME": temporary}, capture_output=True,
            text=True, timeout=60,
        )
        if result.returncode:
            raise SystemExit("Runtime composition failed; no raw output exported.")
        # BaseLoader treats !!js as inert text. Never execute configuration tags.
        rows = yaml.load(result.stdout, Loader=yaml.BaseLoader)
        if not isinstance(rows, list):
            raise SystemExit("Unexpected composition format.")
        selected = {row.get("id"): row for row in rows if isinstance(row, dict)}
        disabled = {
            name: selected.get(name, {}).get("disabled") == "true"
            for name in setup.DISABLED_UPLOAD_MODULES
        }
        mode = selected.get("session-telemetry-otel", {}).get("config", {}).get("mode")
        payload = {
            "category": "installed_runtime_source_and_resolved_business_configuration",
            "source_facts": source_facts,
            "upload_modules_disabled": disabled,
            "telemetry_mode_disabled": mode == "DISABLED",
            "composition_checks_pass": not setup._composition_errors(result.stdout),
            "trigger_facts": {
                "provider_session_log": "official provider request extension; enabled by default upstream",
                "telemetry": "feedback session event or committed feedback; includes history in default feedback-only mode",
                "feedback_entry": "global command and feedback remote service",
                "disabled_mode": "retains service but does not construct export provider",
            },
            "private_logs_read": False, "model_called": False,
            "network_packet_capture": False,
        }
        payload["status"] = "passed" if (
            all(disabled.values()) and payload["telemetry_mode_disabled"]
            and payload["composition_checks_pass"]
            and all(row["reviewed_trigger_guards_present"] for row in source_facts)
        ) else "failed"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"status": payload["status"], "report": str(args.output)}))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
