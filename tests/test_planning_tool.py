"""Exercise the shipped planner with synthetic inputs, never task answers."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import runpy
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.planning import tool
from forgeflow.planning.bundle import build_planner_bundle
from forgeflow.planning.live import LiveInputError

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def planner_bundle(tmp_path):
    if not (WORKSPACE_ROOT / "vendor/erp-bench.source.json").is_file():
        pytest.skip("Fetch the pinned ERP-Bench source before bundle integration checks.")
    archive_path = tmp_path / "planner.tar.gz"
    record = build_planner_bundle(WORKSPACE_ROOT, archive_path)
    with tarfile.open(archive_path) as archive:
        archive.extractall(tmp_path, filter="data")
    return tmp_path / "planner", archive_path, record


def test_bundle_contains_only_reviewed_regular_sources_with_matching_hashes(planner_bundle):
    root, archive_path, record = planner_bundle
    expected = {
        "forgeflow/__init__.py",
        "forgeflow/odoo_credentials.py",
        "forgeflow/planning/__init__.py",
        "forgeflow/planning/live.py",
        "forgeflow/planning/tool.py",
        "forgeflow/planning/objectives.py",
        "upstream/LICENSE",
        "upstream/procurement/__init__.py",
        "upstream/procurement/solver.py",
        "upstream/procurement/procurement_objectives.py",
        "upstream/schemas/__init__.py",
        "upstream/schemas/models.py",
        "upstream/schemas/procurement_invoicing_validation.py",
        "plan.py",
        "USAGE.md",
        "requirements.txt",
    }
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        assert {item.name for item in members} == {
            f"planner/{name}" for name in expected | {"provenance.json"}
        }
        assert all(item.isfile() and item.mode & 0o222 == 0 for item in members)
        assert not any(item.issym() or item.islnk() for item in members)
        assert not any(
            {"tasks", "solution", "tests", "environment"} & set(Path(item.name).parts)
            for item in members
        )
    assert record["task_specific_files"] is False
    assert record["odoo_writes"] is False
    assert set(record["files_sha256"]) == expected
    for name, digest in record["files_sha256"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    assert json.loads((root / "provenance.json").read_text()) == record
    assert (root / "requirements.txt").read_bytes() == (
        WORKSPACE_ROOT / "build-inputs/planner/requirements.txt"
    ).read_bytes()


def toy_blueprint():
    """Five units need a replacement supplier; all identities are invented."""
    broken_offer = "TOY-OLD|TOY-FIN|1|1.0|1"
    return {
        "scenario_number": 0,
        "name": "Synthetic replacement purchase",
        "instruction": "Synthetic fixture only",
        "background_and_policy": "Synthetic fixture only",
        "objective_kind": "repair_plan",
        "margin": 0.2,
        "product": {
            "name": "Toy finished product",
            "code": "TOY-FIN",
            "list_price_dollars": 10,
            "standard_price_dollars": 1,
            "routes": ["buy"],
        },
        "customers": [{
            "name": "Toy customer",
            "ref": "TOY-CUSTOMER",
            "email": "toy@example.test",
            "demand": 5,
            "deadline": 5,
            "budget_dollars": 50,
            "task_order_source": "seeded",
            "task_order_ref": "TOY-SO",
            "seeded_order_state": "sale",
        }],
        "vendors": [
            {
                "name": "Toy canceled vendor" if old else "Toy replacement vendor",
                "ref": "TOY-OLD" if old else "TOY-NEW",
                "supplier_rank": 1,
                "product_code": "TOY-FIN",
                "price_dollars": 1 if old else 2,
                "lead_time": 1,
                "min_qty": 1,
                "max_qty": 10,
            }
            for old in (True, False)
        ],
        "repair_context": {
            "kind": "supplier_cancellation",
            "broken_supplier_offer_key": broken_offer,
            "broken_supplier_vendor_ref": "TOY-OLD",
            "impacted_customer_refs": ["TOY-CUSTOMER"],
            "baseline_offer_quantities": [{"offer_key": broken_offer, "quantity": 5}],
            # The upstream schema uses this name for current live commitments.
            "seeded_purchase_orders": [{
                "ref": "TOY-PO",
                "vendor_ref": "TOY-OLD",
                "product_code": "TOY-FIN",
                "quantity": 5,
                "price_unit": 1,
                "planned_arrival_days": 1,
                "origin_customer_refs": ["TOY-CUSTOMER"],
                "offer_key": broken_offer,
            }],
            "broken_purchase_order_refs": ["TOY-PO"],
        },
    }


def test_copied_bundle_solves_synthetic_repair_with_real_cp_sat_and_trace(
    planner_bundle, tmp_path,
):
    for dependency in ("ortools", "pydantic", "rich"):
        if importlib.util.find_spec(dependency) is None:
            pytest.skip(f"Install optional planning dependencies: missing {dependency}.")
    root, _, _ = planner_bundle
    blueprint = toy_blueprint()
    request_path = tmp_path / "synthetic-blueprint.json"
    request_path.write_text(json.dumps(blueprint))
    script = """
