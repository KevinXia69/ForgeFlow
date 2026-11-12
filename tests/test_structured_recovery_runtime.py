"""Real runtime defaults and mode-bound jobs; provider/ERP boundaries are synthetic."""
import copy
import json
import sys

import pytest
from investigation_support import EVENT
from test_continuation_default_strategy import configured as _configured
from test_continuation_default_strategy import workbench

from forgeflow.investigation import runtime
from forgeflow.investigation.cases import CaseError
from forgeflow.investigation.service import InvestigationService
from forgeflow.recovery.storage import atomic_json, read_json


@pytest.fixture
def configured(tmp_path, monkeypatch):
    return _configured.__wrapped__(tmp_path, monkeypatch)


def awaiting_budget(host, environment):
    view = host.cases.create('owner', operation_id='create', **host.create_inputs('owner'))
    config = host.cases.episode_config(view['case_id'], 'owner', view['current_episode'])
    service = InvestigationService(source=environment.source_factory(config['policy']), contract=config['policy'],
        store=config['store'], bundle=config['store'] / 'unused', connection_factory=lambda: None, event=EVENT)
    service.dispatch(config['run_id'], 'finish', {'status': 'needs_clarification',
        'reason': 'Missing authorized budget', 'question': 'What budget do you authorize for SO-30?'})
    return host.cases.record_terminal(view['case_id'], 'owner', view['current_episode'])


def test_http_polling_reads_owner_snapshots_during_worker_admission(configured):
    from forgeflow.recovery.storage import scope_lock

    _, host, _, environment = runtime.build_runtime(**configured)
    view = awaiting_budget(host, environment)
    with scope_lock(host.cases.root):
        assert host.cases.get(view['case_id'], 'owner') == view
        assert host.cases.list('owner') == [view]
        assert host.cases.list('another-owner') == []


def test_omitted_mode_uses_structured_input_and_never_dispatches_reply(configured, monkeypatch):
    _, host, worker, environment = runtime.build_runtime(**configured)
    monkeypatch.setattr(worker, '_model', lambda **_: pytest.fail('Structured input cannot call provider'))
    view = awaiting_budget(host, environment)
    assert view['decision_input_mode'] == 'structured_budget_v1'
    assert view['decision_contract_version'] == 1
    binding = read_json(configured['root'] / 'runtime-binding.json')
    assert binding['decision_input_mode'] == view['decision_input_mode']
    assert binding['decision_implementation'] == runtime.structured_decision_binding()
    receipt = host.cases.submit_budget(view['case_id'], 'owner', operation_id='amount',
        expected_revision=view['revision'], ticket_id=view['ticket']['ticket_id'], amount='800')
    current = host.cases.get(view['case_id'], 'owner')
    assert current['replies'] == [] and current['confirmation'] is None
    assert current['draft']['input_id'] == receipt['input_id']
    assert current['draft']['proposal']['amount'] == '800.00'
    assert current['usage']['tokens'] == 0
    with pytest.raises(CaseError, match='structured_budget_input_required'):
        host._job('reply', view['case_id'], 'owner', 'amount')
    with pytest.raises(CaseError, match='structured_budget_input_required'):
        host.coordinator.interpret_reply(view['case_id'], 'owner', receipt['input_id'])
    assert not list(host.cases._directory(view['case_id']).glob('workers/interpret-*'))


def test_structured_child_job_binds_mode_before_any_dispatch(configured, monkeypatch):
    _, host, worker, environment = runtime.build_runtime(**configured)
    view = awaiting_budget(host, environment)
    host.cases.submit_budget(view['case_id'], 'owner', operation_id='amount',
        expected_revision=view['revision'], ticket_id=view['ticket']['ticket_id'], amount='800.00')
    view = host.cases.get(view['case_id'], 'owner')
    host.cases.confirm(view['case_id'], 'owner', operation_id='confirm', expected_revision=view['revision'],
        draft_sha256=view['draft']['draft_sha256'], parent_policy_sha256=view['draft']['parent_policy_sha256'],
        confirmed=True)
    job = host._job('child', view['case_id'], 'owner', 'confirm')
    assert job['decision_input_mode'] == 'structured_budget_v1' and job['decision_contract_version'] == 1
    monkeypatch.setattr(host.coordinator, 'run_episode', lambda *_: pytest.fail('Mode mismatch before dispatch'))
    for key, value in [('decision_input_mode', 'legacy_reply'), ('decision_contract_version', 2)]:
        bad = copy.deepcopy(job)
        bad[key] = value
        with pytest.raises(CaseError, match='host_job_binding_changed'):
            host._perform(bad)


def test_runtime_cannot_silently_change_input_mode_or_semantics(configured, monkeypatch):
    runtime.build_runtime(**configured)
    path = configured['root'] / 'runtime-binding.json'
    raw = path.read_bytes()
    with pytest.raises(ValueError, match='continuation_runtime_binding_changed'):
        runtime.build_runtime(**configured, decision_input_mode='legacy_reply')
    identity = runtime.structured_decision_binding()
    identity['source_sha256']['investigation/cases.py'] = '0' * 64
    monkeypatch.setattr(runtime, 'structured_decision_binding', lambda: identity)
    with pytest.raises(ValueError, match='continuation_runtime_binding_changed'):
        runtime.build_runtime(**configured)
    assert path.read_bytes() == raw


def test_original_v2_runtime_requires_its_original_interpreter(configured):
    runtime.build_runtime(**configured, decision_input_mode='legacy_reply')
    path = configured['root'] / 'runtime-binding.json'
    binding = read_json(path)
    assert 'decision_input_mode' not in binding
    del binding['reply_semantics']
    atomic_json(path, binding, replace=True)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='continuation_runtime_binding_changed'):
        runtime.build_runtime(**configured, decision_input_mode='legacy_reply')
    assert path.read_bytes() == before


@pytest.mark.parametrize('mode', [None, 'legacy_reply'])
def test_workbench_passes_explicit_mode_and_new_default(configured, tmp_path, monkeypatch, mode):
    environment = configured['environment_config']
    contract_path, event_path = tmp_path / 'contract.json', tmp_path / 'event.json'
    atomic_json(contract_path, configured['original_contract'])
    atomic_json(event_path, EVENT)
    config = {'root': str(configured['root']), 'socket_root': str(configured['socket_root']),
        'original_contract': str(contract_path), 'event': str(event_path), 'missing_order_id': 30,
        'environment': {name: str(getattr(environment, name)) for name in
            ('lifecycle_file', 'bundle', 'read_key', 'write_key', 'repository', 'container', 'docker_host')}}
    if mode:
        config['decision_input_mode'] = mode
    path = tmp_path / 'host.json'
    atomic_json(path, config)
    monkeypatch.setattr(workbench, 'create_settings', lambda: configured['settings'])
    served = []
    monkeypatch.setattr(workbench.uvicorn, 'run', lambda app, **_: served.append(app))
    monkeypatch.setattr(sys, 'argv', ['continuation_workbench.py', '--config', str(path)])
    workbench.main()
    assert served[0].state.continuation_host.create_inputs('owner')['decision_input_mode'] == (mode or 'structured_budget_v1')
    binding = json.loads((configured['root'] / 'runtime-binding.json').read_text())
    assert binding.get('decision_input_mode', 'legacy_reply') == (mode or 'structured_budget_v1')
