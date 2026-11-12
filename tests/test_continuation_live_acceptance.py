"""No-model checks of predeclared live acceptance inputs and dispatch gates."""
import importlib.util
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.investigation.fixtures import supplier_cancellation_control
from forgeflow.recovery.storage import atomic_json, digest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("continuation_live_acceptance", ROOT / "scripts/continuation_live_acceptance.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


@pytest.mark.parametrize("variant", ["clear", "correct"])
def test_predeclared_user_budget_is_not_inserted_into_initial_policy(variant):
    contract = supplier_cancellation_control().contract
    template, policy, event, script = driver.build_inputs(contract, order_name="SO-1", variant=variant)
    assert "budgets" not in template["fields"]
    assert policy.payload()["orders"] == {"1": {"authorized": True, "budget_cap": None}}
    assert "2000" not in event["text"] and "sales_order_ids" not in event["text"]
    assert sum(reply["confirm"] for reply in script["replies"]) == 1
    assert script["replies"][-1]["expected_amount"] == "2000.00"
    if variant == "correct":
        assert script["replies"][0]["expected_kind"] == ["price_fact", "ambiguous"]
        assert not script["replies"][1]["confirm"]
        assert Decimal(script["replies"][1]["expected_amount"]) != Decimal(script["replies"][-1]["expected_amount"])


def test_changed_source_cannot_start_paid_worker(tmp_path, monkeypatch):
    atomic_json(tmp_path / "preflight.json", {"status": "preflight_ready", "generation": "one", "source_files": {}})
    monkeypatch.setattr(driver, "PiCaseWorker", lambda **_: pytest.fail("must not construct worker"))
    monkeypatch.setattr(driver, "load_config", lambda *_, **__: pytest.fail("must not load model credentials"))
    environment = SimpleNamespace(generation=lambda: "one")
    with pytest.raises(RuntimeError, match="preflight_sources_changed"):
        driver.execute(ROOT, environment, tmp_path)
    assert not (tmp_path / "run-started.json").exists()


def test_prior_dispatch_marker_never_auto_retries(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    provenance = {"test": "no executable model inputs"}
    atomic_json(bundle / "provenance.json", provenance)
    selected = {"container": "continuation-a-clear", "port": 18120}
    atomic_json(tmp_path / "environment-input.json", selected)
    atomic_json(tmp_path / "preflight.json", {"status": "preflight_ready", "generation": "one",
        **selected, "source_files": driver.source_hashes(ROOT), "bundle_provenance_sha256": digest(provenance),
        "inputs": {"environment-input.json": digest(selected)}})
    atomic_json(tmp_path / "run-started.json", {"phase": "unknown"})
    monkeypatch.setattr(driver, "business_tables", lambda _: pytest.fail("must not read or write ERP after prior dispatch"))
    monkeypatch.setattr(driver, "PiCaseWorker", lambda **_: pytest.fail("must not reconstruct model worker"))
    environment = SimpleNamespace(generation=lambda: "one", config=SimpleNamespace(bundle=bundle, **selected))
    with pytest.raises(RuntimeError, match="run_already_started_never_auto_retry"):
        driver.execute(ROOT, environment, tmp_path)


def test_artifact_index_excludes_runtime_credentials_approvals_and_private_case(tmp_path):
    for directory in (tmp_path / "runtime-inputs", tmp_path / "approvals", tmp_path / "store"):
        directory.mkdir()
    atomic_json(tmp_path / "runtime-inputs/model-config.json", {"secret": "not-exported"})
    atomic_json(tmp_path / "approvals/manifest.json", {"grant": "not-exported"})
    atomic_json(tmp_path / "case.json", {"approval": "not-exported"})
    atomic_json(tmp_path / "store/manifest.json", {"public": True})
    assert set(driver.artifact_index(tmp_path)) == {"store/manifest.json"}


@pytest.mark.parametrize("changed", [
    {"container": "continuation-a-clear-r2", "port": 18120},
    {"container": "continuation-a-clear", "port": 18122},
])
def test_preflight_cannot_authorize_another_environment_even_with_same_generation(tmp_path, monkeypatch, changed):
    selected = {"container": "continuation-a-clear", "port": 18120}
    atomic_json(tmp_path / "preflight.json", {"status": "preflight_ready", "generation": "one",
        **selected, "source_files": driver.source_hashes(ROOT),
        "inputs": {"environment-input.json": digest(selected)}})
    monkeypatch.setattr(driver, "load_config", lambda *_, **__: pytest.fail("must not load model credentials"))
    monkeypatch.setattr(driver, "PiCaseWorker", lambda **_: pytest.fail("must not construct model worker"))
    environment = SimpleNamespace(generation=lambda: "one", config=SimpleNamespace(**changed))
    with pytest.raises(RuntimeError, match="preflight_environment_changed"):
        driver.execute(ROOT, environment, tmp_path)
    assert not (tmp_path / "run-started.json").exists()


def test_environment_identity_must_be_in_preflight_input_hashes(tmp_path):
    selected = {"container": "continuation-a-clear", "port": 18120}
    atomic_json(tmp_path / "preflight.json", {"status": "preflight_ready", "generation": "one",
        **selected, "source_files": driver.source_hashes(ROOT), "inputs": {}})
    environment = SimpleNamespace(generation=lambda: "one", config=SimpleNamespace(**selected))
    with pytest.raises(RuntimeError, match="preflight_environment_changed"):
        driver.execute(ROOT, environment, tmp_path)


@pytest.mark.parametrize("args, container, port", [
    ([], "continuation-a-clear", 18120),
    (["--container", "continuation-a-clear-r2", "--port", "18122"], "continuation-a-clear-r2", 18122),
])
def test_host_cli_selects_only_managed_environment_under_runtime(tmp_path, monkeypatch, args, container, port):
    received = []
    monkeypatch.setattr(driver, "ManagedOdooEnvironment", lambda config: received.append(config) or config)
    monkeypatch.setattr(driver, "preflight", lambda *args: {"status": "preflight_ready", "variant": "clear"})
    assert driver.main(["--variant", "clear", "--output", str(tmp_path / "out"), *args]) == 0
    config, = received
    assert config.container == container and config.port == port
    assert config.lifecycle_file == ROOT / ".runtime" / container / "lifecycle.json"
    assert config.bundle == ROOT / ".runtime" / container / "planner"


@pytest.mark.parametrize("extra", [
    ["--container", "../other", "--port", "18122"],
    ["--container", "/tmp/other", "--port", "18122"],
    ["--container", "two containers", "--port", "18122"],
    ["--container", "-option", "--port", "18122"],
    ["--container", "continuation-a-clear-r2", "--port", "80"],
    ["--container", "continuation-a-clear-r2", "--port", "65536"],
    ["--container", "continuation-a-clear-r2", "--port", "18122.5"],
    ["--container", "continuation-a-clear-r2"],
    ["--port", "18122"],
])
def test_host_cli_rejects_invalid_or_incomplete_environment_selection(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(driver, "ManagedOdooEnvironment", lambda _: pytest.fail("must not construct environment"))
    with pytest.raises(SystemExit) as caught:
        driver.main(["--variant", "clear", "--output", str(tmp_path / "out"), *extra])
    assert caught.value.code == 2
