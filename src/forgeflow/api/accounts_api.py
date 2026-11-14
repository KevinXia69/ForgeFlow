"""Account API: activation, login, MFA, revocation and member management.

The invite code is only an enrolment secret.  A browser session is a revocable
row; every authenticated request resolves it, so disabling a member or revoking a
session takes effect on the next call, not after a process restart.
"""
from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..accounts import SESSION_TTL_SECONDS, AccountError, AccountStore
from .auth import ACCOUNT_COOKIE, end_browser_session, require_principal

_STATUS = {
    "credentials_invalid": 401,
    "mfa_code_invalid": 401,
    "mfa_challenge_invalid": 401,
    "invite_invalid": 401,
    "invite_expired": 401,
    "reset_invalid": 401,
    "account_disabled": 403,
    "admin_required": 403,
    "mfa_enrollment_required": 409,
    "accounts_already_initialized": 409,
    "account_exists": 409,
    "too_many_attempts": 429,
    "account_missing": 404,
    "cannot_disable_self": 409,
    "bootstrap_disabled": 403,
    "bootstrap_token_invalid": 403,
}


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=512)


class MfaRequest(BaseModel):
    challenge: str = Field(min_length=8, max_length=256)
    code: str = Field(min_length=6, max_length=8)


class BootstrapRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=10, max_length=512)
    tenant_id: str = Field(default="default", min_length=1, max_length=80)
    display_name: str = Field(default="", max_length=120)


class InviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: str = Field(min_length=3, max_length=20)


class ActivateRequest(BaseModel):
    code: str = Field(min_length=8, max_length=256)
    password: str = Field(min_length=10, max_length=512)
    display_name: str = Field(default="", max_length=120)


class RoleRequest(BaseModel):
    role: str = Field(min_length=3, max_length=20)


class ResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class ResetCompleteRequest(BaseModel):
    token: str = Field(min_length=8, max_length=256)
    new_password: str = Field(min_length=10, max_length=512)


def _error(error: AccountError):
    raise HTTPException(status_code=_STATUS.get(error.code, 400),
                        detail=error.code) from None


def _set_cookie(response: Response, settings, token: str) -> None:
    response.set_cookie(
        ACCOUNT_COOKIE, token, max_age=SESSION_TTL_SECONDS, path="/",
        httponly=True, samesite="strict", secure=settings.secure_cookie)


