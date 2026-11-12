"""Trusted host adapters for one managed Odoo sandbox, never model tools.

The caller supplies deployment paths and credentials. Read adapters expose only
fields_get/search/read; product executors receive the separate write key and
remain approval-gated. Starting/stopping an executor never resets the ERP.
"""
from __future__ import annotations

import getpass
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from forgeflow import runtime_lifecycle as lifecycle
from forgeflow.executor.main import read_key_text
from forgeflow.investigation.sources import OdooSource
from forgeflow.recovery.storage import atomic_json, digest, read_json

from .executor import ProductExecutionError
from .handoff import validate_handoff
from .local import LocalProductExecutor


def require(condition, reason):
    if not condition:
        raise ProductExecutionError(reason)


def relation(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        value = value[0]
    require(type(value) is int and value > 0, "currency_scope_unproven")
    return value


class ReadOnlyConnection:
    def __init__(self, connection):
        self.connection = connection

    def get_model(self, name):
        return ReadOnlyModel(self.connection.get_model(name))


class ReadOnlyModel:
    def __init__(self, model):
        self.model = model

    def __getattr__(self, method):
        require(method in {"fields_get", "search", "read"}, "read_only_method_required")
        return getattr(self.model, method)


class LocalReadConnection:
    """Keyword-only JSON-2 reads; never inherit a host's outbound proxy."""

    def __init__(self, config):
        require(config.host in {"127.0.0.1", "localhost"} and type(config.port) is int
                and 0 < config.port < 65536, "local_environment_required")
        self.url = f"http://{config.host}:{config.port}/json/2"
        self.headers = {"Authorization": "Bearer " + read_key_text(config.read_key),
                        "X-Odoo-Database": config.database}

    def get_model(self, name):
        require(isinstance(name, str) and re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)+", name),
                "read_only_model_required")
        return LocalReadModel(self, name)


class LocalReadModel:
    def __init__(self, connection, name):
        self.connection, self.name = connection, name

    def __getattr__(self, method):
        require(method in {"fields_get", "search", "read"}, "read_only_method_required")

        def invoke(**arguments):
            try:
                response = httpx.post(f"{self.connection.url}/{self.name}/{method}",
                    headers=self.connection.headers, json=arguments, timeout=60, trust_env=False)
                require(response.status_code == 200 and len(response.content) <= 16 * 1024 * 1024,
                        "environment_read_unavailable")
                return response.json()
            except (httpx.HTTPError, ValueError):
                raise ProductExecutionError("environment_read_unavailable") from None
        return invoke


