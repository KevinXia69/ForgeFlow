"""Assertions shared by the disposable Docker acceptance driver and seam tests."""
from __future__ import annotations

import json
import subprocess
import time

GUARD = "FORGEFLOW_WORKBENCH_ISOLATED_TEST=1"
PROVIDER_READY = (
    "import socket,ssl,urllib.request; "
    "c=ssl.create_default_context(cafile='/etc/forgeflow/cert.pem'); "
    "assert urllib.request.urlopen('https://qdrant:8443/',context=c,timeout=2).read()"
    "==b'ci-fixture-not-a-model'; "
    "s=socket.create_connection(('qdrant',6333),timeout=2); "
    "c.wrap_socket(s,server_hostname='qdrant').close()"
)


def checked_json(raw: str, *, marker: str | None = None) -> dict:
    """Require explicit structured success; exit-zero alone is insufficient."""
    if marker:
        matches = [line.removeprefix(marker) for line in raw.splitlines() if line.startswith(marker)]
        if len(matches) != 1:
            raise ValueError("probe_marker_missing_or_ambiguous")
        raw = matches[0]
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise ValueError("probe_did_not_confirm_success")
    return value


def exercise_stack(run, result: dict, *, sleep=time.sleep) -> None:
    """Run against already started services. Injectable process seam holds no keys."""
    def stage(name):
        result["stage"] = name
        print(f"Local acceptance: {name}", flush=True)

    def runtime(command, *, service="supervisor", extra=()):
        return checked_json(run(["exec", "-T", service, "python", "-m",
                                "forgeflow.local_workbench.runtime", command, *extra], capture=True).stdout)

    def doctor():
        for attempt in range(30):
            try:
                value = runtime("doctor", service="api")
                if value.get("checks") and all(item is True for item in value["checks"].values()):
                    return value
            except (subprocess.CalledProcessError, ValueError):
                pass
            if attempt < 29:
                sleep(2)
        raise RuntimeError("runtime_doctor_failed")

    def probe(name):
        if name in {"seed", "state"}:
            command = ["exec", "-T", "-e", GUARD, "odoo", "odoo", "shell", "-c",
                       "/etc/forgeflow/odoo.conf", "--no-http"]
            raw = run(command, source=f"exec(open('/acceptance/{name}_probe.py').read())\n",
                      capture=True).stdout
        else:
            raw = run(["exec", "-T", "-e", GUARD, "api", "python",
                       f"/acceptance/workbench_{name}_probe.py"], capture=True).stdout
        value = checked_json(raw, marker=f"FORGEFLOW_WORKBENCH_{name.upper()}_PROBE=")
        if name in {"state", "control"}:
            assert value.get("read_only") is True
            tables = value.get("tables", {})
            assert len(tables) >= (24 if name == "state" else 11)
            assert all(isinstance(row.get("count"), int) and row["count"] >= 0
                       and isinstance(row.get("digest"), str) and len(row["digest"]) == 32
                       for row in tables.values())
        return value

    def repeated_bootstrap():
        value = checked_json(run(["run", "--rm", "--no-deps", "-T", "bootstrap"], capture=True).stdout)
        if (value.get("reference_indexed_this_run") is not False
                or value.get("reference_documents") != 12 or value.get("migrations_applied") != 0):
            raise ValueError("bootstrap_was_not_a_noop")

    stage("doctor_and_native_seed")
    result["doctor_before"] = doctor()
    seed = probe("seed")
    assert seed.get("real_odoo") is True and seed.get("repeat_seed_preserved_tables", 0) >= 20
    assert seed.get("foreign_installation_rejected") is True
    assert seed.get("synthetic_roles_and_business_verified") is True
    assert seed.get("recovery_approvals_created") == seed.get("recovery_executions_created") == 0
    result["native_seed"] = seed
    result["checks"]["native_seed_idempotent"] = True
    stage("http_reads_and_no_business_write")
    native_before, control_before = probe("state"), probe("control")
    result["http"] = probe("http")
    assert result["http"].get("documents") == 12 and result["http"].get("blocks", 0) >= 12
    assert result["http"].get("erp_mutation_requests") == result["http"].get("real_provider_requests") == 0
    assert result["http"].get("checks") and all(v is True for v in result["http"]["checks"].values())
    assert probe("state") == native_before and probe("control") == control_before
    result["checks"]["http_reads_preserve_native_and_catalog_state"] = True
    repeated_bootstrap()
    assert probe("state") == native_before and probe("control") == control_before
    result["checks"]["bootstrap_preserves_initial_data"] = True
    stage("explicit_synthetic_recovery")
    runtime("quiesce")
    idle = runtime("doctor", extra=("--require-idle",))
    assert idle.get("idle") is True and idle.get("admission_locked") is True
    result["recovery"] = probe("recovery")
    assert result["recovery"].get("checks") and all(v is True for v in result["recovery"]["checks"].values())
    assert result["recovery"].get("approved_plans") == 1 and result["recovery"].get("actions", 0) > 0
    assert result["recovery"].get("real_provider_requests") == 0
    result["checks"]["explicit_http_approval_execution_and_readback"] = True
    stage("safe_stop_and_resume")
    # Stop ingress before idle certification. Workers receive unlimited grace.
    control_before = probe("control")
    run(["stop", "-t", "-1", "api"])
    runtime("doctor", extra=("--require-idle",))
    native_before = probe("state")
    run(["stop", "-t", "-1", "supervisor", "docmind"])
    run(["up", "-d", "--wait", "--wait-timeout", "300", "api", "supervisor", "docmind"])
    runtime("resume-admission")
    result["doctor_after"] = doctor()
    assert probe("state") == native_before and probe("control") == control_before
    repeated_bootstrap()
    assert probe("state") == native_before and probe("control") == control_before
    result["http_after_restart"] = probe("http")
    assert result["http_after_restart"]["catalog_sha256"] == result["http"]["catalog_sha256"]
    assert probe("state") == native_before and probe("control") == control_before
    result["checks"]["stop_resume_and_bootstrap_preserve_data"] = True
    result["stage"] = "complete"
    result["ok"] = all(value is True for value in result["checks"].values())
