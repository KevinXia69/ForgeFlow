"""Create-only local identities and real catalogue publication.

Odoo's separate shell initializer owns synthetic ERP records. This module never
approves a plan, executes an ERP operation, or repairs a user's changed roles.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from forgeflow.superagent.store import ControlStore


class BootstrapError(ValueError):
    """Stable local errors safe for installer output."""


def _settings(config: dict):
    from forgeflow.api.settings import create_settings

    return create_settings(config["env"])


def _principal(config: dict) -> str:
    from forgeflow.api.auth import _identity

    settings = _settings(config)
    if len(settings.invite_codes) != 1:
        raise BootstrapError("local_single_operator_invite_required")
    return _identity(settings, settings.invite_codes[0])


def _adapter(config: dict):
    from forgeflow.integrations.odoo20 import adapter_from_env

    adapter = adapter_from_env(config["env"])
    if adapter is None:
        raise BootstrapError("local_odoo_configuration_missing")
    return adapter


def _identity(config: dict, adapter) -> dict:
    if config["env"].get("FORGEFLOW_ODOO20_LOGIN") != config["operator"]["login"]:
        raise BootstrapError("local_operator_login_mismatch")
    uid = adapter._authenticate()
    companies = adapter.user_company_ids(uid)
    if len(companies) != 1:
        raise BootstrapError("local_operator_company_scope_invalid")
    if not adapter.check_contract().ok:
        raise BootstrapError("local_odoo_contract_incomplete")
    return {"installation_id": str(uuid.UUID(config["installation_id"])),
            "tenant_id": config["env"]["FORGEFLOW_CONTROL_TENANT_ID"],
            "principal_id": _principal(config), "odoo_user_id": uid,
            "company_id": int(companies[0])}


def initialize_control(store: ControlStore, config: dict, identity: dict) -> dict:
    """Claim one fresh database and retain every later user-controlled value."""
    with store.transaction() as cursor:
        cursor.execute("CREATE TABLE IF NOT EXISTS forgeflow_local_installation ("
                       "singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), "
                       "installation_id uuid NOT NULL, tenant_id text NOT NULL, "
                       "principal_id text NOT NULL, odoo_user_id integer NOT NULL, "
                       "company_id integer NOT NULL, reference_pack_sha256 text, "
                       "reference_complete boolean NOT NULL DEFAULT false, "
                       "created_at timestamptz NOT NULL DEFAULT now())")
        row = cursor.execute("SELECT * FROM forgeflow_local_installation "
                             "WHERE singleton FOR UPDATE").fetchone()
        if row is not None:
            if any(str(row[key]) != str(value) for key, value in identity.items()):
                raise BootstrapError("local_database_installation_mismatch")
            binding = cursor.execute(
                "SELECT tenant_id, odoo_user_id FROM control_odoo_bindings "
                "WHERE principal_id=%s", (identity["principal_id"],)).fetchone()
            if (binding is None or binding["tenant_id"] != identity["tenant_id"]
                    or binding["odoo_user_id"] != identity["odoo_user_id"]):
                raise BootstrapError("local_operator_binding_changed")
            return dict(row)
        if cursor.execute("SELECT 1 FROM control_tenants LIMIT 1").fetchone():
            raise BootstrapError("local_database_not_fresh")
        cursor.execute(
            "INSERT INTO control_tenants(id,display_name) VALUES(%s,%s)",
            (identity["tenant_id"], "Local synthetic workbench"))
        cursor.execute(
            "INSERT INTO control_principals(id,tenant_id,display_name,role) VALUES(%s,%s,%s,%s)",
            (config["env"].get("FORGEFLOW_SOURCE_PRINCIPAL", "source-service"),
             identity["tenant_id"], "Local source service", "planner"))
        # Keep this operation in the outer transaction. The binding helper uses
        # store.transaction(), so do not nest it: insert the same two rows here.
        # Its invariants are additionally checked by the existing lookup below.
        cursor.execute(
            "INSERT INTO control_odoo_bindings(principal_id,tenant_id,odoo_user_id,role) "
            "VALUES(%s,%s,%s,'admin')",
            (identity["principal_id"], identity["tenant_id"], identity["odoo_user_id"]))
        cursor.execute(
            "INSERT INTO control_principals(id,tenant_id,display_name,role) "
            "VALUES(%s,%s,%s,'admin')",
            (identity["principal_id"], identity["tenant_id"], config["operator"]["display_name"]))
        cursor.execute(
            "INSERT INTO control_source_subscriptions(id,tenant_id,source_type,enabled,"
            "interval_seconds,config) VALUES(%s,%s,'odoo20.purchase_order',false,60,'{}')",
            (uuid.uuid4(), identity["tenant_id"]))
        cursor.execute(
            "INSERT INTO forgeflow_local_installation(installation_id,tenant_id,principal_id,"
            "odoo_user_id,company_id) VALUES(%s,%s,%s,%s,%s)",
            tuple(identity[key] for key in (
                "installation_id", "tenant_id", "principal_id", "odoo_user_id", "company_id")))
        row = cursor.execute("SELECT * FROM forgeflow_local_installation WHERE singleton").fetchone()
    return dict(row)


def _reference_root() -> Path:
    from forgeflow.knowledge.reference_facts import default_reference_facts_path

    return default_reference_facts_path().parent


def bootstrap(config: dict, *, store=None, adapter=None, reference_root: Path | None = None,
              install_pack=None) -> dict:
    from forgeflow.knowledge.reference_pack import install_reference_pack, load_reference_pack
    from forgeflow.semantic import SemanticConfig

    owned = store is None
    store = store or ControlStore(config["env"]["FORGEFLOW_CONTROL_DATABASE_URL"])
    try:
        # The first bootstrap is the only writer before API/loops are started.
        # Serialize independent installer invocations too, including migration.
        store.connection.execute("SELECT pg_advisory_lock(716506390)")
        store.connection.commit()
        migrations = store.migrate()
        identity = _identity(config, adapter or _adapter(config))
        record = initialize_control(store, config, identity)
        root = reference_root or _reference_root()
        pack = load_reference_pack(root)
        pack_hash = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
        if record["reference_complete"]:
            if record["reference_pack_sha256"] != pack_hash:
                raise BootstrapError("local_reference_release_changed")
            # An operator may have withdrawn or changed a document. Restarting
            # must not restore it, reindex it, or spend embedding quota again.
            indexed = False
        else:
            result = (install_pack or install_reference_pack)(
                pack, tenant_id=identity["tenant_id"], store=store,
                config=SemanticConfig.from_env(config["env"]))
            if not result.get("ok") or not result.get("generation"):
                raise BootstrapError("local_reference_publication_incomplete")
            with store.transaction() as cursor:
                cursor.execute("UPDATE forgeflow_local_installation SET reference_complete=true, "
                               "reference_pack_sha256=%s WHERE singleton", (pack_hash,))
            indexed = True
        return {"ok": True, "mode": "local_synthetic_workbench",
                "migrations_applied": len(migrations), "reference_documents": len(pack.documents),
                "reference_indexed_this_run": indexed,
                "source_polling_default": "disabled_on_first_install",
                "automatic_approvals": False, "automatic_erp_execution": False}
    finally:
        store.connection.rollback()
        store.connection.execute("SELECT pg_advisory_unlock(716506390)")
        store.connection.commit()
        if owned:
            store.close()


def idle_status(config: dict, *, store=None) -> dict:
    """Require the local admission barrier as well as a drained work queue."""
    from .admission import paused

    admission_locked = paused(config)
    owned = store is None
    store = store or ControlStore(config["env"]["FORGEFLOW_CONTROL_DATABASE_URL"])
    try:
        row = store.connection.execute(
            "SELECT (SELECT count(*) FROM control_effects WHERE state IN ('pending','running',"
            "'reconcile_required')) AS effects, "
            "(SELECT count(*) FROM control_assignments WHERE state IN ('leased','running')) "
            "AS assignments, (SELECT count(*) FROM knowledge_parse_jobs "
            "WHERE state IN ('queued','processing')) AS parse_jobs").fetchone()
        counts = {key: int(value) for key, value in row.items()}
        return {"ok": admission_locked and not any(counts.values()), "read_only": True,
                "idle": not any(counts.values()), "counts": counts,
                "admission_locked": admission_locked, "api_ingress_must_be_stopped": True}
    finally:
        if owned:
            store.close()


def doctor(config: dict) -> dict:
    """Read-only operational checks; never submits a model, embedding or ERP write."""
    checks: dict[str, bool] = {}
    errors: dict[str, str] = {}

    def check(name, operation):
        try:
            checks[name] = bool(operation())
        except Exception as error:
            checks[name] = False
            errors[name] = type(error).__name__

    def control():
        store = ControlStore(config["env"]["FORGEFLOW_CONTROL_DATABASE_URL"])
        try:
            row = store.connection.execute(
                "SELECT installation_id,principal_id,reference_complete FROM "
                "forgeflow_local_installation WHERE singleton").fetchone()
            ok = bool(row and str(row["installation_id"]) == config["installation_id"]
                      and row["principal_id"] == _principal(config) and row["reference_complete"])
            rows = store.connection.execute(
                "SELECT DISTINCT kind FROM control_service_heartbeats WHERE "
                "(tenant_id=%s OR tenant_id IS NULL) AND last_seen>now()-interval '90 seconds'",
                (config["env"]["FORGEFLOW_CONTROL_TENANT_ID"],)).fetchall()
            checks["supervisor_heartbeats"] = {r["kind"] for r in rows} >= {
                "source", "dispatcher", "watchdog", "outbox"}
            return ok
        finally:
            store.close()

    def semantic():
        from forgeflow.semantic import SemanticConfig
        from forgeflow.semantic.qdrant import check_collection

        return bool(check_collection(SemanticConfig.from_env(config["env"])))

    def docmind():
        from forgeflow.knowledge.docmind import DocMindConfig

        return DocMindConfig.from_env(config["env"]) is not None

    def assistant():
        from forgeflow.api.dsh_harness import DshHarness

        harness = DshHarness.from_env()
        return harness is not None and harness.configured()

    check("control_bootstrap", control)
    check("odoo_native_contract", lambda: _adapter(config).check_contract().ok)
    check("qdrant_schema", semantic)
    check("docmind_configured", docmind)
    check("assistant_configured", assistant)
    return {"ok": all(checks.values()), "read_only": True, "checks": checks,
            "errors": errors, "billable_provider_requests": 0,
            "note": "Configuration checks do not verify provider credentials or a document parse."}
