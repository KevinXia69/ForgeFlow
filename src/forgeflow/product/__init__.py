"""Server-independent product approval slice for ForgeFlow.

The slice is deliberately independent of Docker, Odoo and any model provider:

* :mod:`forgeflow.product.state` owns the case/plan/approval state model;
* :mod:`forgeflow.product.executor` defines the executor protocol and a
  deterministic local fake;
* :mod:`forgeflow.product.service` persists cases and enforces approval
  semantics.

A real executor can be injected into :func:`forgeflow.api.app.create_app`
without changing the HTTP surface or the persisted CaseFile shape.
"""

from .executor import (
    FakeProductExecutor,
    ProductExecutionError,
    ProductExecutor,
    build_fake_plan,
)
from .service import (
    APPROVAL_TTL_SECONDS,
    ProductCaseError,
    ProductCaseNotFound,
    ProductCaseService,
)
from .state import (
    AWAITING_APPROVAL,
    AWAITING_SELECTION,
    CASE_ID,
    EXECUTING,
    EXPIRED,
    FAILED,
    PLAN_ID,
    PREPARING,
    PRODUCT_STATES,
    REJECTED,
    SCENARIOS,
    STATES,
    SUCCEEDED,
    TERMINAL_STATES,
    ProductCase,
    canonical_digest,
    normalize_execution,
    normalize_plan,
    utc_stamp,
)

__all__ = [
    "APPROVAL_TTL_SECONDS",
    "AWAITING_APPROVAL",
    "AWAITING_SELECTION",
    "CASE_ID",
    "EXECUTING",
    "EXPIRED",
    "FAILED",
    "PLAN_ID",
    "PREPARING",
    "PRODUCT_STATES",
    "REJECTED",
    "SCENARIOS",
    "STATES",
    "SUCCEEDED",
    "TERMINAL_STATES",
    "FakeProductExecutor",
    "ProductCase",
    "ProductCaseError",
    "ProductCaseNotFound",
    "ProductCaseService",
    "ProductExecutionError",
    "ProductExecutor",
    "build_fake_plan",
    "canonical_digest",
    "normalize_execution",
    "normalize_plan",
    "utc_stamp",
]
