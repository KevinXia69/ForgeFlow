"""Compose the authenticated continuation workbench for a trusted Odoo binding."""
from __future__ import annotations

import hashlib
from pathlib import Path

from forgeflow.api.app import create_app
from forgeflow.api.investigation import InvestigationAdapter
from forgeflow.config import load_config
from forgeflow.contract import contract_from_payload
from forgeflow.investigation.cases import CASE_ID, InvestigationCases, require
from forgeflow.investigation.continuation import ContinuationCoordinator
from forgeflow.investigation.event import trusted_event
from forgeflow.investigation.host import ContinuationHost
from forgeflow.investigation.intake import task_template
from forgeflow.investigation.policy import policy_from_payload
from forgeflow.investigation.worker import IMAGE, PiCaseWorker
from forgeflow.product import ProductCaseService
from forgeflow.product.environment import ManagedOdooEnvironment
from forgeflow.product.handoff import HandoffProductExecutor, HandoffRegistry
from forgeflow.recovery.storage import atomic_json, digest, read_json, scope_lock

DEFAULT_CONTINUATION_STRATEGY = 'fixed'
DEFAULT_REPLY_CONTRACT_VERSION = 2
DEFAULT_DECISION_INPUT_MODE = 'structured_budget_v1'


class WorkbenchCases(InvestigationCases):
    """Serve HTTP readers without contending with the registered decision writer."""

    def get(self, case_id, principal):
        # Each case.json is published atomically. Polling a committed snapshot
        # must not take the exclusive admission lock away from the worker.
        require(not self.root.is_symlink(), "invalid_case_directory")
        return self.public(self._read(case_id, principal))

    def list(self, principal):
        """Return only this authenticated owner's committed case snapshots."""
        require(not self.root.is_symlink(), "invalid_case_directory")
        found = []
        for directory in self.root.glob("ic-*/"):
            if not CASE_ID.fullmatch(directory.name) or directory.is_symlink():
                continue
            try:
                state = read_json(directory / "case.json")
                if state.get("owner") == principal:
                    found.append(self.public(state))
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                # A case directory may also be visible just before its first
                # aggregate is committed.
                continue
        return sorted(found, key=lambda row: row["updated_at"], reverse=True)


def reply_semantics_binding():
    """A new interpreter must never silently reopen an existing v2 runtime."""
    source = Path(__file__).resolve().parent
    return {'revision': 'interaction-repair-v1', 'source_sha256': {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in ('clarification.py', 'clarification_v2.py', 'worker.py')}}


def structured_decision_binding():
    source = Path(__file__).resolve().parent
    paths = [source / name for name in ('cases.py', 'host.py', 'continuation.py')]
    paths.append(source.parent / 'api/investigation.py')
    return {'revision': 'structured-budget-v1', 'source_sha256': {
        str(path.relative_to(source.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths}}


def missing_budget_policy(contract, order_id):
    """Trusted setup deliberately omits one existing grant; not a model tool."""
    original = contract.payload()
    if type(order_id) is not int or str(order_id) not in original['budgets']:
        raise ValueError('missing_order_not_authorized')
    return policy_from_payload({'kind': 'forgeflow.investigation_policy', 'schema_version': 1,
        'policy_id': 'continuation-missing-budget-v1', 'task_id': contract.task_id,
        **{k: original[k] for k in ('task_type', 'anchor_date', 'margin', 'margin_basis', 'time_granularity')},
        'orders': {str(i): {'authorized': True, 'budget_cap': None if i == order_id else original['budgets'][str(i)]}
                   for i in original['sales_order_ids']}, 'sources': {'policy': 'trusted-task-owner'}})


def build_runtime(*, settings, environment_config, root, socket_root, original_contract,
                  missing_order_id, event, strategy=DEFAULT_CONTINUATION_STRATEGY, image=IMAGE,
                  reply_contract_version=DEFAULT_REPLY_CONTRACT_VERSION,
                  decision_input_mode=DEFAULT_DECISION_INPUT_MODE):
    """All parameters are host configuration; HTTP has no policy upload route."""
    root = Path(root)
    if type(reply_contract_version) is not int or reply_contract_version not in (1, 2):
        raise ValueError('unsupported_reply_contract_version')
    if decision_input_mode not in ('legacy_reply', 'structured_budget_v1'):
        raise ValueError('unsupported_decision_input_mode')
    contract = contract_from_payload(original_contract)
    policy = missing_budget_policy(contract, missing_order_id)
    template = task_template(contract)
    event = trusted_event(event)
    environment = ManagedOdooEnvironment(environment_config)
    generation = environment.generation()
    model_config = load_config(environment_config.repository, require_model=True)
    binding = {'schema_version': 1, 'generation': generation, 'template': template,
        'policy': policy.payload(), 'event': event, 'strategy': strategy, 'image': image,
        'product_root': str(settings.approvals_root.resolve()), 'socket_root': str(Path(socket_root).resolve()),
        'bundle': str(environment_config.bundle.resolve()),
        'model': {'provider': 'nebius', 'model': model_config.model, 'base_url': model_config.base_url}}
    # Explicit v1 leaves the old binding format and bytes intact on restart.
    if reply_contract_version == 2:
        binding['reply_contract_version'] = reply_contract_version
        if decision_input_mode == 'legacy_reply':
            binding['reply_semantics'] = reply_semantics_binding()
    if decision_input_mode == 'structured_budget_v1':
        binding.update(decision_input_mode=decision_input_mode, decision_contract_version=1,
                       decision_implementation=structured_decision_binding())
    with scope_lock(root):
        path = root / 'runtime-binding.json'
        if path.exists():
            if read_json(path) != binding:
                raise ValueError('continuation_runtime_binding_changed')
        else:
            atomic_json(path, binding)
    registry = HandoffRegistry(root / 'product-registry', socket_root=socket_root,
                               generation=environment.generation, executor_factory=environment.executor_factory)
    router = HandoffProductExecutor(registry)
    product = ProductCaseService(settings.approvals_root, router,
        environment_generation=generation if settings.deployment == 'hosted' else None)
    cases = WorkbenchCases(root / 'investigations', source_factory=environment.source_factory,
                               generation=environment.generation, currency_probe=environment.currency_probe)
    worker = PiCaseWorker(config=model_config, source_factory=environment.source_factory,
        connection_factory=environment.connection_factory, bundle=environment_config.bundle, image=image,
        docker_host=environment_config.docker_host)
    coordinator = ContinuationCoordinator(cases, episode_runner=worker.episode, reply_interpreter=worker.reply)
    host = ContinuationHost(cases=cases, coordinator=coordinator, product=product, strategy=strategy,
        recover_worker=worker.recover,
        create_inputs=lambda principal: {'template': template, 'policy': policy, 'event': event,
                                        'reply_contract_version': reply_contract_version,
                                        'decision_input_mode': decision_input_mode})
    adapter = InvestigationAdapter(cases=cases, create_case=host.create, schedule=host.schedule,
        background=host.background, request_closure=host.request_closure,
        expire=host.expire, reconcile_closure=host.reconcile_closure,
        expire_pending=host.expire_pending)
    app = create_app(settings, product_executor=router, investigation_adapter=adapter)
    app.state.continuation_host = host
    app.state.continuation_runtime_sha256 = digest(binding)
    return app, host, worker, environment
