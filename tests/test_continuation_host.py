"""Persisted UI intent dispatch and recovery without replacement workers."""
import threading

import pytest
from test_continuation_worker import runner_for
from test_investigation_continuation import make_system

from forgeflow.investigation.cases import CaseError
from forgeflow.investigation.continuation import ContinuationCoordinator
from forgeflow.investigation.host import ContinuationHost
from forgeflow.recovery.storage import atomic_json, read_json


def setup(tmp_path, runner=None):
    system = make_system(tmp_path)
    calls = []
    coordinator = ContinuationCoordinator(system['cases'],
        episode_runner=runner or runner_for(system, calls), reply_interpreter=None)
    host = ContinuationHost(cases=system['cases'], coordinator=coordinator, product=None,
                            create_inputs=lambda principal: {})
    return system, host, calls


def wait(host, job):
    thread = host._threads[job['job_id']]
    thread.join(3)
    assert not thread.is_alive()
    return read_json(host.root / job['job_id'] / 'result.json')


def test_dispatch_original_parent_once_and_wait_without_worker(tmp_path):
    system, host, calls = setup(tmp_path)
    identity = system['view']['case_id']
    job = host.schedule('parent', identity, 'owner', 'create-1')
    assert wait(host, job)['status'] == 'complete'
    assert len(calls) == 1
    assert system['cases'].get(identity, 'owner')['status'] == 'awaiting_human'
    assert host.schedule('parent', identity, 'owner', 'create-1')['status'] == 'complete'
    assert len(calls) == 1
    assert not any(t.is_alive() for t in host._threads.values())


def test_host_does_not_dispatch_arbitrary_create_operation_or_wrong_owner(tmp_path):
    system, host, calls = setup(tmp_path)
    for owner, operation in [('other', 'create-1'), ('owner', 'invented')]:
        with pytest.raises(CaseError):
            host.schedule('parent', system['view']['case_id'], owner, operation)
    assert calls == []


def test_crash_with_result_before_case_finish_reconciles_without_new_provider(tmp_path, monkeypatch):
    system, host, calls = setup(tmp_path)
    original = system['cases'].finish_work
    monkeypatch.setattr(system['cases'], 'finish_work', lambda *a, **k: (_ for _ in ()).throw(OSError('crash')))
    job = host.schedule('parent', system['view']['case_id'], 'owner', 'create-1')
    assert wait(host, job)['status'] == 'reconciliation_required'
    monkeypatch.setattr(system['cases'], 'finish_work', original)
    assert host.reconcile(job['job_id'])['status'] == 'complete'
    assert len(calls) == 1


def test_host_restart_does_not_restart_unknown_original_worker(tmp_path):
    called = []
    def fail(**kwargs):
        called.append(1)
        raise RuntimeError('unknown')
    system, host, _ = setup(tmp_path, runner=fail)
    job = host.schedule('parent', system['view']['case_id'], 'owner', 'create-1')
    assert wait(host, job)['status'] == 'reconciliation_required'
    second = ContinuationHost(cases=system['cases'], coordinator=host.coordinator,
                              product=None, create_inputs=lambda _: {})
    assert second.schedule('parent', system['view']['case_id'], 'owner', 'create-1')['status'] == 'reconciliation_required'
    with pytest.raises(CaseError, match='worker_result_unknown'):
        second.reconcile(job['job_id'])
    assert called == [1]


def test_enqueue_binding_keeps_reply_identity_after_user_correction(tmp_path):
    system, host, _ = setup(tmp_path)
    identity = system['view']['case_id']
    wait(host, host.schedule('parent', identity, 'owner', 'create-1'))
    view = system['cases'].get(identity, 'owner')
    first = system['cases'].submit_reply(identity, 'owner', operation_id='first',
                                         expected_revision=view['revision'], text='800 USD')
    original_job = host._job('reply', identity, 'owner', 'first')
    view = system['cases'].get(identity, 'owner')
    system['cases'].submit_reply(identity, 'owner', operation_id='corrected',
                                 expected_revision=view['revision'], text='900 USD instead')
    assert host._job('reply', identity, 'owner', 'first') == original_job
    assert original_job['binding']['reply_id'] == first['reply_id']
    with pytest.raises(CaseError, match='reply_superseded'):
        host._perform(original_job)


def test_started_job_is_not_inferred_complete_from_released_lock(tmp_path):
    system, host, calls = setup(tmp_path)
    job = host._job('parent', system['view']['case_id'], 'owner', 'create-1')
    directory = host.root / job['job_id']
    directory.mkdir(parents=True)
    atomic_json(directory / 'intent.json', job)
    atomic_json(directory / 'started.json', {'phase': 'started'})
    result = host.schedule('parent', system['view']['case_id'], 'owner', 'create-1')
    assert result['status'] == 'reconciliation_required'
    assert calls == []
    # Coordinator writes claim before invoke. No claim proves that dispatch
    # has not started; recovery uses the already allocated same episode.
    assert host.reconcile(job['job_id'])['status'] == 'complete'
    assert len(calls) == 1


