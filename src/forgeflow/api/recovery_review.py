"""Local, authenticated review turns. Model text is never a policy or an execution request."""

from pathlib import Path

from fastapi import Depends, HTTPException
from pydantic import Field
from starlette.responses import FileResponse

from forgeflow.investigation.cases import CaseError
from forgeflow.product.service import ProductCaseError
from forgeflow.recovery.storage import StorageError

from .auth import require_principal
from .investigation import Operation


class RegisteredTurn(Operation):
    event_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    registration_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class EventQuestion(RegisteredTurn):
    text: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class RetryTurn(RegisteredTurn):
    previous: str = Field(min_length=1, max_length=120)


class VerifiedNotice(RetryTurn):
    notice_id: str = Field(min_length=1, max_length=120)
    purchase_line_id: int = Field(strict=True, gt=0)


class OrderQuestion(Operation):
    plan_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_version: int = Field(strict=True, ge=1)
    order_id: int = Field(strict=True, gt=0)
    text: str = Field(min_length=1, max_length=4000, pattern=r"\S")


def install_review(app, session):
    app.state.review_session = session

    def invoke(method, *args):
        try:
            return method(*args)
        except (CaseError, ProductCaseError) as error:
            raise HTTPException(error.status, error.code) from None
        except (ValueError, OSError, StorageError):
            raise HTTPException(409, "review_evidence_unavailable") from None

    @app.get("/recovery-review", include_in_schema=False)
    def page():
        return FileResponse(Path(__file__).with_name("static") / "recovery-review.html")

    @app.get("/api/recovery-review/session")
    def current(uid: str = Depends(require_principal)):
        return invoke(session.view, uid)

    @app.post("/api/recovery-review/session/investigate")
    def investigate(body: EventQuestion, uid: str = Depends(require_principal)):
        return invoke(session.submit, uid, {"kind": "investigate", **body.model_dump()})

    @app.post("/api/recovery-review/session/retry")
    def retry(body: RetryTurn, uid: str = Depends(require_principal)):
        return invoke(session.submit, uid, {"kind": "retry", **body.model_dump()})

    @app.post("/api/recovery-review/session/clarify")
    def clarify(body: VerifiedNotice, uid: str = Depends(require_principal)):
        return invoke(
            session.submit,
            uid,
            {
                "kind": "clarify",
                **body.model_dump(),
                "text": "根据已登记的供应商澄清重新调查。重新取得业务证据，再准备对应方案。",
            },
        )

    @app.post("/api/recovery-review/products/{case_id}/investigate")
    def investigate_order(case_id: str, body: OrderQuestion, uid: str = Depends(require_principal)):
        return invoke(
            session.submit, uid, {"kind": "fact", "case_id": case_id, **body.model_dump()}
        )

    @app.get("/api/recovery-review/products/{case_id}/investigate/{operation_id}")
    def result(case_id: str, operation_id: str, uid: str = Depends(require_principal)):
        reply = invoke(session.get, uid, operation_id)
        if reply["kind"] != "fact" or reply.get("product_case_id") != case_id:
            raise HTTPException(404, "review_operation_not_found")
        return reply
