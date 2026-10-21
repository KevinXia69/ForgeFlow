"""Authenticated FastAPI surface over recorded runs and product approval cases."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, StreamingResponse

from ..product import (
    ProductCaseError,
    ProductCaseService,
    ProductExecutor,
)
from ..product.progress import HUB
from ..product.state import SCENARIOS
from ..runview import RUN_ID, build_run_view
from . import assistant
from .approvals import ApprovalService, ApprovalUnavailable
from .auth import (
    InvalidInviteCode,
    issue_cookie,
    optional_principal,
    require_principal,
    session_expiry,
    verify_cookie,
)
from .case_context import answer_context
from .case_draft import draft_view
from .case_qa import PLANNED_COST_CONTEXT, strip_case_reference
from .case_qa import answer_case as answer_case_question
from .dsh_harness import DshHarness, DshUnavailable
from .evidence import ReadOnlyEvidenceProjection, principal_view, public_evidence
from .model_settings import (
    ModelSettingsError,
    ModelSettingsStore,
    build_settings,
    platform_masked,
    platform_settings,
)
from .plan_diagnostics import read_traceability
from .plans import list_plans, read_plan
from .runs import RunService, RunUnavailable
from .settings import Settings
from .stream_hub import AssistantStreamHub, StreamCapacityError, TurnJournal
from .trust import is_trusted_api_request

logger = logging.getLogger(__name__)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
READ_ERRORS = (OSError, ValueError, TypeError, OverflowError)


class SessionRequest(BaseModel):
    invite_code: str


class CreateRunRequest(BaseModel):
    scenario: str
    idempotency_key: str


class KnowledgeRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=5, ge=1, le=10)
    lang: Literal["zh", "en"] = "zh"


class ModelRequest(BaseModel):
    profile: str = "custom"
    base_url: str
    model: str
    api_key: str


class AssistantTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=1000)


class AssistantRequest(KnowledgeRequest):
    model: ModelRequest | None = None
    history: list[AssistantTurn] = Field(default_factory=list, max_length=6)
    conversation_id: str | None = Field(default=None, max_length=64)
    turn_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    resume: bool = False


class StreamCancelRequest(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=64)
    turn_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class ModelTestRequest(BaseModel):
    model: ModelRequest | None = None


class ApprovalRequest(BaseModel):
    ttl_seconds: int = Field(default=900, ge=60, le=3600)


class ProductCaseRequest(BaseModel):
    scenario: str


class ProductDraftRequest(BaseModel):
    kind: str
    product_code: str
    units: int = Field(gt=0)
    due_days: int = Field(ge=0)


class ProductApproveRequest(BaseModel):
    plan_id: str
    comment: str = Field(default="", max_length=2000)


class ProductUnstartedRequest(BaseModel):
    model_config = {"extra": "forbid"}
    plan_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProductSelectionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    plan_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    comparison_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    selection_revision: int = Field(strict=True, ge=1, le=10000)


class ProductDecisionRequest(BaseModel):
    plan_id: str
    reason: str = Field(default="rejected", min_length=1, max_length=2000)
    changes: dict[str, Any] | None = None


class ProductReprepareRequest(BaseModel):
    model_config = {"extra": "forbid"}
    failed_version: int = Field(strict=True, ge=1, le=10000)
    reason: str = Field(min_length=1, max_length=2000)


class ProductAskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    conversation_id: str | None = Field(default=None, max_length=64)
    turn_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    resume: bool = False


class ModelSettingsRequest(BaseModel):
    profile: str
    base_url: str
    model: str
    api_key: str | None = None


def _case_question_prompt(case: dict[str, Any], question: str) -> str:
    """Give the read-only agent the exact public facts, not hidden state."""
    lang = "zh" if any("\u4e00" <= char <= "\u9fff" for char in question) else "en"
    from .case_context import case_context

    facts = case_context(case)
    language = "Chinese" if lang == "zh" else "English"
    return (
        "You are answering one question about the active ForgeFlow case. "
        f"Answer in {language}, in a human, nontechnical voice, with no more than "
        "two short paragraphs and at most two numbers. Use only the case facts below. "
        "Do not ask for approval, do not imply approval or execution happened unless "
        "the facts show it, and do not invent evidence. End with one natural next "
        "question. Put any case id only in a final short reference line. "
        "Candidate preferences do not prove global optimality. Selection is not approval. "
        "When selection_pending is true, the displayed plan is only the last confirmed "
        "choice; the saved request is not yet confirmed. Omitted context fields are "
        "unknown here, never evidence of absence, zero cost or no execution. "
        "product_selection_conflict means a choice was refused due to changed executor state; "
        "it is not a preparation failure and cannot establish whether another request changed ERP.\n\n"
        f"Cost field meaning: {PLANNED_COST_CONTEXT}\n\n"
        f"Case facts: {facts}\n\nQuestion: {question}"
    )

def _conversation_key(uid: str, conversation_id: str | None) -> str:
    """Scope one browser conversation to its authenticated owner."""
    return f"{uid}:{conversation_id or 'default'}"


def create_app(
    settings: Settings,
    *,
    spawn=None,
    product_executor: ProductExecutor | None = None,
) -> FastAPI:
    """Build the app; every data read stays below ``settings.data_root``.

    Reads remain available without an executor. Mutations require a local
    executor and owner-bound product state; recorded runs remain read-only.
    """
    app = FastAPI(
        title="ForgeFlow API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    approvals = ApprovalService(settings.approvals_root)
    runs = RunService(settings.data_root, settings.approvals_root / "runs.json",
                      spawn=spawn, enabled=settings.runner_enabled)
    model_settings = ModelSettingsStore(settings.approvals_root)
    platform_model = platform_settings()
    product_cases = ProductCaseService(settings.approvals_root, product_executor)
    app.state.product_cases = product_cases
    app.state.product_executor = product_executor
    dsh_harness = DshHarness.from_env()
    app.state.dsh_harness = dsh_harness
    assistant_hub = AssistantStreamHub(journal=TurnJournal(settings.approvals_root / "turn-journal"))
    evidence = ReadOnlyEvidenceProjection(settings, product_cases, runs, lambda: _executor_connection())
    scoped_harnesses: dict[str, Any] = {}
    owner_snapshots: dict[str, Path] = {}
    harness_lock = threading.RLock()

    def _harness_for(uid: str):
        with harness_lock:
            return _scoped_harness(uid)

    def _scoped_harness(uid: str):
        if dsh_harness is None or settings.deployment == "review":
            return None
        try:
            path = evidence.publish(uid)
        except (OSError, ValueError):
            return None
        scope = principal_view(settings, uid)["scope_id"]
        previous = owner_snapshots.get(scope)
        if previous is not None and previous != path:
            # Do not leave revoked objects readable by an older MCP process.
            assistant_hub.forget_scope(previous.stem + ":")
            previous.unlink(missing_ok=True)
            retired = scoped_harnesses.pop(str(previous), None)
            if retired is not None and hasattr(retired, "close"):
                try:
                    retired.close()
                except Exception:
                    logger.warning("retired assistant runtime failed to close")
        owner_snapshots[scope] = path
        if str(path) not in scoped_harnesses:
            if not hasattr(dsh_harness, "with_evidence"):
                return None
            try:
                scoped_harnesses[str(path)] = dsh_harness.with_evidence(path)
                scoped_harnesses[str(path)].evidence_scope_path = path
            except DshUnavailable:
                return None
        return scoped_harnesses[str(path)]

    def _scope_is_current(uid: str, harness) -> bool:
        try:
            return evidence.publish(uid) == harness.evidence_scope_path
        except (OSError, ValueError):
            return False

    def _scoped_stream(uid: str, harness, question: str, key: str, cancel_event=None):
        options = {"cancel_event": cancel_event} if getattr(harness, "supports_turn_cancellation", False) else {}
        for event in harness.stream(question, key, **options):
            if not _scope_is_current(uid, harness):
                _harness_for(uid)
                return
            yield event

    def _job_key(base: str, turn_id: str) -> str:
        return f"{base}:turn:{hashlib.sha256(turn_id.encode()).hexdigest()}"

    def _stream_response(base: str, body, request: Request, producer, metadata=None) -> StreamingResponse:
        if body.resume and not body.turn_id:
            raise HTTPException(status_code=400, detail="turn_id_required")
        digest = hashlib.sha256(body.question.encode()).hexdigest()
        key = _job_key(base, body.turn_id or f"legacy-{digest}")
        raw_last = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
        try:
            last = max(0, int(raw_last)) if raw_last else None
        except ValueError:
            last = None
        job = assistant_hub.get(key)
        if body.resume and job is None:
            raise HTTPException(status_code=410, detail="turn_unavailable")
        if job is not None:
            if job.cancelled:
                raise HTTPException(status_code=409, detail="turn_cancelled")
            if job.request_hash is not None and job.request_hash != digest:
                raise HTTPException(status_code=409, detail="turn_payload_mismatch")
        if job is None or (not body.turn_id and job.done and last is None and not body.resume):
            stopped = threading.Event()
            try:
                job = assistant_hub.start(key, lambda: producer(stopped),
                                          request_hash=digest, on_cancel=stopped.set,
                                          reuse_completed=bool(body.turn_id), metadata=metadata)
            except ValueError:
                raise HTTPException(status_code=409, detail="turn_payload_mismatch") from None
            except StreamCapacityError:
                raise HTTPException(status_code=503, detail="stream_capacity") from None
            if job.cancelled:
                raise HTTPException(status_code=409, detail="turn_cancelled")
            last = None
        return StreamingResponse(job.subscribe(last), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def _require_local_model_settings(
        _uid: str = Depends(require_principal),
    ) -> None:
        if settings.deployment == "review":
            raise HTTPException(status_code=403, detail="settings_read_only")

    def _executor_connection() -> dict[str, Any]:
        configured = product_executor is not None and settings.deployment != "review"
        if not configured:
            return {"configured": False, "connection": "unavailable", "ready": False, "erp_ready": None, "checked_at": None}
        if not bool(getattr(product_executor, "live", False)):
            return {"configured": True, "connection": "synthetic", "ready": True, "erp_ready": None, "checked_at": None}
        socket_path = getattr(product_executor, "socket_path", None)
        if not isinstance(socket_path, str):
            return {"configured": True, "connection": "unknown", "ready": None, "erp_ready": None, "checked_at": None}
        from ..recovery.client import ExecutorError, health
        try:
            result = health(socket_path, timeout=1.0)
            connected = result.get("ok") is True
        except ExecutorError:
            connected = False
        return {"configured": True, "connection": "connected" if connected else "unavailable",
                "ready": connected, "erp_ready": None, "checked_at": datetime.now(UTC).isoformat()}

    def _product_service(*, execute: bool = False) -> ProductCaseService:
        if execute and _executor_connection()["ready"] is not True:
            raise HTTPException(status_code=503, detail="product_execution_unavailable")
        return product_cases

    def _require_product_decision() -> ProductCaseService:
        if settings.deployment == "review":
            raise HTTPException(status_code=403, detail="review_read_only")
        return product_cases

    def _product_error(error: ProductCaseError) -> HTTPException:
        return HTTPException(status_code=error.status, detail=error.code)

    def _can_view_run(run_id: str, uid: str) -> bool:
        return evidence.can_view_run(run_id, uid)

    def _model_view() -> dict[str, Any]:
        stored = model_settings.load() if settings.deployment != "review" else None
        if stored is not None:
            view = {**model_settings.masked(), "source": "workspace", "can_override": True}
        elif platform_model is not None:
            view = platform_masked(platform_model)
        else:
            view = {
                "configured": False, "profile": None, "base_url": "", "model": "",
                "has_key": False, "updated_at": None,
                "source": "none", "can_override": True,
            }
        if settings.deployment == "review":
            view["read_only"] = True
        return view

    def _retrieved_answer(question: str, limit: int) -> dict[str, Any]:
        from ..knowledge.answer import answer as knowledge_answer_impl

        return knowledge_answer_impl(question, settings.knowledge_root, limit=limit)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.url.path.startswith("/api") and not is_trusted_api_request(request, settings):
            return JSONResponse({"detail": "untrusted request"}, status_code=403)
        try:
            response = await call_next(request)
        except Exception:  # convert unhandled failures into a headed JSON 500
            logger.exception("unhandled API error")
            response = JSONResponse({"detail": "internal server error"}, status_code=500)
        response.headers.update(SECURITY_HEADERS)
        return response


    @app.get("/api/config")
    def config(request: Request) -> dict[str, Any]:
        # Public boot fields (deployment/runner) stay available to the login
        # page; the native-shell credential URL is only for an authenticated
        # owner and never for review deployments.
        dsh_web_url = ""
        if (settings.deployment != "review" and optional_principal(request) is not None
                and os.environ.get("FORGEFLOW_DEVELOPER_TOOLS") == "1"):
            dsh_web_url = os.environ.get("FORGEFLOW_DSH_WEB_URL", "").strip()
            if not dsh_web_url:
                try:
                    dsh_web_url = Path(
                        os.environ.get("FORGEFLOW_DSH_WEB_URL_FILE", ".runtime/dsh-web-url")
                    ).read_text(encoding="utf-8").strip()
                except OSError:
                    dsh_web_url = ""
        return {
            "deployment": settings.deployment,
            "runner_enabled": settings.runner_enabled,
            "dsh_web_url": dsh_web_url,
        }

    @app.delete("/api/auth/session", status_code=204)
    def delete_session(response: Response, uid: str = Depends(require_principal)) -> Response:
        response.delete_cookie(settings.cookie_name, path="/", secure=settings.secure_cookie, httponly=True, samesite="Lax")
        response.status_code = 204
        return response

    @app.get("/api/auth/me")
    def auth_me(uid: str = Depends(require_principal)) -> dict[str, str]:
        return principal_view(settings, uid)

    @app.get("/api/sessions")
    def list_sessions(_uid: str = Depends(require_principal)) -> list[dict[str, Any]]:
        # Global native session caches are not an owner-scoped business API.
        return []

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/capabilities")
    def capabilities(_uid: str = Depends(require_principal)) -> dict[str, Any]:
        review = settings.deployment == "review"
        connection = _executor_connection()
        product_approval = not review and connection["ready"] is True
        live = product_approval and bool(getattr(product_executor, "live", False))
        if review:
            reason = "review_read_only"
        elif connection["ready"] is not True:
            reason = "product_execution_unavailable"
        else:
            reason = "synthetic_executor" if not live else "product_executor_available"
        return {
            "schema_version": 2,
            "deployment": settings.deployment,
            "principal": principal_view(settings, _uid),
            "case_read": True, "case_qa": True,
            "case_create": product_approval, "case_prepare": product_approval,
            "case_approve": product_approval, "product_prepare": product_approval,
            "product_execute": product_approval,
            "benchmark_create": settings.runner_enabled and not review,
            "developer_tools": False, "assistant_permissions": "read_only",
            "product_scenarios": [] if review else [
                {"id": name, "task": SCENARIOS[name]["task"],
                 "label": SCENARIOS[name]["label"], "can_create": product_approval}
                for name in product_cases.supported_scenarios()
            ],
            "environment": {"deployment": settings.deployment,
                "data_source": "synthetic_erp_bench",
                "scenario": getattr(product_executor, "scenario", None),
                "task": getattr(product_executor, "task", None),
                "execution": "live_sandbox" if live else "synthetic_executor" if product_approval else "unavailable",
                "evidence_time": None, **connection},
            "project_qa": True,
            "historical_review": True,
            "live_prepare": live,
            "live_execute": live,
            "product_approval": product_approval,
            "worker_available": False,
            "runner_enabled": settings.runner_enabled,
            "reason": reason,
        }

    @app.post("/api/auth/session", status_code=201)
    def create_session(
        body: SessionRequest, response: Response, request: Request,
    ) -> dict[str, Any]:
        issued_at = int(time.time())
        audience = request.headers.get("host") or request.url.netloc
        try:
            cookie = issue_cookie(
                settings, body.invite_code, now=issued_at, audience=audience,
            )
        except InvalidInviteCode:
            raise HTTPException(status_code=401, detail="invalid invite code") from None
        uid = verify_cookie(settings, cookie, now=issued_at, audience=audience)
        if uid is None:
            raise HTTPException(status_code=500, detail="session issuance failed")
        response.set_cookie(
            settings.cookie_name,
            cookie,
            max_age=settings.session_ttl_sec,
            path="/",
            httponly=True,
            samesite="Lax",
            secure=settings.secure_cookie,
        )
        return {"uid": uid, "expires_at": session_expiry(settings, now=issued_at)}

    @app.get("/api/runs")
    def list_runs(_uid: str = Depends(require_principal)) -> list[dict[str, Any]]:
        return evidence.list_runs(_uid)

    def _run_dir(run_id: str, uid: str):
        if RUN_ID.fullmatch(run_id) is None:
            raise HTTPException(status_code=400, detail="invalid run id")
        if not _can_view_run(run_id, uid):
            raise HTTPException(status_code=404, detail="run not found")
        run_dir = settings.data_root / run_id
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise HTTPException(status_code=404, detail="run not found")
        return run_dir

    @app.post("/api/knowledge/answer")
    def knowledge_answer(
        body: KnowledgeRequest, _uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        try:
            from ..knowledge.answer import answer as knowledge_answer_impl
        except ImportError:
            raise HTTPException(status_code=503, detail="knowledge_unavailable") from None
        try:
            return knowledge_answer_impl(
                body.question, settings.knowledge_root,
                limit=body.limit, locale=body.lang,
            )
        except (OSError, ValueError):
            raise HTTPException(status_code=503, detail="knowledge_unavailable") from None

    @app.get("/api/settings/model")
    def read_model_settings(_uid: str = Depends(require_principal)) -> dict[str, Any]:
        return _model_view()

    @app.put("/api/settings/model")
    def write_model_settings(
        body: ModelSettingsRequest,
        _principal: None = Depends(_require_local_model_settings),
    ) -> dict[str, Any]:
        try:
            model_settings.save(body.profile, body.base_url, body.model, body.api_key)
        except ModelSettingsError as error:
            raise HTTPException(status_code=400, detail=error.code) from None
        return _model_view()

    @app.delete("/api/settings/model", status_code=204)
    def delete_model_settings(
        _principal: None = Depends(_require_local_model_settings),
    ) -> Response:
        try:
            model_settings.clear()
        except ModelSettingsError as error:
            raise HTTPException(status_code=400, detail=error.code) from None
        return Response(status_code=204)

    @app.post("/api/settings/model/test")
    def test_model_settings(
        body: ModelTestRequest | None = None,
        _uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        configured = body.model if body is not None else None
        model = None
        if configured is not None:
            try:
                model = build_settings(
                    configured.profile, configured.base_url,
                    configured.model, configured.api_key,
                )
            except ModelSettingsError as error:
                raise HTTPException(status_code=400, detail=error.code) from None
        elif settings.deployment != "review":
            model = model_settings.load()
        if model is None or not model.api_key:
            model = platform_model
        if model is None or not model.api_key:
            raise HTTPException(status_code=400, detail="not_configured")
        started = time.monotonic()
        try:
            assistant.generate("ping", [], model)
        except assistant.AssistantError as error:
            return {"ok": False, "error": error.code}
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000)}

    @app.post("/api/assistant/stream")
    def assistant_stream(
        body: AssistantRequest,
        request: Request,
        uid: str = Depends(require_principal),
    ) -> StreamingResponse:
        """Live DSH thinking/tool trace plus the final answer as SSE.

        Browser-supplied model requests stay on the one-shot endpoint; this
        stream is specifically the local harness sidecar.
        """
        harness = _harness_for(uid)
        if harness is None or body.model is not None:
            raise HTTPException(status_code=503, detail="stream_unavailable")

        key = f"{harness.evidence_scope_path.stem}:{_conversation_key(uid, body.conversation_id)}"
        return _stream_response(key, body, request,
            lambda stopped: _scoped_stream(uid, harness, body.question, key, stopped),
            metadata={"owner": uid, "kind": "assistant", "turn_id": body.turn_id})

    @app.post("/api/assistant/cancel")
    def cancel_assistant(body: StreamCancelRequest, uid: str = Depends(require_principal)):
        harness = _harness_for(uid)
        if harness is None:
            raise HTTPException(status_code=404, detail="turn_unavailable")
        base = f"{harness.evidence_scope_path.stem}:{_conversation_key(uid, body.conversation_id)}"
        cancelled = assistant_hub.cancel(_job_key(base, body.turn_id))
        return {"cancelled": cancelled, "provider_interrupt_confirmed": False}

    @app.post("/api/assistant/answer")
    def assistant_answer(
        body: AssistantRequest, uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        harness = _harness_for(uid) if body.model is None else None
        if harness is not None:
            started = time.monotonic()
            try:
                reply = harness.ask(
                    body.question,
                    _conversation_key(uid, body.conversation_id),
                )
            except DshUnavailable:
                pass
            else:
                if not _scope_is_current(uid, harness):
                    _harness_for(uid)
                    raise HTTPException(status_code=409, detail="evidence_scope_changed")
                return {
                    "found": True,
                    "mode": "harness",
                    "answer": reply.answer,
                    "citations": reply.citations,
                    "passages": [],
                    "activity": reply.activity,
                    "scope": reply.scope,
                    "session_id": reply.session_id,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }
        transient = None
        if body.model is not None:
            try:
                transient = build_settings(
                    body.model.profile, body.model.base_url,
                    body.model.model, body.model.api_key,
                )
            except ModelSettingsError as error:
                raise HTTPException(status_code=400, detail=error.code) from None
        if transient is not None:
            try:
                return assistant.answer_with_settings(
                    body.question, settings.knowledge_root, transient,
                    limit=body.limit,
                    history=[turn.model_dump() for turn in body.history],
                    locale=body.lang,
                )
            except (ImportError, OSError, ValueError):
                raise HTTPException(
                    status_code=503, detail="knowledge_unavailable"
                ) from None
        if settings.deployment != "review":
            stored = model_settings.load()
            if stored is not None and stored.api_key:
                try:
                    return assistant.answer_with_settings(
                        body.question, settings.knowledge_root, stored,
                        limit=body.limit,
                        history=[turn.model_dump() for turn in body.history],
                        locale=body.lang,
                    )
                except (ImportError, OSError, ValueError):
                    raise HTTPException(
                        status_code=503, detail="knowledge_unavailable"
                    ) from None
        if platform_model is not None:
            try:
                return assistant.answer_with_settings(
                    body.question, settings.knowledge_root, platform_model,
                    limit=body.limit,
                    history=[turn.model_dump() for turn in body.history],
                    locale=body.lang,
                )
            except (ImportError, OSError, ValueError):
                raise HTTPException(
                    status_code=503, detail="knowledge_unavailable"
                ) from None
        try:
            retrieved = _retrieved_answer(body.question, body.limit)
        except (ImportError, OSError, ValueError):
            raise HTTPException(status_code=503, detail="knowledge_unavailable") from None
        return {**retrieved, "mode": "retrieval"}

    @app.get("/api/scenarios")
    def scenarios(_uid: str = Depends(require_principal)) -> list[dict[str, Any]]:
        return [
            {"id": "A", "task": "2280_medium_repair_plan_medium",
             "label": "供应商断供", "supported": True},
            {"id": "B", "task": "2299_hard_repair_plan_hard",
             "label": "产线停机", "supported": True},
        ]

    @app.get("/api/product/cases")
    def list_product_cases(
        uid: str = Depends(require_principal),
    ) -> list[dict[str, Any]]:
        return evidence.list_cases(uid)

    @app.post("/api/product/cases/draft")
    def preview_product_draft(body: ProductDraftRequest,
                              uid: str = Depends(require_principal)) -> dict[str, Any]:
        return draft_view(body.model_dump())

    @app.post("/api/product/cases/from-draft", status_code=201)
    def create_product_from_draft(body: ProductDraftRequest,
                                  uid: str = Depends(require_principal)) -> dict[str, Any]:
        preview = draft_view(body.model_dump())
        if not preview["valid"] or not preview["scenario"]:
            raise HTTPException(status_code=422, detail={"errors": preview["errors"]})
        service = _product_service(execute=True)
        try:
            created = service.create(uid, preview["scenario"])
            return public_evidence(service.prepare(created["case_id"], uid))
        except ProductCaseError as error:
            raise _product_error(error) from None

    @app.post("/api/product/cases", status_code=201)
    def create_product_case(
        body: ProductCaseRequest, uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        service = _product_service(execute=True)
        try:
            created = service.create(uid, body.scenario)
            return public_evidence(service.prepare(created["case_id"], uid))
        except ProductCaseError as error:
            raise _product_error(error) from None

    @app.post("/api/product/cases/{case_id}/prepare")
    def prepare_product_case(
        case_id: str, uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        service = _product_service(execute=True)
        try:
            return public_evidence(service.prepare(case_id, uid))
        except ProductCaseError as error:
            raise _product_error(error) from None

    @app.post("/api/product/cases/{case_id}/ask")
    def ask_product_case(
        case_id: str,
        body: ProductAskRequest,
        uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        case = evidence.get_case(case_id, uid)
        if case is None:
            raise HTTPException(status_code=404, detail="case_not_found")
        fallback = answer_case_question(body.question, case)
        fallback["case_context"] = answer_context(case)
        fallback["evidence_refs"] = [{"kind": "case", "id": case_id,
            "href": f"/api/product/cases/{case_id}", "label": "Case evidence"}]
        harness = _harness_for(uid)
        if harness is not None:
            try:
                reply = harness.ask(
                    _case_question_prompt(case, body.question),
                    f"case-{uid}:{case_id}:{body.conversation_id or 'default'}",
                )
                if not _scope_is_current(uid, harness):
                    _harness_for(uid)
                    raise HTTPException(status_code=409, detail="evidence_scope_changed")
                answer = strip_case_reference(reply.answer.strip(), case_id, "zh" if body.question and any("\u4e00" <= ch <= "\u9fff" for ch in body.question) else "en")
                if answer:
                    return {
                        **fallback,
                        "answer": answer,
                        "mode": "harness",
                        "citations": reply.citations or fallback.get("citations", []),
                    }
            except DshUnavailable:
                pass
        return fallback

    @app.post("/api/product/cases/{case_id}/ask/stream")
    def stream_product_case(case_id: str, body: ProductAskRequest, request: Request,
                            uid: str = Depends(require_principal)) -> StreamingResponse:
        case = evidence.get_case(case_id, uid)
        if case is None:
            raise HTTPException(status_code=404, detail="case_not_found")
        fallback = answer_case_question(body.question, case)
        fallback["case_context"] = answer_context(case)
        fallback["evidence_refs"] = [{"kind": "case", "id": case_id,
            "href": f"/api/product/cases/{case_id}", "label": "Case evidence"}]
        harness = _harness_for(uid)
        scope = harness.evidence_scope_path.stem if harness is not None else evidence.publish(uid).stem
        conversation = f"case-{uid}:{case_id}:{body.conversation_id or 'default'}"
        base = f"{scope}:{conversation}"

        def produce(stopped):
            if harness is None:
                yield {"type": "final", **fallback}
                return
            try:
                for event in _scoped_stream(uid, harness, _case_question_prompt(case, body.question),
                                             conversation, stopped):
                    if event.get("type") == "final":
                        final = {**fallback, **event, "evidence_refs": fallback["evidence_refs"],
                                 "case_context": fallback["case_context"]}
                        final["answer"] = strip_case_reference(str(final.get("answer") or ""), case_id,
                            "zh" if any("\u4e00" <= ch <= "\u9fff" for ch in body.question) else "en")
                        yield final
                    else:
                        yield event
            except DshUnavailable:
                if not stopped.is_set():
                    yield {"type": "error", "code": "stream_failed"}

        return _stream_response(base, body, request, produce,
            metadata={"owner": uid, "kind": "case", "case_id": case_id, "turn_id": body.turn_id})

    @app.post("/api/product/cases/{case_id}/ask/cancel")
    def cancel_product_question(case_id: str, body: StreamCancelRequest,
                                uid: str = Depends(require_principal)):
        if evidence.get_case(case_id, uid) is None:
            raise HTTPException(status_code=404, detail="case_not_found")
        harness = _harness_for(uid)
        scope = harness.evidence_scope_path.stem if harness is not None else evidence.publish(uid).stem
        base = f"{scope}:case-{uid}:{case_id}:{body.conversation_id or 'default'}"
        cancelled = assistant_hub.cancel(_job_key(base, body.turn_id))
        return {"cancelled": cancelled, "provider_interrupt_confirmed": False}

    @app.post("/api/product/cases/{case_id}/approve")
    def approve_product_case(
        case_id: str,
        body: ProductApproveRequest,
        uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        service = _product_service(execute=True)
        try:
            return public_evidence(service.approve(case_id, uid, body.plan_id, comment=body.comment))
        except ProductCaseError as error:
            raise _product_error(error) from None

    @app.post("/api/product/cases/{case_id}/select")
    def select_product_candidate(case_id: str, body: ProductSelectionRequest,
                                 uid: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            return public_evidence(_require_product_decision().select_candidate(
                case_id, uid, body.plan_id, comparison_id=body.comparison_id,
                selection_revision=body.selection_revision))
        except ProductCaseError as error:
            raise _product_error(error) from None

    @app.post("/api/product/cases/{case_id}/reject")
    def reject_product_case(case_id: str, body: ProductDecisionRequest,
                            uid: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            return public_evidence(_require_product_decision().reject(
                case_id, uid, body.reason, plan_id=body.plan_id))
        except ProductCaseError as error:
            raise _product_error(error) from None

    @app.post("/api/product/cases/{case_id}/revision")
    def revise_product_case(case_id: str, body: ProductDecisionRequest,
                            uid: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            return public_evidence(_require_product_decision().request_revision(
                case_id, uid, body.plan_id, reason=body.reason, changes=body.changes))
        except ProductCaseError as error:
            raise _product_error(error) from None

    @app.post("/api/product/cases/{case_id}/reprepare")
    def reprepare_product_case(case_id: str, body: ProductReprepareRequest,
                              uid: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            return public_evidence(_require_product_decision().reprepare(
                case_id, uid, body.failed_version, reason=body.reason))
        except ProductCaseError as error:
            raise _product_error(error) from None

    @app.post("/api/product/cases/{case_id}/reconcile")
    def reconcile_product_case(case_id: str, uid: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            return public_evidence(_require_product_decision().reconcile(case_id, uid))
        except ProductCaseError as error:
            raise _product_error(error) from None

    @app.post("/api/product/cases/{case_id}/cancel-unstarted")
    def cancel_unstarted_product_case(case_id: str, body: ProductUnstartedRequest,
                                     uid: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            return public_evidence(_require_product_decision().cancel_unstarted(case_id, uid, body.plan_id))
        except ProductCaseError as error:
            raise _product_error(error) from None

    @app.get("/api/product/cases/{case_id}/events")
    async def product_case_events(
        case_id: str, uid: str = Depends(require_principal),
    ) -> StreamingResponse:
        service = _product_service()
        if service.get(case_id, uid) is None:
            raise HTTPException(status_code=404, detail="case_not_found")

        async def generate():
            sent = 0
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                events = HUB.snapshot(case_id)
                for event in events[sent:]:
                    yield f"data: {json.dumps(event, ensure_ascii=True)}\n\n"
                    sent += 1
                if any(
                    event.get("phase") == "verify"
                    and event.get("status") in ("done", "failed")
                    for event in events
                ):
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.4)
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/product/cases/{case_id}")
    def product_case_detail(
        case_id: str, uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        case = evidence.get_case(case_id, uid)
        if case is None:
            raise HTTPException(status_code=404, detail="case_not_found")
        return case

    @app.post("/api/runs", status_code=202)
    def create_run(
        body: CreateRunRequest, uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        if settings.deployment == "review" or not settings.runner_enabled:
            raise HTTPException(status_code=403, detail="benchmark_creation_unavailable")
        try:
            return runs.create(uid, body.scenario, body.idempotency_key)
        except RunUnavailable as error:
            status = 503 if error.code == "runner_disabled" else 400
            raise HTTPException(status_code=status, detail=error.code) from None

    @app.get("/api/runs/{run_id:path}/events")
    def run_events(run_id: str, uid: str = Depends(require_principal)):
        """Server-sent snapshot stream: one event per change, then end."""
        run_dir = _run_dir(run_id, uid)

        def events():
            seen = None
            deadline = time.monotonic() + 30
            terminal = {"completed", "failed", "interrupted"}
            while True:
                try:
                    view = build_run_view(run_dir, settings.data_root)
                except READ_ERRORS:
                    view = None
                if view is not None:
                    payload = {
                        "run_id": view["run_id"],
                        "state": view.get("state"),
                        "score": view["evaluation"]["score"],
                        "evaluation_valid": view["evaluation"]["evaluation_valid"],
                        "attestation": view["attestation"]["status"],
                    }
                    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    if encoded != seen:
                        yield f"event: snapshot\ndata: {encoded}\n\n"
                        seen = encoded
                    if payload["state"] in terminal:
                        break
                if time.monotonic() > deadline:
                    break
                time.sleep(1)
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/runs/{run_id:path}/plans")
    def plans(run_id: str, uid: str = Depends(require_principal)) -> list[dict[str, Any]]:
        return list_plans(_run_dir(run_id, uid))

    @app.get("/api/runs/{run_id:path}/plans/{plan_id}")
    def plan_detail(
        run_id: str, plan_id: str, uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        plan = read_plan(_run_dir(run_id, uid), plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        return plan

    @app.get("/api/runs/{run_id:path}/plans/{plan_id}/traceability")
    def plan_traceability(
        run_id: str, plan_id: str, uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        result = read_traceability(_run_dir(run_id, uid), plan_id)
        if result is None:
            raise HTTPException(status_code=404, detail="plan not found")
        return result

    @app.post("/api/runs/{run_id:path}/plans/{plan_id}/approvals", status_code=201)
    def approve(
        run_id: str, plan_id: str, body: ApprovalRequest, uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        plan = read_plan(_run_dir(run_id, uid), plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        # Recorded runs are evidence, not an executable product case. This
        # endpoint cannot prove a current run/plan/policy/snapshot binding.
        raise HTTPException(status_code=409, detail="historical_evidence_read_only")

    @app.get("/api/runs/{run_id:path}/plans/{plan_id}/approvals")
    def list_approvals(
        run_id: str, plan_id: str, uid: str = Depends(require_principal),
    ) -> list[dict[str, Any]]:
        _run_dir(run_id, uid)
        try:
            return public_evidence(approvals.list(run_id=run_id, plan_id=plan_id))
        except ApprovalUnavailable as error:
            raise HTTPException(status_code=400, detail=error.code) from None

    @app.post("/api/runs/{run_id:path}/plans/{plan_id}/approvals/{nonce}/revoke")
    def revoke_approval(
        run_id: str, plan_id: str, nonce: str, uid: str = Depends(require_principal),
    ) -> dict[str, Any]:
        _run_dir(run_id, uid)
        raise HTTPException(status_code=409, detail="historical_evidence_read_only")

    @app.get("/api/runs/{run_id:path}")
    def run_detail(run_id: str, uid: str = Depends(require_principal)) -> dict[str, Any]:
        if RUN_ID.fullmatch(run_id) is None:
            raise HTTPException(status_code=400, detail="invalid run id")
        if not _can_view_run(run_id, uid):
            raise HTTPException(status_code=404, detail="run not found")
        try:
            view = evidence.get_run(run_id, uid)
        except READ_ERRORS:
            view = None
        if view is None:
            raise HTTPException(status_code=404, detail="run not found")
        return view

    try:
        from .web import mount_web

        mount_web(app, settings)
    except ImportError:
        pass
    return app
