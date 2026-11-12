"""Real prepared child to independent product prepare, with case metering."""
import pytest
from test_investigation_handoff_integrity import prepared_case as prepared_case
from test_product_handoff import real_bundle as real_bundle
from test_product_handoff import wired as wired

from forgeflow.investigation.continuation import ContinuationCoordinator
from forgeflow.investigation.host import ContinuationHost
from forgeflow.recovery.storage import read_json


def host_for(cases, product):
    c = ContinuationCoordinator(cases, episode_runner=None, reply_interpreter=None)
    return ContinuationHost(cases=cases, coordinator=c, product=product, create_inputs=lambda _: {})


def handoff_job(cases, view, host):
    cases.handoff(view['case_id'], 'owner', operation_id='handoff', expected_revision=view['revision'])
    return host._job('handoff', view['case_id'], 'owner', 'handoff')


def test_real_child_request_reaches_product_fresh_prepare_and_case_counter(prepared_case, wired):
    cases, view, original_plan = prepared_case
    host = host_for(cases, wired.cases)
    job = handoff_job(cases, view, host)
    host._perform(job)
    after = cases.get(view['case_id'], 'owner')
    product = wired.cases.get(after['handoff']['product_case_id'], 'owner')
    assert after['status'] == 'product_review' and product['state'] == 'awaiting_approval'
    assert product['plan']['plan_id'] != original_plan.name
    paths = wired.registry.paths(cases.handoff_context(view['case_id'], 'owner')['binding'])
    assert read_json(original_plan / 'request.json') == read_json(paths.store / product['plan']['plan_id'] / 'request.json')
    assert after['usage']['prepares'] == 1 and after['usage']['tokens'] == 0
    assert product['approval'] is None and product['execution'] is None
    assert all(not conn.write_attempts for conn in wired.connections)
    host._perform(job)
    assert cases.get(view['case_id'], 'owner')['usage']['prepares'] == 1


def test_handoff_crash_after_product_prepare_recovers_same_receipt_and_plan(prepared_case, wired, monkeypatch):
    cases, view, _ = prepared_case
    host = host_for(cases, wired.cases)
    job = handoff_job(cases, view, host)
    original = cases.complete_handoff
    monkeypatch.setattr(cases, 'complete_handoff', lambda *a, **kw: (_ for _ in ()).throw(OSError('lost final response')))
    with pytest.raises(OSError):
        host._perform(job)
    first = wired.cases.get(job['binding']['product_case_id'], 'owner')
    monkeypatch.setattr(cases, 'complete_handoff', original)
    host._perform(job)
    second = wired.cases.get(job['binding']['product_case_id'], 'owner')
    assert first['plan']['plan_id'] == second['plan']['plan_id']
    assert len(wired.executors) == 1
    assert cases.get(view['case_id'], 'owner')['usage']['prepares'] == 1