def test_parallel_schedule_does_not_duplicate_provider(tmp_path):
    started, release = threading.Event(), threading.Event()
    system = make_system(tmp_path)
    calls = []
    original = runner_for(system, calls)
    def run(**kwargs):
        started.set()
        assert release.wait(3)
        return original(**kwargs)
    c = ContinuationCoordinator(system['cases'], episode_runner=run, reply_interpreter=None)
    host = ContinuationHost(cases=system['cases'], coordinator=c, product=None, create_inputs=lambda _: {})
    first = host.schedule('parent', system['view']['case_id'], 'owner', 'create-1')
    assert started.wait(2)
    try:
        assert host.schedule('parent', system['view']['case_id'], 'owner', 'create-1')['status'] == 'running'
    finally:
        release.set()
    assert wait(host, first)['status'] == 'complete'
    assert len(calls) == 1


def test_known_cancellation_releases_environment_but_not_unknown_work(tmp_path):
    from test_investigation_continuation import waiting

    from forgeflow.investigation.intake import task_template
    system, host, _ = setup(tmp_path)
    view = waiting(system)
    cases, identity = system['cases'], view['case_id']
    reply = cases.submit_reply(identity, 'owner', operation_id='cancel', expected_revision=view['revision'], text='cancel')
    cases.propose(identity, 'owner', reply_id=reply['reply_id'], proposal={'kind': 'cancel'})
    receipt = cases.release_environment(identity, 'owner')
    other = cases.create('owner', operation_id='create-2', template=task_template(system['contract']),
                          policy=system['policy'], event=__import__('investigation_support').EVENT)
    assert other['case_id'] != identity
    assert cases.release_environment(identity, 'owner') == receipt
    assert read_json(cases.root / 'environment.json')['active_case'] == other['case_id']
    with pytest.raises(CaseError, match='case_environment_released'):
        cases.episode_config(identity, 'owner', view['current_episode'])


def test_release_crash_between_aggregate_and_environment_recovers_same_receipt(tmp_path, monkeypatch):
    from test_investigation_continuation import waiting

    import forgeflow.investigation.cases as module
    system, host, _ = setup(tmp_path)
    view = waiting(system)
    cases, identity = system['cases'], view['case_id']
    reply = cases.submit_reply(identity, 'owner', operation_id='cancel', expected_revision=view['revision'], text='cancel')
    cases.propose(identity, 'owner', reply_id=reply['reply_id'], proposal={'kind': 'cancel'})
    original = module.atomic_json
    def fail(path, value, **kwargs):
        if path.name == 'environment.json':
            raise OSError('crash before releasing claim')
        return original(path, value, **kwargs)
    monkeypatch.setattr(module, 'atomic_json', fail)
    with pytest.raises(OSError):
        cases.release_environment(identity, 'owner')
    assert read_json(cases.root / 'environment.json')['active_case'] == identity
    monkeypatch.setattr(module, 'atomic_json', original)
    cases.release_environment(identity, 'owner')
    assert read_json(cases.root / 'environment.json')['active_case'] is None


def test_fast_correction_waits_for_old_parser_usage_then_interprets_new_reply(tmp_path):
    system, host, _ = setup(tmp_path)
    cases, identity = system['cases'], system['view']['case_id']
    wait(host, host.schedule('parent', identity, 'owner', 'create-1'))
    started, release = threading.Event(), threading.Event()
    calls = []
    def interpret(**kwargs):
        text = kwargs['reply']['text']
        calls.append(text)
        if len(calls) == 1:
            started.set()
            assert release.wait(3)
        amount = '800.00' if text == '800 USD' else '900.00'
        return {'proposal': {'kind': 'budget_authorization_proposal', 'order_id': '30',
                             'amount': amount, 'currency': 'USD'},
                'usage': {'tokens': 100, 'seconds': 1, 'queries': 0, 'validations': 0, 'prepares': 0}}
    host.coordinator.reply_interpreter = interpret
    view = cases.get(identity, 'owner')
    cases.submit_reply(identity, 'owner', operation_id='first', expected_revision=view['revision'], text='800 USD')
    first = host.schedule('reply', identity, 'owner', 'first')
    assert started.wait(2)
    view = cases.get(identity, 'owner')
    cases.submit_reply(identity, 'owner', operation_id='correction', expected_revision=view['revision'], text='900 USD instead')
    second = host.schedule('reply', identity, 'owner', 'correction')
    release.set()
    assert wait(host, first)['status'] == 'superseded'
    assert wait(host, second)['status'] == 'complete'
    view = cases.get(identity, 'owner')
    assert view['draft']['proposal']['amount'] == '900.00'
    assert view['usage']['tokens'] == 200 and view['usage_complete']
    assert len(view['episodes']) == 1 and view['confirmation'] is None
    assert calls == ['800 USD', '900 USD instead']
