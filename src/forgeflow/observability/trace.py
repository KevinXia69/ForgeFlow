"""Small, dependency-free trace identifiers shared by control and tool calls."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

TRACE_HEADER = "X-ForgeFlow-Trace"


def new_trace_id() -> str:
    return secrets.token_hex(16)


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Identifiers that may be correlated without carrying secrets or prompts."""

    trace_id: str = field(default_factory=new_trace_id)
    tenant_id: str | None = None
    case_id: str | None = None
    task_id: str | None = None
    assignment_id: str | None = None
    operation_id: str | None = None

    def headers(self) -> dict[str, str]:
        return {TRACE_HEADER: self.trace_id}