def currency_evidence(connection, template):
    """Prove a single currency over selected orders and their supply closure.

    This is independent of budget amounts. Missing/mixed currency is unsupported,
    never defaulted to USD or inferred from a supplier price.
    """
    require(isinstance(template, dict) and isinstance(template.get("fields"), dict),
            "invalid_currency_template")
    fields = template["fields"]
    ids = fields.get("sales_order_ids")
    require(isinstance(ids, list) and ids and all(type(i) is int and i > 0 for i in ids)
            and len(set(ids)) == len(ids), "currency_scope_unproven")
    connection = ReadOnlyConnection(connection)

    def rows(model, names, *, identities=None, domain=None):
        api = connection.get_model(model)
        names = ["id", *names]
        schema = api.fields_get(allfields=names, attributes=["type", "relation"])
        require(isinstance(schema, dict) and set(names) <= set(schema), "currency_schema_unavailable")
        if identities is None:
            identities = api.search(domain=domain or [], limit=10000, order="id asc")
        require(isinstance(identities, list) and len(identities) < 10000
                and all(type(i) is int and i > 0 for i in identities)
                and len(set(identities)) == len(identities), "currency_scope_unproven")
        if not identities:
            return []
        result = api.read(ids=identities, fields=names)
        result = [result] if isinstance(result, dict) else result
        require(isinstance(result, list) and len(result) == len(identities)
                and all(isinstance(row, dict) and set(names) <= set(row) for row in result)
                and {row["id"] for row in result} == set(identities), "currency_scope_unproven")
        return result

    orders = rows("sale.order", ["company_id", "currency_id"], identities=ids)
    companies = {relation(row["company_id"]) for row in orders}
    require(len(companies) == 1, "mixed_company_unsupported")
    company_id = next(iter(companies))
    company = rows("res.company", ["currency_id"], identities=[company_id])[0]
    products = rows("product.product", ["product_tmpl_id", "default_code"],
                    domain=[("default_code", "=", fields["product_code"])])
    require(len(products) == 1, "currency_scope_unproven")
    target = products[0]
    boms = rows("mrp.bom", ["product_id", "product_tmpl_id"], domain=[
        ("active", "=", True), ("type", "=", "normal"),
        ("product_tmpl_id", "=", relation(target["product_tmpl_id"]))])
    boms = [row for row in boms if not row["product_id"] or relation(row["product_id"]) == target["id"]]
    component_ids = set()
    if boms:
        lines = rows("mrp.bom.line", ["product_id"], domain=[("bom_id", "in", [row["id"] for row in boms])])
        component_ids = {relation(row["product_id"]) for row in lines} - {target["id"]}
    if component_ids:
        products.extend(rows("product.product", ["product_tmpl_id", "default_code"], identities=sorted(component_ids)))
    product_ids = [row["id"] for row in products]
    templates = sorted({relation(row["product_tmpl_id"]) for row in products})
    po_lines = rows("purchase.order.line", ["order_id"], domain=[("product_id", "in", product_ids)])
    purchases = rows("purchase.order", ["company_id", "currency_id"],
                     identities=sorted({relation(row["order_id"]) for row in po_lines}))
    offers = rows("product.supplierinfo", ["company_id", "currency_id", "product_id"],
        domain=[("product_tmpl_id", "in", templates), "|", ("product_id", "=", False),
                ("product_id", "in", product_ids)])
    require(all(relation(row["company_id"]) == company_id for row in purchases)
            and all(not row["company_id"] or relation(row["company_id"]) == company_id for row in offers),
            "mixed_company_unsupported")
    currency_ids = {"res.company:" + str(company_id): relation(company["currency_id"])}
    for model, records in (("sale.order", orders), ("purchase.order", purchases), ("product.supplierinfo", offers)):
        currency_ids.update({f"{model}:{row['id']}": relation(row["currency_id"]) for row in records})
    currencies = rows("res.currency", ["name"], identities=sorted(set(currency_ids.values())))
    names = {row["id"]: row["name"] for row in currencies}
    require(all(isinstance(name, str) and re.fullmatch(r"[A-Z]{3}", name) for name in names.values())
            and len(set(names.values())) == 1, "single_currency_unproven")
    return {"currency": next(iter(names.values())), "company_id": company_id,
            "record_currencies": {key: names[value] for key, value in sorted(currency_ids.items())},
            "provenance": "Odoo read: company currency, selected sales orders, related purchase orders and supplier offers"}


@dataclass(frozen=True)
class EnvironmentConfig:
    lifecycle_file: Path
    container: str
    bundle: Path
    read_key: Path
    write_key: Path
    repository: Path
    docker_host: str
    host: str = "127.0.0.1"
    port: int = 8069
    database: str = "bench"
    agent_user: str = ""


