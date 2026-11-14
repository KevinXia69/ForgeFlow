"""Synthetic localhost backend for the browser logout acceptance test."""
from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from forgeflow.accounts import AccountStore
from forgeflow.api.app import create_app
from forgeflow.api.settings import create_settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--mode", choices=("invite", "account"), required=True)
    args = parser.parse_args()
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(args.root / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(args.root / "state"),
        "FORGEFLOW_SESSION_SECRET": "synthetic-browser-session-secret-00000001",
        "FORGEFLOW_INVITE_CODES": "synthetic-browser-invite-00000001,synthetic-other-invite-00000001",
        "FORGEFLOW_SECURE_COOKIE": "false",
    })
    accounts = None
    if args.mode == "account":
        accounts = AccountStore(args.root / "accounts.sqlite3")
        admin, _secret = accounts.bootstrap(email="admin@example.test", password="synthetic-admin-123")
        code = accounts.invite(admin_principal_id=admin.principal_id,
                               email="member@example.test", role="planner")
        accounts.activate(code=code, password="synthetic-member-123", display_name="Synthetic member")
        other = accounts.invite(admin_principal_id=admin.principal_id,
                                email="other@example.test", role="planner")
        accounts.activate(code=other, password="synthetic-other-123", display_name="Synthetic other")
    app = create_app(settings, accounts=accounts)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error", access_log=False)


if __name__ == "__main__":
    main()
