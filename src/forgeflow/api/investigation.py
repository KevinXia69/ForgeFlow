"""Authenticated continuation routes over a host-configured case adapter.

The browser submits typed budget input, legacy reply text and confirmation bindings, never policy,
template, generation, actor, ERP credentials or product handoff payloads.
Scheduling is injected by the host and must durably claim its stable operation
before dispatch. Repeating an HTTP operation may repeat enqueue, not worker work.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from starlette.responses import FileResponse

from forgeflow.investigation.cases import CaseError, InvestigationCases
from forgeflow.investigation.clarification import ClarificationError
from forgeflow.investigation.intake import IntakeError

from .auth import require_principal


class Operation(BaseModel):
    model_config = {"extra": "forbid"}
    operation_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class RevisionOperation(Operation):
    expected_revision: int = Field(strict=True, ge=1)


class ReplyOperation(RevisionOperation):
    text: str = Field(min_length=1, max_length=8000)


class BudgetOperation(RevisionOperation):
    ticket_id: str = Field(strict=True, min_length=1, max_length=120)
    amount: str = Field(strict=True, min_length=1, max_length=64)


class BudgetControlOperation(RevisionOperation):
    action: Literal["withdraw", "pause", "resume"]


class ConfirmOperation(RevisionOperation):
    draft_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parent_policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: bool = Field(strict=True)


@dataclass
class InvestigationAdapter:
    cases: InvestigationCases
    create_case: Callable  # (principal, operation_id) -> public view; host owns parent scheduling
    schedule: Callable  # (action, case_id, principal, operation_id); durable/idempotent host dispatch
    background: Callable | None = None  # (case_id, principal) -> safe host status projection
    request_closure: Callable | None = None  # authenticated cancellation, including expired cases
    expire: Callable | None = None  # host verifies actual expiry; caller supplies no reason or clock
    reconcile_closure: Callable | None = None  # original receipts only, never replacement dispatch
    expire_pending: Callable | None = None  # host-owned expiry sweep; no model dispatch

    def detail(self, case_id, principal):
        view = self.cases.get(case_id, principal)
        status = self.background(case_id, principal) if self.background else {
            "status": "idle", "requires_reconciliation": False, "jobs": []}
        return {**view, "background": status}

    def require_available_background(self, case_id, principal):
        if self.detail(case_id, principal)["background"]["requires_reconciliation"]:
            raise CaseError("investigation_reconciliation_required", 409)

    def close(self, action, case_id, principal, **arguments):
        # Closing must remain available while ordinary work is blocked. Validate
        # ownership before selecting a host callback; never accept actor/reason,
        # generation, worker identity or a product receipt from the browser.
        self.cases.get(case_id, principal)
        callback = {"cancel": self.request_closure, "expired": self.expire,
                    "reconcile": self.reconcile_closure}[action]
        if callback is None:
            raise CaseError("investigation_closure_unavailable", 503)
        callback(case_id, principal, **arguments)
        return self.detail(case_id, principal)

    def create(self, principal, operation_id):
        view = self.create_case(principal, operation_id)
        return self.detail(view["case_id"], principal)

    def enqueue(self, action, case_id, principal, operation_id):
        try:
            self.schedule(action, case_id, principal, operation_id)
        except Exception as error:
            # The business operation may already be committed. Keep its stable
            # id so a caller can safely re-enqueue, never invent a second case.
            raise CaseError("investigation_schedule_unavailable", 503) from error


def install_investigation(app: FastAPI, adapter: InvestigationAdapter | None, settings):
    app.state.investigation_adapter = adapter

    if adapter is not None and adapter.expire_pending is not None and settings.deployment != "review":
        previous_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def with_expiry_sweep(application):
            async with previous_lifespan(application) as state:
                stop = Event()

                def sweep():
                    while not stop.wait(5):
                        try:
                            adapter.expire_pending()
                            application.state.investigation_expiry_sweep = "checked"
                        except Exception:
                            # A failed inspection never clears a claim. Keep raw
                            # host failures out of browser-facing diagnostics.
                            application.state.investigation_expiry_sweep = "reconciliation_required"

                worker = Thread(target=sweep, name="investigation-expiry", daemon=True)
                worker.start()
                try:
                    yield state
                finally:
                    stop.set()
                    await asyncio.to_thread(worker.join, 5)

        app.router.lifespan_context = with_expiry_sweep

    def available(*, mutation=False):
        if adapter is None:
            raise HTTPException(503, "investigation_unavailable")
        if mutation and settings.deployment == "review":
            raise HTTPException(403, "review_is_read_only")
        return adapter

    def invoke(callback):
        try:
            return callback()
        except CaseError as error:
            raise HTTPException(error.status, error.code) from None
        except ClarificationError as error:
            raise HTTPException(422, error.code) from None
        except IntakeError as error:
            raise HTTPException(409, str(error)) from None
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(409, "investigation_evidence_unavailable") from None

    @app.get("/api/investigation/config")
    def config(_principal: str = Depends(require_principal)):
        return {"available": adapter is not None,
                "can_create": adapter is not None and settings.deployment != "review",
                "can_close": adapter is not None and adapter.request_closure is not None
                    and settings.deployment != "review",
                "read_only": settings.deployment == "review"}

    @app.get("/api/investigation/cases")
    def list_cases(principal: str = Depends(require_principal)):
        return invoke(lambda: {"cases": available().cases.list(principal)})

    @app.post("/api/investigation/cases", status_code=201)
    def create(body: Operation, principal: str = Depends(require_principal)):
        return invoke(lambda: available(mutation=True).create(principal, body.operation_id))

    @app.get("/api/investigation/cases/{case_id}")
    def detail(case_id: str, principal: str = Depends(require_principal)):
        return invoke(lambda: available().detail(case_id, principal))

    @app.post("/api/investigation/cases/{case_id}/reply")
    def reply(case_id: str, body: ReplyOperation, principal: str = Depends(require_principal)):
        def run():
            host = available(mutation=True)
            host.require_available_background(case_id, principal)
            host.cases.submit_reply(case_id, principal, **body.model_dump())
            host.enqueue("reply", case_id, principal, body.operation_id)
            return host.detail(case_id, principal)
        return invoke(run)

    @app.post("/api/investigation/cases/{case_id}/confirm")
    def confirm(case_id: str, body: ConfirmOperation, principal: str = Depends(require_principal)):
        def run():
            host = available(mutation=True)
            host.require_available_background(case_id, principal)
            host.cases.confirm(case_id, principal, **body.model_dump())
            host.enqueue("child", case_id, principal, body.operation_id)
            return host.detail(case_id, principal)
        return invoke(run)

    @app.post("/api/investigation/cases/{case_id}/budget")
    def budget(case_id: str, body: BudgetOperation, principal: str = Depends(require_principal)):
        def run():
            host = available(mutation=True)
            host.require_available_background(case_id, principal)
            host.cases.submit_budget(case_id, principal, **body.model_dump())
            # Structured input only creates a candidate. It never dispatches a
            # parser, investigator or product operation.
            return host.detail(case_id, principal)
        return invoke(run)

    @app.post("/api/investigation/cases/{case_id}/budget-control")
    def budget_control(case_id: str, body: BudgetControlOperation,
                       principal: str = Depends(require_principal)):
        def run():
            host = available(mutation=True)
            host.require_available_background(case_id, principal)
            host.cases.budget_control(case_id, principal, **body.model_dump())
            return host.detail(case_id, principal)
        return invoke(run)

    @app.post("/api/investigation/cases/{case_id}/handoff")
    def handoff(case_id: str, body: RevisionOperation, principal: str = Depends(require_principal)):
        def run():
            host = available(mutation=True)
            host.require_available_background(case_id, principal)
            host.cases.handoff(case_id, principal, **body.model_dump())
            host.enqueue("handoff", case_id, principal, body.operation_id)
            return host.detail(case_id, principal)
        return invoke(run)

    @app.post("/api/investigation/cases/{case_id}/cancel")
    def cancel(case_id: str, body: RevisionOperation, principal: str = Depends(require_principal)):
        return invoke(lambda: available(mutation=True).close(
            "cancel", case_id, principal, **body.model_dump()))

    @app.post("/api/investigation/cases/{case_id}/close-expired")
    def close_expired(case_id: str, body: Operation, principal: str = Depends(require_principal)):
        # The stable closure identity is derived by the host from the expired
        # case. Repeating a client request only checks that original intent.
        return invoke(lambda: available(mutation=True).close("expired", case_id, principal))

    @app.post("/api/investigation/cases/{case_id}/close-reconcile")
    def reconcile_closure(case_id: str, body: Operation, principal: str = Depends(require_principal)):
        return invoke(lambda: available(mutation=True).close("reconcile", case_id, principal))

    @app.get("/investigation", include_in_schema=False)
    def investigation_page():
        return FileResponse(Path(__file__).with_name("static") / "investigation.html")
