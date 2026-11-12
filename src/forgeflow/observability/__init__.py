"""Cross-service identifiers for acceptance and audit records."""
from .trace import TRACE_HEADER, TraceContext, new_trace_id

__all__ = ["TRACE_HEADER", "TraceContext", "new_trace_id"]
