"""Legacy reply strategy selection and immutable restart bindings, without ERP or model calls."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from investigation_support import EVENT, make_case

from forgeflow.api.settings import create_settings
from forgeflow.investigation import runtime
from forgeflow.investigation.continuation import DEFAULT_REPLY_ALLOWANCE
from forgeflow.investigation.policy import PartialPolicy
from forgeflow.investigation.service import InvestigationService
from forgeflow.investigation.sources import FixtureSource
from forgeflow.product.environment import EnvironmentConfig
from forgeflow.recovery.storage import atomic_json, read_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('continuation_workbench', ROOT / 'scripts/continuation_workbench.py')
workbench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workbench)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    source, contract, _ = make_case()

    class LocalEnvironment:
        def __init__(self, config):
            self.config = config

        def generation(self):
            return 'default-selection-generation'

        def source_factory(self, policy):
            return FixtureSource(source.records, policy.source_policy() if isinstance(policy, PartialPolicy) else policy)

        def currency_probe(self, _):
            return {'currency': 'USD', 'company_id': 1, 'provenance': 'independent-fixture',
                    'record_currencies': {'order-and-offers': 'USD'}}

        def connection_factory(self):
            pytest.fail('Default selection must not access ERP')

        def executor_factory(self, *_):
            pytest.fail('Reply interpretation must not construct an execution adapter')

    monkeypatch.setattr(runtime, 'ManagedOdooEnvironment', LocalEnvironment)
    monkeypatch.setattr(runtime, 'load_config', lambda *_, **__: SimpleNamespace(
        model='local-no-provider', base_url='http://127.0.0.1/unused', api_key='unused-fixture'))
    settings = create_settings({'FORGEFLOW_DATA_ROOT': str(tmp_path / 'runs'),
        'FORGEFLOW_SESSION_SECRET': 'local-default-strategy-secret-0123456789',
        'FORGEFLOW_INVITE_CODES': 'local-default-strategy-owner'})
    config = EnvironmentConfig(lifecycle_file=tmp_path / 'lifecycle.json', container='no-container',
        bundle=tmp_path / 'bundle', read_key=tmp_path / 'read', write_key=tmp_path / 'write',
        repository=ROOT, docker_host='unix:///unused')
    return {'settings': settings, 'environment_config': config, 'root': tmp_path / 'runtime',
            'socket_root': tmp_path / 'sockets', 'original_contract': contract.payload(),
            'missing_order_id': 30, 'event': EVENT}


def waiting_reply(host, environment):
    cases = host.cases
    view = cases.create('owner', operation_id='create', **host.create_inputs('owner'))
    config = cases.episode_config(view['case_id'], 'owner', view['current_episode'])
    service = InvestigationService(source=environment.source_factory(config['policy']), contract=config['policy'],
        store=config['store'], bundle=config['store'] / 'unused', connection_factory=lambda: None, event=EVENT)
    service.dispatch(config['run_id'], 'finish', {'status': 'needs_clarification',
        'reason': 'Missing authorized budget', 'question': 'What budget do you authorize for SO-30?'})
    view = cases.record_terminal(view['case_id'], 'owner', view['current_episode'])
    cases.submit_reply(view['case_id'], 'owner', operation_id='answer', expected_revision=view['revision'],
                       text='I authorize a budget of 800 USD for SO-30.')
    return view['case_id']


@pytest.mark.parametrize('explicit, expected', [(None, 'fixed'), ('fixed', 'fixed'), ('interactive', 'interactive')])
def test_runtime_selection_reaches_real_host_job_and_reply_worker(configured, monkeypatch, explicit, expected):
    options = {} if explicit is None else {'strategy': explicit}
    app, host, worker, environment = runtime.build_runtime(**configured, decision_input_mode="legacy_reply", **options)
    calls = []

    def local_provider_boundary(**arguments):
        calls.append(arguments)
        assert arguments['mode'] == 'reply' and arguments['strategy'] == expected
        assert arguments['allowance'] == DEFAULT_REPLY_ALLOWANCE
        visible = json.loads(arguments['prompt'])['visible_replies']
        current = visible['replies'][-1]
        result = arguments['dispatch']('proposal', {'schema_version': 2, 'kind': 'budget_authorization_proposal',
            'order_id': '30', 'amount': '800.00', 'currency': 'USD',
            'source_spans': [{'reply_id': current['reply_id'], 'start': 0, 'end': len(current['text'])}]})
        assert result['accepted'] and not result['authorization_created']
        return {'seconds': 1, 'runtime_valid': True}, {'tokens': 10}

    monkeypatch.setattr(worker, '_model', local_provider_boundary)
    identity = waiting_reply(host, environment)
    job = host._job('reply', identity, 'owner', 'answer')
    assert job['binding']['strategy'] == expected
    view = host._perform(job)
    assert len(calls) == 1 and view['draft']['proposal']['amount'] == '800.00'
    assert view['confirmation'] is None and len(view['episodes']) == 1
    assert view['usage']['tokens'] == 10
    assert read_json(configured['root'] / 'runtime-binding.json')['strategy'] == expected
    assert app.state.continuation_host is host


def test_explicit_rules_only_replaces_reply_parser_not_parent_investigation(configured, monkeypatch):
    _, host, worker, environment = runtime.build_runtime(**configured, decision_input_mode="legacy_reply", strategy='rules')
    monkeypatch.setattr(worker, '_model', lambda **_: pytest.fail('Rule reply must not call a provider'))
    assert host.coordinator.episode_runner.__self__ is worker
    assert host.coordinator.episode_runner.__func__ is worker.episode.__func__
    identity = waiting_reply(host, environment)
    job = host._job('reply', identity, 'owner', 'answer')
    assert job['binding']['strategy'] == 'rules'
    view = host._perform(job)
    assert view['draft']['proposal']['amount'] == '800.00'
    assert view['usage']['tokens'] == 0 and view['confirmation'] is None


def test_old_interactive_binding_requires_explicit_strategy_on_restart(configured, monkeypatch):
    app, _, _, _ = runtime.build_runtime(**configured, decision_input_mode="legacy_reply", strategy='interactive')
    path = configured['root'] / 'runtime-binding.json'
    before = path.read_bytes()
    original_worker = runtime.PiCaseWorker
    monkeypatch.setattr(runtime, 'PiCaseWorker', lambda **_: pytest.fail('Changed binding must fail before worker construction'))
    with pytest.raises(ValueError, match='continuation_runtime_binding_changed'):
        runtime.build_runtime(**configured, decision_input_mode="legacy_reply")
    assert path.read_bytes() == before
    monkeypatch.setattr(runtime, 'PiCaseWorker', original_worker)
    restarted, host, _, _ = runtime.build_runtime(**configured, decision_input_mode="legacy_reply", strategy='interactive')
    assert host.strategy == 'interactive' and path.read_bytes() == before
    assert restarted.state.continuation_runtime_sha256 == app.state.continuation_runtime_sha256


@pytest.mark.parametrize('explicit, expected', [(None, 'fixed'), ('fixed', 'fixed'),
                                               ('interactive', 'interactive'), ('rules', 'rules')])
def test_workbench_config_selection_composes_the_bound_runtime(configured, tmp_path, monkeypatch, explicit, expected):
    environment = configured['environment_config']
    contract_path, event_path = tmp_path / 'contract.json', tmp_path / 'event.json'
    atomic_json(contract_path, configured['original_contract'])
    atomic_json(event_path, EVENT)
    config = {'root': str(configured['root']), 'socket_root': str(configured['socket_root']),
        'original_contract': str(contract_path), 'event': str(event_path), 'missing_order_id': 30,
        'environment': {name: str(getattr(environment, name)) for name in
            ('lifecycle_file', 'bundle', 'read_key', 'write_key', 'repository', 'container', 'docker_host')}}
    if explicit is not None:
        config['strategy'] = explicit
    path = tmp_path / 'host.json'
    atomic_json(path, config)
    monkeypatch.setattr(workbench, 'create_settings', lambda: configured['settings'])
    served = []
    monkeypatch.setattr(workbench.uvicorn, 'run', lambda app, **kwargs: served.append((app, kwargs)))
    monkeypatch.setattr(sys, 'argv', ['continuation_workbench.py', '--config', str(path), '--port', '8099'])
    workbench.main()
    (app, address), = served
    assert app.state.continuation_host.strategy == expected
    assert address == {'host': '127.0.0.1', 'port': 8099}
    assert json.loads((configured['root'] / 'runtime-binding.json').read_text())['strategy'] == expected