import json
import sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from forgeflow.planning import tool
assert Path(tool.__file__).resolve().is_relative_to(root)
result = tool.solve_blueprint(json.loads(Path(sys.argv[2]).read_text()), root / 'upstream')
import procurement.solver
assert Path(procurement.solver.__file__).resolve().is_relative_to(root / 'upstream')
print(json.dumps(result))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(root), str(request_path)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = json.loads(completed.stdout)
    assert result["odoo_writes"] is False
    assert result["execution_validated"] is False
    assert result["blueprint_sha256"] == hashlib.sha256(
        json.dumps(blueprint, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    assert result["solver"]["version"] == "9.15.6755"
    assert result["trace"]["models"]
    assert result["trace"]["models"][0]["constraints"] > 0
    assert result["trace"]["models"][0]["linear_constraints"] > 0
    assert result["trace"]["solve_calls"]
    assert all(
        call["status"] in {"OPTIMAL", "FEASIBLE"}
        for call in result["trace"]["solve_calls"]
    )
    purchases = result["plan"]["purchase_orders"]
    assert {(row["vendor_ref"], row["quantity"]) for row in purchases} == {("TOY-NEW", 5)}
    assert result["plan"]["expected_new_spend"] == 10
    assert not list(root.rglob("__pycache__"))


@pytest.mark.parametrize("location", ["request", "disruption"])
def test_invalid_secret_field_is_rejected_before_any_persistence_or_connection(
    tmp_path, monkeypatch, capsys, location,
):
    secret = "fixture-secret-must-never-be-logged"
    request = {
        "target_product_code": "TOY-FIN",
        "sales_order_ids": [1],
        "anchor_date": "2026-01-01",
        "margin": 0.2,
        "sales_order_budget_caps": {"1": 50},
        "disruption": {"kind": "supplier_cancellation", "vendor_ref": "TOY-OLD"},
    }
    (request if location == "request" else request["disruption"])["api_key"] = secret
    request_path = tmp_path / "invalid-request.json"
    request_path.write_text(json.dumps(request))
    output = tmp_path / "must-not-exist"
    original_read = Path.read_text

    def read_request_only(path, *args, **kwargs):
        assert path == request_path, "Rejected requests must not read connection credentials."
        return original_read(path, *args, **kwargs)

    def unexpected_side_effect(*args, **kwargs):
        pytest.fail("Invalid requests must be rejected before output/audit creation or RPC.")

    monkeypatch.setattr(Path, "read_text", read_request_only)
    monkeypatch.setattr(Path, "mkdir", unexpected_side_effect)
    monkeypatch.setitem(
        sys.modules, "odoolib", SimpleNamespace(get_connection=unexpected_side_effect)
    )
    assert tool.main(["--request", str(request_path), "--output", str(output)]) == 2
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert json.loads(captured.out)["ok"] is False
    assert not output.exists()


@pytest.fixture
def synthetic_cli(tmp_path, monkeypatch):
    """Run the actual CLI flow with synthetic reads and in-memory artifact writes."""
    from forgeflow.planning import live

    fixture_module = runpy.run_path(str(Path(__file__).with_name("test_planning_live.py")))
    snapshot, request = fixture_module["fixture_data"]()
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    output = tmp_path / "result"
    reads, saved, connections = [], [], []
    original_read = Path.read_text

    def read_fixture(path, *args, **kwargs):
        reads.append(path)
        if path == Path("/etc/odoo/api_key"):
            return "synthetic-credential-not-for-output"
        assert path == request_path
        return original_read(path, *args, **kwargs)

    def connection(**kwargs):
        connections.append(kwargs)
        return object()

    monkeypatch.setattr(Path, "read_text", read_fixture)
    monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tool, "_write_json", lambda path, value: saved.append((path, copy.deepcopy(value)))
    )
    monkeypatch.setitem(sys.modules, "odoolib", SimpleNamespace(get_connection=connection))
    monkeypatch.setattr(live, "collect_snapshot", lambda client, req: copy.deepcopy(snapshot))
    return SimpleNamespace(
        snapshot=snapshot, request=request, output=output, saved=saved, reads=reads,
        connections=connections, argv=["--request", str(request_path), "--output", str(output)],
        add_demand=fixture_module["add_sales_demand"],
    )


def test_cli_returns_safe_complete_missing_demand_protocol_before_solver(
    synthetic_cli, monkeypatch, capsys,
):
    fixture = synthetic_cli
    fixture.add_demand(fixture.snapshot, 12)
    fixture.add_demand(fixture.snapshot, 9, pid=3)
    line = copy.deepcopy(fixture.snapshot["models"]["sale.order.line"][-1])
    line["id"] = 4
    fixture.snapshot["models"]["sale.order.line"].append(line)

    def unexpected_solver(*args, **kwargs):
        pytest.fail("Shared demand must be rejected before invoking the solver.")

    monkeypatch.setattr(tool, "solve_blueprint", unexpected_solver)
    assert tool.main(fixture.argv) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "schema_version": 1, "ok": False, "odoo_writes": False,
        "error_code": "unselected_active_demand",
        "details": {"missing_sales_order_ids": [9, 12]},
    }
    assert len(fixture.connections) == 1
    assert [path.name for path, _ in fixture.saved] == [
        "request.json", "request.json", "snapshot.json", "snapshot.json",
    ]
    assert all("synthetic-credential" not in json.dumps(value) for _, value in fixture.saved)
    assert "synthetic-credential" not in captured.out


