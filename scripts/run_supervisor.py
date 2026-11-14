#!/usr/bin/env python3
"""Run source, dispatcher and watchdog loops under one local supervisor."""
from __future__ import annotations

import json
import os
import signal
import time

from forgeflow.api.dsh_harness import DshHarness
from forgeflow.integrations.odoo20 import (
    Odoo20Adapter,
    Odoo20BusinessService,
    Odoo20Config,
    resolver_from_env,
)
from forgeflow.superagent import (
    CompositeToolBox,
    ControlStore,
    Dispatcher,
    DispatcherLoop,
    DshWorker,
    EffectWatchdog,
    HttpModelRunner,
    ModelRouterPolicy,
    OdooReadToolBox,
    OutboxConsumer,
    PiDockerRunner,
    PiWorker,
    PlanEffects,
    Role,
    Router,
    SourceService,
    Supervisor,
    TenantContext,
    ToolLoopWorker,
    WatchdogLoop,
    ensure_subscription,
)


def _knowledge_toolbox():
    """Register task-scoped knowledge tools when semantic retrieval is configured."""
    try:
        from pathlib import Path

        from forgeflow.semantic import SemanticConfig
        from forgeflow.superagent.store import ControlStore
        from forgeflow.superagent.tools import ScopedKnowledgeToolBox

        SemanticConfig.from_env()
        url = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
        if not url:
            return None
        store = ControlStore(url)
        root = Path(os.environ.get("FORGEFLOW_KNOWLEDGE_ROOT", ".")).resolve()
        return ScopedKnowledgeToolBox(store=store, root=root)
    except Exception as error:
        # A missing semantic configuration is expected; a broken setup must be
        # visible instead of silently shipping an investigation without tools.
        print(json.dumps({"knowledge_tools": "unavailable",
                          "error": type(error).__name__}), flush=True)
        return None


def _serve_until_signal(supervisor: Supervisor, *, poll_seconds: float = 0.2) -> None:
    """Keep signal handlers free of locks and join service threads normally."""
    stopping = False

    def request_stop(_number, _frame):
        nonlocal stopping
        stopping = True

    previous = {number: signal.signal(number, request_stop)
                for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        supervisor.start()
        while not stopping and not supervisor._stop.is_set():
            time.sleep(poll_seconds)
    finally:
        try:
            supervisor.stop()
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)


def _source_context(store, tenant: str, principal: str, *, seed_tenant_on_start: bool):
    if seed_tenant_on_start:
        store.seed_tenant(tenant, "Supervisor", 1,
                          ((principal, "Source Service", "planner"),))
    row = store.connection.execute(
        "SELECT t.policy_version,p.role FROM control_tenants t "
        "JOIN control_principals p ON p.tenant_id=t.id WHERE t.id=%s AND p.id=%s",
        (tenant, principal)).fetchone()
    store.connection.commit()
    if row is None or row["role"] != "planner":
        raise RuntimeError("source_principal_membership_unavailable")
    return TenantContext(tenant, principal, frozenset({Role.PLANNER}), int(row["policy_version"]))