def install_account_routes(app, store: AccountStore, settings) -> None:
    def _account(principal: str):
        account = store.account_for_principal(principal)
        if account is None or not account.active:
            raise HTTPException(status_code=401, detail="account_disabled")
        return account

    @app.post("/api/accounts/bootstrap", status_code=201)
    def bootstrap(body: BootstrapRequest) -> dict[str, Any]:
        configured = os.environ.get("FORGEFLOW_ACCOUNTS_BOOTSTRAP_TOKEN", "")
        if not configured:
            raise HTTPException(status_code=403, detail="bootstrap_disabled")
        if not hmac.compare_digest(configured, body.token):
            raise HTTPException(status_code=403, detail="bootstrap_token_invalid")
        try:
            account, secret = store.bootstrap(
                email=body.email, password=body.password,
                tenant_id=body.tenant_id, display_name=body.display_name)
        except AccountError as error:
            _error(error)
        return {"account": account.as_dict(), "mfa_secret": secret}

    @app.post("/api/accounts/login")
    def login(body: LoginRequest, response: Response,
              request: Request) -> dict[str, Any]:
        client = request.client.host if request.client else "unknown"
        try:
            result = store.login(email=body.email, password=body.password,
                                 client=client)
        except AccountError as error:
            _error(error)
        if result.get("mfa_required"):
            return {"mfa_required": True, "challenge": result["challenge"]}
        session = result["session"]
        _set_cookie(response, settings, session.token)
        return {"mfa_required": False, "account": session.account.as_dict(),
                "expires_at": session.expires_at}

    @app.post("/api/accounts/mfa")
    def verify_mfa(body: MfaRequest, response: Response,
                   request: Request) -> dict[str, Any]:
        client = request.client.host if request.client else "unknown"
        try:
            session = store.verify_mfa(challenge=body.challenge, code=body.code,
                                       client=client)
        except AccountError as error:
            _error(error)
        _set_cookie(response, settings, session.token)
        return {"mfa_required": False, "account": session.account.as_dict(),
                "expires_at": session.expires_at}

    @app.delete("/api/accounts/session", status_code=204)
    def logout(request: Request, response: Response) -> Response:
        return end_browser_session(request, response)

    @app.get("/api/accounts/me")
    def me(principal: str = Depends(require_principal)) -> dict[str, Any]:
        account = _account(principal)
        return {**account.as_dict(), "sessions": store.sessions(
            principal_id=principal)}

    @app.post("/api/accounts/activate", status_code=201)
    def activate(body: ActivateRequest) -> dict[str, Any]:
        try:
            account, secret = store.activate(
                code=body.code, password=body.password,
                display_name=body.display_name)
        except AccountError as error:
            _error(error)
        return {"account": account.as_dict(), "mfa_secret": secret}

    @app.post("/api/accounts/invites", status_code=201)
    def invite(body: InviteRequest,
               principal: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            code = store.invite(admin_principal_id=principal,
                                email=body.email, role=body.role)
        except AccountError as error:
            _error(error)
        return {"code": code, "email": body.email.lower(), "role": body.role}

    @app.get("/api/accounts/members")
    def members(principal: str = Depends(require_principal)) -> list[dict[str, Any]]:
        try:
            return store.members(admin_principal_id=principal)
        except AccountError as error:
            _error(error)

    @app.post("/api/accounts/members/{account_id}/role")
    def set_role(account_id: str, body: RoleRequest,
                 principal: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            return store.set_role(admin_principal_id=principal,
                                  account_id=account_id, role=body.role)
        except AccountError as error:
            _error(error)

    @app.post("/api/accounts/members/{account_id}/mfa")
    def issue_mfa(account_id: str,
                  principal: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            secret = store.issue_mfa_secret(admin_principal_id=principal,
                                            account_id=account_id)
        except AccountError as error:
            _error(error)
        return {"account_id": account_id, "mfa_secret": secret}

    @app.post("/api/accounts/members/{account_id}/disable")
    def disable(account_id: str,
                principal: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            revoked = store.disable(admin_principal_id=principal,
                                    account_id=account_id)
        except AccountError as error:
            _error(error)
        return {"account_id": account_id, "state": "disabled",
                "revoked_sessions": revoked}

    @app.post("/api/accounts/password/reset", status_code=201)
    def create_reset(body: ResetRequest,
                     principal: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            token = store.create_reset(admin_principal_id=principal,
                                       email=body.email)
        except AccountError as error:
            _error(error)
        return {"token": token}

    @app.post("/api/accounts/password/complete")
    def complete_reset(body: ResetCompleteRequest) -> dict[str, Any]:
        try:
            store.reset_password(token=body.token, password=body.new_password)
        except AccountError as error:
            _error(error)
        return {"ok": True}

    @app.get("/api/accounts/sessions")
    def sessions(principal: str = Depends(require_principal)) -> list[dict[str, Any]]:
        try:
            return store.sessions(principal_id=principal)
        except AccountError as error:
            _error(error)

    @app.post("/api/accounts/sessions/revoke")
    def revoke_sessions(principal: str = Depends(require_principal)) -> dict[str, Any]:
        try:
            revoked = store.revoke_account(principal)
        except AccountError as error:
            _error(error)
        return {"revoked_sessions": revoked}

    @app.get("/api/accounts/audit")
    def audit(principal: str = Depends(require_principal),
              limit: int = 100) -> list[dict[str, Any]]:
        try:
            return store.audit(admin_principal_id=principal, limit=limit)
        except AccountError as error:
            _error(error)