class ManagedOdooEnvironment:
    def __init__(self, config: EnvironmentConfig):
        self.config = config
        self.env = dict(os.environ)
        for name in ("DOCKER_CONTEXT", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            self.env.pop(name, None)
        self.env["DOCKER_HOST"] = config.docker_host
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def generation(self):
        config = self.config
        record = lifecycle.read_record(config.lifecycle_file)
        require(isinstance(record, dict) and record.get("schema_version") == 1
                and record.get("uid") == os.getuid() and record.get("ready") is True
                and isinstance(record.get("instance"), str) and re.fullmatch(r"[a-f0-9]{32}", record["instance"])
                and record.get("config", {}).get("container") == config.container
                and record.get("config", {}).get("odoo_port") == config.port,
                "environment_generation_unproven")
        command = ["docker", "inspect", "--format", '{{json .Id}}|{{json .Image}}|{{json .State.Running}}|{{json .Config.Labels}}', config.container]
        try:
            result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
            import json
            identity, image, running, labels = [json.loads(part) for part in result.stdout.strip().split("|", 3)]
            require(result.returncode == 0 and running is True and identity == record.get("container_id")
                    and image == record.get("image_id") and isinstance(labels, dict)
                    and labels.get("io.forgeflow.instance") == record["instance"]
                    and labels.get("io.forgeflow.project") == record.get("project"), "environment_generation_changed")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise ProductExecutionError("environment_generation_unavailable") from None
        return digest({"instance": record["instance"], "container_id": identity,
                       "image_id": image, "database": config.database})

    def connection_factory(self):
        self.generation()
        return LocalReadConnection(self.config)

    def source_factory(self, policy):
        source_policy = getattr(policy, "source_policy", None)
        return OdooSource(self.connection_factory(), source_policy() if callable(source_policy) else policy)

    def currency_probe(self, template):
        return currency_evidence(self.connection_factory(), template)

    def guard_binding(self, binding):
        from forgeflow.contract import contract_from_payload
        from forgeflow.investigation.intake import task_template

        require(binding["generation"] == self.generation(), "environment_generation_changed")
        current = self.currency_probe(task_template(contract_from_payload(binding["contract"])))
        require(current == binding["currency"], "product_handoff_currency_changed")

    def executor_factory(self, binding, paths):
        binding = validate_handoff(binding, historical=True)
        require(binding["generation"] == self.generation(), "environment_generation_changed")
        config = self.config
        command = [sys.executable, "-B", "-m", "forgeflow.executor.main", "--socket", str(paths.socket),
            "--store", str(paths.store), "--bundle", str(config.bundle), "--key", str(config.write_key),
            "--agent-user", config.agent_user or getpass.getuser(), "--approval-store", str(paths.approvals),
            "--policy-file", str(paths.policy), "--product-verifier-container", config.container,
            "--database", config.database, "--host", config.host, "--port", str(config.port), "--allow-local-peer"]
        launch = {"schema_version": 1, "binding_sha256": digest(binding), "generation": binding["generation"],
                  "command_sha256": digest(command), "docker_host_sha256": digest(config.docker_host)}
        for path in (paths.store, paths.approvals):
            require(not path.is_symlink(), "product_handoff_store_untrusted")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with lifecycle.exclusive_lease(paths.directory / "executor-launch.lock"):
            launch_path = paths.directory / "executor-launch.json"
            if launch_path.exists():
                require(read_json(launch_path) == launch, "product_executor_configuration_changed")
            else:
                atomic_json(launch_path, launch)
            process_path = paths.directory / "executor-process.json"
            record, state = lifecycle.managed_process(process_path)
            require(state != "pid_reused", "product_executor_identity_changed")
            if state == "running":
                require(paths.socket.exists() and not paths.socket.is_symlink(), "product_executor_socket_unavailable")
                info = paths.socket.stat()
                require(stat.S_ISSOCK(info.st_mode) and record.get("socket_identity") == [info.st_dev, info.st_ino],
                        "product_executor_socket_changed")
            else:
                require(not paths.socket.exists() and not paths.socket.is_symlink(), "product_executor_stale_socket")
                with (paths.directory / "executor.log").open("ab") as log:
                    process = lifecycle.spawn_managed(process_path, command, "handoff-executor",
                        cwd=config.repository, env=self.env, stdout=log, stderr=subprocess.STDOUT)
                ready = False
                for _ in range(40):
                    if process.poll() is not None:
                        raise ProductExecutionError("product_executor_exited_before_ready")
                    if paths.socket.exists():
                        info = paths.socket.lstat()
                        require(stat.S_ISSOCK(info.st_mode), "product_executor_socket_changed")
                        record = lifecycle.read_record(process_path)
                        record["socket_identity"] = [info.st_dev, info.st_ino]
                        lifecycle.write_record(process_path, record)
                        ready = True
                        break
                    time.sleep(0.25)
                require(ready, "product_executor_not_ready")
        return LocalProductExecutor(socket_path=str(paths.socket), store=paths.store,
            approvals=paths.approvals, request=paths.request, policy=paths.policy,
            handoff_binding=binding, container=config.container, task=binding["contract"]["task_id"],
            binding_guard=self.guard_binding, enable_candidates="review_profile" in binding,
            review_connection=self.connection_factory if "review_profile" in binding else None)