@pytest.mark.parametrize(
    ("code", "details"),
    [
        (None, None),
        ("unknown-secret-marker", {"missing_sales_order_ids": [1]}),
        (["unselected_active_demand"], {"missing_sales_order_ids": [1]}),
        ("unselected_active_demand", None),
        ("unselected_active_demand", {}),
        ("unselected_active_demand", {"missing_sales_order_ids": [1], "secret": "secret-marker"}),
        *[
            ("unselected_active_demand", {"missing_sales_order_ids": ids})
            for ids in (
                [], [True], [0], [-1], [1.5], ["secret-marker"], [2**53],
                [1, 1], [2, 1], (1, 2), "secret-marker", list(range(1, 502)),
            )
        ],
    ],
)
def test_cli_rejects_unrecognized_or_malformed_error_details_without_echo(
    synthetic_cli, monkeypatch, capsys, code, details,
):
    from forgeflow.planning import live

    def failed_blueprint(*args):
        raise LiveInputError("private-business-secret-marker", code=code, details=details)

    monkeypatch.setattr(live, "build_blueprint", failed_blueprint)
    assert tool.main(synthetic_cli.argv) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "ok": False, "error": tool.PLANNING_FAILURE_MESSAGE,
    }
    assert captured.err == ""
    assert "secret-marker" not in captured.out
    assert "synthetic-credential" not in captured.out


@pytest.mark.parametrize("stage", ["collect_snapshot", "build_blueprint"])
def test_rpc_and_other_exception_text_is_not_exposed(
    synthetic_cli, monkeypatch, capsys, stage,
):
    from forgeflow.planning import live

    def failed(*args):
        raise RuntimeError("private-rpc-secret-marker")

    monkeypatch.setattr(live, stage, failed)
    assert tool.main(synthetic_cli.argv) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "ok": False, "error_type": "RuntimeError", "error": tool.PLANNING_FAILURE_MESSAGE,
    }
    assert captured.err == ""
    assert "secret-marker" not in captured.out
    assert "synthetic-credential" not in captured.out


