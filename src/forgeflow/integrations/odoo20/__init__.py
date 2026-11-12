"""Odoo 20 integration surface.

The resolver factory lives here, next to the adapter it wraps, so every service
entry point builds *the same* object resolution instead of each one re-reading
the connection variables.  A service without a configured ERP gets ``None``;
callers must report "no business connection" rather than invent a record id.
"""
from __future__ import annotations

import os
from typing import Mapping

from .adapter import Odoo20Adapter, Odoo20Config, Odoo20Error, Odoo20Unavailable
from .business import Odoo20BusinessService, PreparedCancellation
from .contracts import ContractCheck, OdooVersion
from .executor import ExecutionResult, Odoo20BusinessExecutor
from .resolver import BusinessObjectResolver
from .verifier import Odoo20BusinessVerifier, VerificationCheck, VerificationResult

__all__ = [
    "BusinessObjectResolver", "ContractCheck", "ExecutionResult", "Odoo20Adapter",
    "Odoo20BusinessExecutor", "Odoo20BusinessService", "Odoo20BusinessVerifier",
    "Odoo20Config", "Odoo20Error", "Odoo20Unavailable", "OdooVersion",
    "PreparedCancellation", "VerificationCheck", "VerificationResult",
    "adapter_from_env", "resolver_from_env",
]

#: Every value the ERP connection needs.  A partial set is not a connection.
ODOO20_ENV_KEYS = (
    "FORGEFLOW_ODOO20_URL",
    "FORGEFLOW_ODOO20_DB",
    "FORGEFLOW_ODOO20_LOGIN",
    "FORGEFLOW_ODOO20_PASSWORD",
)


def adapter_from_env(environ: Mapping[str, str] | None = None) -> Odoo20Adapter | None:
    """Build the ERP adapter from the environment, or ``None`` if unconfigured."""
    env = os.environ if environ is None else environ
    if not all(env.get(key) for key in ODOO20_ENV_KEYS):
        return None
    company = str(env.get("FORGEFLOW_ODOO20_COMPANY_ID") or "").strip()
    return Odoo20Adapter(Odoo20Config(
        env["FORGEFLOW_ODOO20_URL"], env["FORGEFLOW_ODOO20_DB"],
        env["FORGEFLOW_ODOO20_LOGIN"], env["FORGEFLOW_ODOO20_PASSWORD"],
        company_id=int(company) if company.isdigit() else 0))


def resolver_from_env(environ: Mapping[str, str] | None = None, *,
                      company_ids=None) -> BusinessObjectResolver | None:
    """The shared business-object resolver, or ``None`` without an ERP.

    ``company_ids`` narrows candidate records to the companies a tenant is
    allowed to see.  It is applied in the lookup itself, before any candidate
    reaches a model or a page.
    """
    adapter = adapter_from_env(environ)
    if adapter is None:
        return None
    return BusinessObjectResolver(adapter, company_ids=company_ids)
