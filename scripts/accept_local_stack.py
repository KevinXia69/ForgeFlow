#!/usr/bin/env python3
"""Disposable real Docker/Odoo/PG/Qdrant acceptance with a named CI model fixture.

Validates installation, identity, reference publication, preservation and native
Odoo contracts. Embeddings/reranking are deterministic transport fixtures; this
is deliberately not advertised as real-provider answer quality acceptance.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path

import yaml
from local_stack_acceptance import (
    EXCEPTION_TYPES,
    PROVIDER_READY,
    collect_diagnostics,
    exercise_stack,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-image", required=True)
    parser.add_argument("--odoo-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    args.output.chmod(0o700)
    project = "forgeflow-install-test-" + uuid.uuid4().hex[:10]
    endpoint = os.environ.get("DOCKER_HOST", "")
    if os.environ.get("DOCKER_CONTEXT") or not endpoint:
        endpoint = subprocess.check_output(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], text=True).strip()
    if not endpoint.startswith(("unix://", "npipe://")):
        raise RuntimeError("disposable_acceptance_requires_local_docker")
    command = ["docker", "compose"]
    if subprocess.run([*command, "version"], capture_output=True).returncode:
        if not shutil.which("docker-compose"):
            raise RuntimeError("docker_compose_required")
        command = ["docker-compose"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = yaml.safe_load((ROOT / "deploy/workbench/compose.yaml").read_text())
    for name, service in config["services"].items():
        service.pop("ports", None)
        if name in ("setup", "api", "supervisor", "docmind", "bootstrap"):
            service["image"] = args.app_image
        if name in ("odoo", "odoo-init"):
            service["image"] = args.odoo_image
    config["services"]["api"]["ports"] = [f"127.0.0.1:{port}:8000"]
    config["services"]["test-provider"] = {
        "image": args.app_image, "user": "10001:10001", "read_only": True,
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
        "command": ["python", "/acceptance/provider.py"],
        "network_mode": "service:qdrant",
        "healthcheck": {"test": ["CMD", "python", "-c", PROVIDER_READY],
                        "interval": "2s", "timeout": "5s", "retries": 60},
        "depends_on": {"qdrant": {"condition": "service_started"}},
        "volumes": ["qdrant-config:/etc/forgeflow:ro",
                    f"{ROOT / 'tests/fixtures/workbench_embedding_provider.py'}:/acceptance/provider.py:ro"],
    }
    config["services"]["bootstrap"]["depends_on"]["test-provider"] = {"condition": "service_healthy"}
    # Helpers are mounted only into this process's unique disposable project.
    for service in ("odoo", "api"):
        config["services"][service]["volumes"] = list(config["services"][service]["volumes"])
    for name in ("seed", "state"):
        config["services"]["odoo"]["volumes"].append(
            f"{ROOT / f'tests/integrations/workbench_{name}_probe.py'}:/acceptance/{name}_probe.py:ro")
    for name in ("http", "control", "recovery"):
        config["services"]["api"]["volumes"].append(
            f"{ROOT / f'tests/integrations/workbench_{name}_probe.py'}:/acceptance/workbench_{name}_probe.py:ro")
    result = {"ok": False, "scope": "real_local_services_with_fixture_embeddings",
              "project": project, "real_provider_answers": "not_tested",
              "document_provider_parsing": "not_tested", "browser_ui": "not_tested",
              "stage": "initialize", "checks": {}}
    with tempfile.TemporaryDirectory(prefix="forgeflow-compose-") as temporary:
        compose_path = Path(temporary) / "compose.yaml"
        compose_path.write_text(yaml.safe_dump(config, sort_keys=False))
        compose = [*command, "-p", project, "-f", str(compose_path)]
        log_path = args.output / "containers.log"
        with log_path.open("w") as log:
            log_path.chmod(0o600)

            def run(arguments, *, source=None, capture=False, timeout=1200):
                try:
                    return subprocess.run([*compose, *arguments], input=source, text=True,
                                          stdout=subprocess.PIPE if capture else log,
                                          stderr=log, check=True, timeout=timeout)
                except subprocess.CalledProcessError as error:
                    if capture and error.stdout:
                        # Helpers emit bounded marker JSON; keep it private so
                        # the failing stage remains inspectable by the operator.
                        log.write(error.stdout)
                        log.flush()
                    raise

            try:
                print("Generating a new isolated configuration with synthetic CI keys", flush=True)
                result["stage"] = "generate_configuration"
                setup = """import json,os
from pathlib import Path
from forgeflow.local_workbench.config import save_configuration,atomic_write
value=save_configuration({'profile':'deepseek_flash','model_api_key':'ci-model-fixture-key','dashscope_api_key':'sk-ci-parser-fixture-key','siliconflow_api_key':'ci-embedding-fixture-key','display_name':'CI synthetic operator'})
value['env']['EMBEDDING_API_URL']='https://qdrant:8443/v1/embeddings'
value['env']['RERANKER_API_URL']='https://qdrant:8443/v1/rerank'
atomic_write(Path('/config/runtime.json'),json.dumps(value).encode())
"""
                run(["run", "--rm", "--no-deps", "-T", "setup", "python", "-"], source=setup)
                print("Starting real empty databases, Odoo addon and TLS Qdrant", flush=True)
                result["stage"] = "start_empty_stack"
                run(["up", "-d", "--wait", "--wait-timeout", "900"])
                result["checks"]["fresh_stack_healthy"] = True
                exercise_stack(run, result)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=10) as response:
                    assert response.status == 200 and json.load(response) == {"ok": True}
                result["checks"]["host_loopback_port"] = True
            except Exception as error:
                result["ok"] = False
                name = type(error).__name__
                result["error_type"] = name if name in EXCEPTION_TYPES else "UnexpectedError"
            finally:
                try:
                    subprocess.run([*compose, "logs", "--no-color", "--tail", "120"], stdout=log, stderr=log, timeout=60)
                except subprocess.TimeoutExpired:
                    result["container_log_capture_timed_out"] = True
                log.flush()
                # Only closed service/state/code enums enter the uploaded
                # receipt. The raw log remains private to this runner.
                result["diagnostics"] = collect_diagnostics(compose, log_path)
                # Only the unique project created above is removed; never use a
                # global Docker prune or touch the user's workbench volumes.
                try:
                    cleanup = subprocess.run([*compose, "down", "-v", "--remove-orphans"],
                                             stdout=log, stderr=log, timeout=120)
                    result["isolated_cleanup_ok"] = cleanup.returncode == 0
                except subprocess.TimeoutExpired:
                    result["isolated_cleanup_ok"] = False
                result["ok"] = result["ok"] and result["isolated_cleanup_ok"]
                receipt = args.output / "result.json"
                receipt.write_text(json.dumps(result, indent=2) + "\n")
                receipt.chmod(0o600)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