def test_safe_protocol_accepts_complete_boundary_list_but_never_exception_text():
    ids = list(range(1, 500)) + [2**53 - 1]
    error = LiveInputError(
        "secret-marker", code="unselected_active_demand",
        details={"missing_sales_order_ids": ids},
    )
    protocol = tool._live_input_failure(error)
    assert protocol["details"]["missing_sales_order_ids"] == ids
    assert protocol["details"]["missing_sales_order_ids"] is not ids
    assert "secret-marker" not in json.dumps(protocol)
    assert len(json.dumps(protocol).encode("utf-8")) <= 16 * 1024


def test_cli_success_structure_and_saved_artifacts_are_unchanged(
    synthetic_cli, monkeypatch, capsys,
):
    result = {
        "schema_version": 1, "odoo_writes": False,
        "plan": {"objective_value": 7, "expected_new_spend": 25},
    }
    blueprints = []

    def solve(blueprint, upstream):
        blueprints.append(blueprint)
        return result

    monkeypatch.setattr(tool, "solve_blueprint", solve)
    assert tool.main(synthetic_cli.argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": True, "odoo_writes": False,
        "plan_path": str(synthetic_cli.output / "plan.json"),
        "objective_value": 7, "expected_new_spend": 25,
    }
    assert len(blueprints) == 1
    assert [path.name for path, _ in synthetic_cli.saved] == [
        "request.json", "request.json", "snapshot.json", "snapshot.json",
        "blueprint.json", "blueprint.json", "plan.json", "plan.json",
    ]
    assert synthetic_cli.saved[-1][1] == result


def run_export(source: Path, destination: Path) -> None:
    pytest.importorskip("harbor")
    from forgeflow.agents.pi_nebius import _build_log_export_command

    subprocess.run(
        ["bash", "-c", _build_log_export_command(str(source), str(destination))],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=True,
        capture_output=True,
        text=True,
    )


def test_planning_export_accepts_only_allowlisted_regular_files_and_attempt_directories(tmp_path):
    source = tmp_path / "private logs"
    destination = tmp_path / "harbor logs"
    planning = source / "planning"
    attempt = planning / ("a" * 32)
    attempt.mkdir(parents=True)
    destination.mkdir()
    regular = {"request.json": '{"request":1}\n', "snapshot.json": '{"snapshot":1}\n'}
    for name, text in regular.items():
        (attempt / name).write_text(text)
    private = tmp_path / "private"
    private.mkdir()
    (private / "request.json").write_text("not an allowed artifact")
    (attempt / "blueprint.json").symlink_to(private / "request.json")
    (attempt / "plan.json").mkdir()
    (attempt / "plan.json" / "nested.json").write_text("not a regular file")
    (attempt / "unknown.json").write_text("not an allowlisted name")
    (planning / ("b" * 32)).symlink_to(private, target_is_directory=True)
    for invalid_name in ("not-a-uuid", "C" * 32):
        directory = planning / invalid_name
        directory.mkdir()
        (directory / "request.json").write_text("not a valid attempt")
    run_export(source, destination)
    exported = {
        path.relative_to(destination).as_posix(): path.read_text()
        for path in destination.rglob("*") if path.is_file()
    }
    assert exported == {f"planning/{'a' * 32}/{name}": text for name, text in regular.items()}
    assert (private / "request.json").read_text() == "not an allowed artifact"


def test_planning_export_rejects_symlinked_root_and_oversized_file(tmp_path):
    source = tmp_path / "logs"
    destination = tmp_path / "export"
    source.mkdir()
    destination.mkdir()
    private = tmp_path / "private" / ("d" * 32)
    private.mkdir(parents=True)
    (private / "request.json").write_text("must not be exported")
    (source / "planning").symlink_to(private.parent, target_is_directory=True)
    run_export(source, destination)
    assert list(destination.iterdir()) == []
    (source / "planning").unlink()
    attempt = source / "planning" / ("e" * 32)
    attempt.mkdir(parents=True)
    with (attempt / "snapshot.json").open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)
    (attempt / "request.json").write_text("{}\n")
    run_export(source, destination)
    assert (destination / "planning" / ("e" * 32) / "request.json").read_text() == "{}\n"
    assert not (destination / "planning" / ("e" * 32) / "snapshot.json").exists()
