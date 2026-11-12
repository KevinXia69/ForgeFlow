from forgeflow.observability import TRACE_HEADER, TraceContext, new_trace_id


def test_trace_id_is_stable_within_context_and_correlatable() -> None:
    trace = TraceContext(tenant_id="tenant-a", case_id="case-a")
    assert trace.trace_id == trace.trace_id
    assert len(trace.trace_id) == 32
    assert trace.headers() == {TRACE_HEADER: trace.trace_id}
    assert new_trace_id() != trace.trace_id
