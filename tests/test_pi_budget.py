"""Hermetic Pi adapter tests: never import or read the private upstream tree.

Only the upstream base-class load is replaced with a small fixture. The actual
adapter method AST, Harbor prompt decorator, clock, deadline and cleanup run.
Pinned Pi runtime/VM integration is a separate, deliberately unrun acceptance.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import shlex
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forgeflow.budget import (
    CLEANUP_ALLOWANCE_SEC,
    BudgetCleanupError,
    BudgetClock,
    BudgetExceeded,
    BudgetRecorder,
    Deadline,
)
from forgeflow.config import NebiusConfig


@pytest.fixture
def pi_module():
    class FixtureUpstream:
        def __init__(self, *, logs_dir, model_name, **kwargs):
            self.logs_dir = logs_dir
            self.model_name = model_name

        def render_instruction(self, instruction):
            return instruction

    path = Path(__file__).resolve().parents[1] / "src/forgeflow/agents/pi_nebius.py"
    syntax = ast.parse(path.read_text(), filename=str(path))
    replaced = 0
    for node in syntax.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "UpstreamPi" for target in node.targets
        ):
            node.value = ast.Name(id="FixtureUpstream", ctx=ast.Load())
            replaced += 1
    assert replaced == 1
    module = ModuleType("_hermetic_pi_budget")
    module.__file__ = str(path)
    module.__dict__["FixtureUpstream"] = FixtureUpstream
    exec(compile(ast.fix_missing_locations(syntax), str(path), "exec"), module.__dict__)
    return module


def make_agent(module, tmp_path, **kwargs):
    module.load_config = lambda root: NebiusConfig(tmp_path, api_key="synthetic-key-canary")
    agent = module.PiNebius(logs_dir=tmp_path / "agent", workspace_root=tmp_path, **kwargs)
    agent._configure_budget = AsyncMock()
    agent.validate_isolation = AsyncMock()
    agent._upload_config_text = AsyncMock()
    agent.exec_as_agent = AsyncMock()
    agent.exec_as_root = AsyncMock()
    environment = SimpleNamespace(with_default_user=lambda user: contextlib.nullcontext())
    return agent, environment


def test_adapter_binds_explicit_budget_and_root_gate_in_launch(pi_module, tmp_path):
    agent, environment = make_agent(pi_module, tmp_path, agent_budget_sec=17)
    asyncio.run(agent.run("synthetic prompt canary", environment, SimpleNamespace()))
    call = agent.exec_as_agent.call_args.kwargs
    assert call["timeout_sec"] <= 17
    assert pi_module.BUDGET_EXTENSION_PATH in shlex.split(call["command"])
    assert pi_module.TOOL_AUDIT_EXTENSION in shlex.split(call["command"])
    payload = json.loads((tmp_path / "host-budget.json").read_text())
    assert not (tmp_path / "agent" / "budget.json").exists()
    assert payload["phases"]["agent"]["limit_sec"] == 17
    assert payload["phases"]["agent"]["status"] == "completed"
    assert payload["phases"]["cleanup"]["limit_sec"] == CLEANUP_ALLOWANCE_SEC
    assert payload["cleanup_quiescence"] == "confirmed"
    assert payload["possible_partial_execution"] is False
    serialized = json.dumps(payload)
    assert "synthetic-key-canary" not in serialized
    assert "synthetic prompt canary" not in serialized
    commands = [call.kwargs["command"] for call in agent.exec_as_root.call_args_list]
    assert "budget.closed" in commands[0] and "pkill -STOP" in commands[0]
    assert "pkill -KILL" in commands[-1]
    for command in commands:
        assert subprocess.run(["bash", "-n", "-c", command], capture_output=True).returncode == 0


def test_timeout_preserves_possible_writes_and_stops_dispatch_before_executor(pi_module, tmp_path):
    agent, environment = make_agent(
        pi_module, tmp_path, agent_budget_sec=1,
        planner_profile="cp-sat-recovery-v1", security_profile="permission-split-v1",
    )
    clocks = {"continuous": 100.0, "utc": 1_800_000_000.0}
    clock = BudgetClock("synthetic-suspend", lambda: clocks["continuous"],
                        lambda: 100.0, lambda: clocks["utc"], lambda: 0)
    agent._budget_recorder = BudgetRecorder(tmp_path / "host-budget.json", clock)

    async def stopped_model(*args, **kwargs):
        clocks["continuous"] += 2
        clocks["utc"] += 2
        await asyncio.Event().wait()

    agent.exec_as_agent = AsyncMock(side_effect=stopped_model)
    with pytest.raises(BudgetExceeded):
        asyncio.run(agent.run("Task", environment, SimpleNamespace()))
    payload = json.loads((tmp_path / "host-budget.json").read_text())
    assert payload["phases"]["agent"]["status"] == "timed_out"
    assert payload["possible_partial_execution"] is True
    commands = [call.kwargs["command"] for call in agent.exec_as_root.call_args_list]
    assert "budget.closed" in commands[0]
    assert "executor.ready" in commands[1]
    assert "pkill -KILL" in commands[2]
    assert agent.exec_as_agent.await_count == 1


def test_no_new_model_dispatch_if_budget_expires_during_preparation(pi_module, tmp_path):
    agent, environment = make_agent(pi_module, tmp_path, agent_budget_sec=1)
    current = [100.0]
    clock = BudgetClock("synthetic", lambda: current[0])
    agent._budget_recorder = BudgetRecorder(tmp_path / "host-budget.json", clock)

    async def expire_during_upload(*args, **kwargs):
        current[0] += 2

    agent._upload_config_text = AsyncMock(side_effect=expire_during_upload)
    with pytest.raises(BudgetExceeded):
        asyncio.run(agent.run("Task", environment, SimpleNamespace()))
    agent.exec_as_agent.assert_not_called()
    payload = json.loads((tmp_path / "host-budget.json").read_text())
    assert payload["dispatch_started"] is False
    assert payload["possible_partial_execution"] is False


def test_cleanup_hang_is_bounded_and_does_not_forge_quiescence(pi_module, tmp_path):
    agent, environment = make_agent(pi_module, tmp_path)
    current = [100.0]
    clock = BudgetClock("synthetic", lambda: current[0])
    agent._budget_recorder = BudgetRecorder(tmp_path / "host-budget.json", clock)
    calls = []

    async def cleanup(*args, **kwargs):
        calls.append(kwargs["command"])
        if len(calls) == 1:
            current[0] += 4  # beyond the gate-close stage's 3 second allowance
            await asyncio.Event().wait()

    agent.exec_as_root = AsyncMock(side_effect=cleanup)
    with pytest.raises(BudgetCleanupError):
        asyncio.run(agent.run("Task", environment, SimpleNamespace()))
    payload = json.loads((tmp_path / "host-budget.json").read_text())
    assert len(calls) == 2  # still tries the distinct final kill/export stage
    assert payload["cleanup_quiescence"] == "unknown"
    assert payload["phases"]["cleanup"]["status"] == "timed_out"
    assert payload["possible_partial_execution"] is True


def test_transport_cancellation_still_runs_bounded_cleanup(pi_module, tmp_path):
    agent, environment = make_agent(pi_module, tmp_path)
    agent.exec_as_agent = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(agent.run("Task", environment, SimpleNamespace()))
    payload = json.loads((tmp_path / "host-budget.json").read_text())
    assert payload["phases"]["agent"]["status"] == "cancelled"
    assert payload["phases"]["cleanup"]["status"] == "completed"
    assert payload["possible_partial_execution"] is True


def test_setup_timeout_has_its_own_budget_and_never_dispatches_model(pi_module, tmp_path):
    agent, environment = make_agent(pi_module, tmp_path)
    current = [100.0]
    clock = BudgetClock("synthetic", lambda: current[0])
    agent._budget_recorder = BudgetRecorder(tmp_path / "host-budget.json", clock)

    async def installation(*args):
        current[0] += 1801
        await asyncio.Event().wait()

    agent._install = AsyncMock(side_effect=installation)
    with pytest.raises(BudgetExceeded):
        asyncio.run(agent.install(environment))
    payload = json.loads((tmp_path / "host-budget.json").read_text())
    assert payload["phases"]["setup"]["limit_sec"] == 1800
    assert payload["phases"]["setup"]["status"] == "timed_out"
    assert "agent" not in payload["phases"]
    agent.exec_as_agent.assert_not_called()


def test_budget_configuration_rejects_arbitrary_remote_data(pi_module, tmp_path):
    agent, environment = make_agent(pi_module, tmp_path)
    agent.exec_as_root.return_value = SimpleNamespace(stdout='{"secret-canary":"value"}')
    with pytest.raises(RuntimeError, match="clock sample is unavailable") as error:
        asyncio.run(pi_module.PiNebius._configure_budget(
            agent, environment, Deadline(agent._budget().clock, 600),
        ))
    assert "secret-canary" not in str(error.value)
    assert agent._budget().data["container_samples"] == []