def main(*, admission_wrapper=None, seed_tenant_on_start: bool = True) -> int:
    required = ("FORGEFLOW_ODOO20_URL", "FORGEFLOW_ODOO20_DB", "FORGEFLOW_ODOO20_LOGIN",
                "FORGEFLOW_ODOO20_PASSWORD", "FORGEFLOW_CONTROL_DATABASE_URL")
    if not all(os.environ.get(key) for key in required):
        print(json.dumps({"ok": False, "error": "credentials_required"}))
        return 2
    harness = DshHarness.from_env()
    harness_ready = harness is not None and harness.configured()
    runner = HttpModelRunner.from_env()
    adapter = None
    if runner is None and not harness_ready:
        print(json.dumps({"ok": False, "error": "worker_not_configured"}))
        return 2
    tenant = os.environ.get("FORGEFLOW_CONTROL_TENANT_ID", "forgeflow-project")
    principal = os.environ.get("FORGEFLOW_SOURCE_PRINCIPAL", "source-service")
    store = ControlStore(os.environ["FORGEFLOW_CONTROL_DATABASE_URL"])
    source_store = dispatcher_store = watchdog_store = outbox_store = None
    try:
        store.migrate()
        ctx = _source_context(store, tenant, principal, seed_tenant_on_start=seed_tenant_on_start)
        # Create-only: a restart must not re-enable a source an operator
        # disabled or reset the interval they chose.
        ensure_subscription(store, tenant_id=tenant,
                            source_type="odoo20.purchase_order",
                            interval_seconds=60)
        source_store = ControlStore(os.environ["FORGEFLOW_CONTROL_DATABASE_URL"])
        dispatcher_store = ControlStore(os.environ["FORGEFLOW_CONTROL_DATABASE_URL"])
        watchdog_store = ControlStore(os.environ["FORGEFLOW_CONTROL_DATABASE_URL"])
        outbox_store = ControlStore(os.environ["FORGEFLOW_CONTROL_DATABASE_URL"])
        adapter = Odoo20Adapter(Odoo20Config(
            os.environ["FORGEFLOW_ODOO20_URL"], os.environ["FORGEFLOW_ODOO20_DB"],
            os.environ["FORGEFLOW_ODOO20_LOGIN"], os.environ["FORGEFLOW_ODOO20_PASSWORD"]))
        # A configured deployment endpoint gives the Router a real model
        # decision; a missing configuration keeps the deterministic heuristic.
        from forgeflow.superagent.quotas import QuotaLimits, QuotaStore
        quota_store = QuotaStore(dispatcher_store, QuotaLimits.from_env())
        router = Router(policy=ModelRouterPolicy(runner) if runner is not None else None)
        if runner is not None:
            # Investigation reads the real ERP through an authorised tool; a
            # single-shot summariser cannot verify what it writes.  The same
            # quota store covers per-call tool/search work and wall/clock spend.
            boxes = [OdooReadToolBox(adapter, resolver=resolver_from_env())]
            knowledge = _knowledge_toolbox()
            if knowledge is not None:
                boxes.append(knowledge)
            router.register(ToolLoopWorker(
                "deepseek-investigator", {"assess"}, runner=runner, max_steps=4,
                tools=CompositeToolBox(boxes), quotas=quota_store))
        else:
            router.register(DshWorker("dsh", {"assess"},
                                      lambda prompt: harness.ask(prompt).answer))
        try:
            pi = PiDockerRunner.from_env()
            router.register(PiWorker("pi", {"research"}, pi.ask))
        except ValueError:
            pass
        dispatcher = Dispatcher(dispatcher_store, router, resolver=resolver_from_env(),
                                quotas=quota_store)
        # Planning, execution and verification are durable dispatcher effects, not
        # side effects of an HTTP request.
        PlanEffects(Odoo20BusinessService(adapter)).register(dispatcher)
        supervisor = Supervisor()
        wrap = admission_wrapper or (lambda _name, runnable: runnable)
        supervisor.register("source", wrap("source", SourceService(source_store, adapter, ctx)))
        supervisor.register("dispatcher", DispatcherLoop(
            dispatcher, "dispatcher-supervised", tenant_id=tenant, interval_seconds=2))
        supervisor.register("watchdog", WatchdogLoop(EffectWatchdog(watchdog_store),
                                                      interval_seconds=5))
        supervisor.register("outbox", wrap("outbox", OutboxConsumer(
            outbox_store, adapter, ctx, tick_seconds=5)))
        _serve_until_signal(supervisor)
        print(json.dumps({"ok": True, "status": supervisor.status()}, ensure_ascii=False))
        return 0
    finally:
        for connection_store in (source_store, dispatcher_store, watchdog_store,
                                 outbox_store, store):
            if connection_store is not None:
                connection_store.close()


if __name__ == "__main__":
    raise SystemExit(main())
