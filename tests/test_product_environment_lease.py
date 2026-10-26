"""One hosted ERP baseline belongs to one principal until an operator reset."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from forgeflow.product import FakeProductExecutor, ProductCaseError, ProductCaseService
from forgeflow.product.lease import EnvironmentLease, EnvironmentLeaseError

GENERATION_A = "a" * 32
GENERATION_B = "b" * 32


def service(root, generation=GENERATION_A, executor=None):
    return ProductCaseService(root, executor or FakeProductExecutor(),
                              environment_generation=generation)


def test_two_independent_service_instances_admit_only_one_owner(tmp_path):
    barrier = threading.Barrier(2)

    def create(owner):
        instance = service(tmp_path)
        barrier.wait(timeout=5)
        try:
            return owner, instance.create(owner, "A")["case_id"]
        except ProductCaseError as error:
            return owner, error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["reviewer-a", "reviewer-b"]))
    winners = [(owner, result) for owner, result in results if result != "environment_busy"]
    assert len(winners) == 1
    owner, case_id = winners[0]
    fresh = service(tmp_path)
    assert fresh.get(case_id, owner) is not None
    status = fresh.environment_status(owner)
    assert status["available"] is True and status["owned_by_me"] is True
    other = "reviewer-b" if owner == "reviewer-a" else "reviewer-a"
    assert fresh.environment_status(other)["reason"] == "environment_busy"
    assert owner not in json.dumps(fresh.environment_status(other))
    assert fresh.get(case_id, other) is None


def test_completion_and_process_recreation_do_not_release_the_environment(tmp_path):
    first = service(tmp_path)
    case = first.create("reviewer-a", "A")
    prepared = first.prepare(case["case_id"], "reviewer-a")
    first.approve(case["case_id"], "reviewer-a", prepared["plan"]["plan_id"])
    fresh = service(tmp_path)
    with pytest.raises(ProductCaseError, match="environment_busy"):
        fresh.create("reviewer-b", "A")
    assert fresh.environment_status("reviewer-a")["owned_by_me"] is True
    assert fresh.get(case["case_id"], "reviewer-a")["execution"] is not None


def test_environment_change_requires_explicit_reset_and_keeps_old_cases_read_only(tmp_path):
    executor = FakeProductExecutor()
    previous = service(tmp_path, executor=executor)
    old_case = previous.create("reviewer-a", "A")
    prepared = previous.prepare(old_case["case_id"], "reviewer-a")
    current = service(tmp_path, GENERATION_B, executor)
    with pytest.raises(ProductCaseError, match="environment_generation_mismatch"):
        current.create("reviewer-b", "A")
    lease = EnvironmentLease(previous.root, GENERATION_B)
    lease.initialize_empty(previous_generation=GENERATION_A)
    for instance in (previous, current):
        with pytest.raises(ProductCaseError, match="environment_generation_mismatch"):
            instance.prepare(old_case["case_id"], "reviewer-a")
        with pytest.raises(ProductCaseError, match="environment_generation_mismatch"):
            instance.approve(old_case["case_id"], "reviewer-a", prepared["plan"]["plan_id"])
    historical = current.get(old_case["case_id"], "reviewer-a")
    assert historical["plan"]["plan_id"] == prepared["plan"]["plan_id"]
    assert historical["decision"]["can_approve"] is False
    assert historical["decision"]["reason"] == "environment_generation_mismatch"
    assert executor.apply_calls == 0
    assert executor.prepare_calls == 1
    new_case = current.create("reviewer-b", "A")
    assert current.prepare(new_case["case_id"], "reviewer-b")["plan"] is not None
    assert current.get(old_case["case_id"], "reviewer-b") is None


def test_same_generation_cannot_be_reset_and_cas_protects_newer_state(tmp_path):
    lease = EnvironmentLease(tmp_path, GENERATION_A)
    lease.claim("reviewer-a", "1" * 32)
    with pytest.raises(EnvironmentLeaseError, match="new_environment_generation_required"):
        lease.initialize_empty(previous_generation=GENERATION_A)
    next_lease = EnvironmentLease(tmp_path, GENERATION_B)
    with pytest.raises(EnvironmentLeaseError, match="environment_generation_mismatch"):
        next_lease.initialize_empty(previous_generation="c" * 32)
    lease.require("reviewer-a", "1" * 32)


def test_corrupt_or_symlinked_lease_never_becomes_an_empty_available_environment(tmp_path):
    lease = EnvironmentLease(tmp_path, GENERATION_A)
    lease.path.write_text("{bad-json", encoding="utf-8")
    with pytest.raises(EnvironmentLeaseError, match="environment_lease_unavailable"):
        lease.claim("reviewer-a", "1" * 32)
    lease.path.unlink()
    other = tmp_path / "outside.json"
    other.write_text("{}", encoding="utf-8")
    lease.path.symlink_to(other)
    with pytest.raises(EnvironmentLeaseError, match="environment_lease_unavailable"):
        lease.claim("reviewer-a", "1" * 32)
    assert other.read_text(encoding="utf-8") == "{}"


def test_local_service_preserves_multiple_owner_behavior(tmp_path):
    local = ProductCaseService(tmp_path, FakeProductExecutor())
    first = local.create("reviewer-a", "A")
    second = local.create("reviewer-b", "A")
    assert first["case_id"] != second["case_id"]
    assert local.environment_status("reviewer-b")["enforced"] is False
