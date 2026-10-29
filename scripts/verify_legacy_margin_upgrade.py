#!/usr/bin/env python3
"""Offline upgrade rehearsal using actual c42063a code and synthetic ERP only.

An isolated old-code subprocess creates its own bundle, unmarked manifest,
approval and journal. Current code receives those unchanged artifacts and the
serialized Fake ERP state. No Docker, network, production ERP, browser approval
or original benchmark scoring is involved. Old evidence is retained in workspace;
the public report contains hashes and bounded assertions only.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD_COMMIT = "c42063a95f6bb7104d1c37e3e0be83d843f3b112"
MODES = ("unstarted", "consumed_no_dispatch", "pending", "completed_prefix", "completed", "invalid_margin")


def need(condition, code):
    if not condition:
        raise RuntimeError(code)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def connection_state(connection):
    return {"db": connection.db, "columns": connection.columns, "writes": connection.writes}


def restore_connection(connection, state):
    connection.db = {model: {int(key): row for key, row in rows.items()}
                     for model, rows in state["db"].items()}
    connection.columns, connection.writes = state["columns"], state["writes"]
    connection.fail = None


def _old_worker(config):
    # The parent passes -I and inserts only the archived tree before executing
    # this driver, so every forgeflow and synthetic-test import below is old code.
    import pytest
    import test_product_versions as product
    import test_recovery_service as recovery

    from forgeflow.recovery import scope_guard, service
    from forgeflow.recovery.journal import Journal
    from forgeflow.recovery.readback import ReadbackAdapter
    from forgeflow.recovery.storage import read_json

    need(Path(service.__file__).resolve().is_relative_to(Path(config["old_root"])), "old_import_not_isolated")
    root = Path(config["workspace"])
    with pytest.MonkeyPatch.context() as patch:
        system = recovery.system.__wrapped__(patch, root)
        connection, request, options, _ = system
        if config["mode"] == "invalid_margin":
            from recovery_synthetic import Fake
            request, snapshot, _, plan = json.loads(Path(config["fixture"]).read_text())
            connection.__dict__.update(Fake(snapshot).__dict__)
            connection.writes, connection.fail = [], None
            patch.setattr(service, "collect_snapshot", lambda *args: copy.deepcopy(snapshot))
            patch.setattr(service, "solve_blueprint", lambda *args: copy.deepcopy(plan))
        contract = recovery._contract(margin=request["margin"])
        decision = service.prepare_product(connection, request, run_id=product.RUN, owner_sha256=product.OWNER,
            expected_revision=0, operation_id="prepare-1", contract=contract, **options)
        plan_id = decision["selected_plan_id"]
        grant = product.grant_from(product.authorize(options, decision)["approval"])
        mode = config["mode"]
        old_apply, old_append, consume = ReadbackAdapter.apply, Journal.append, service._consume_grant

        def apply_then_stop(adapter, step):
            old_apply(adapter, step)
            if connection.writes:
                raise KeyboardInterrupt("controlled committed response loss")

        def append_then_stop(journal, event, data):
            old_append(journal, event, data)
            if event == "verified":
                raise KeyboardInterrupt("controlled verified prefix")

        def consume_then_stop(*args, **kwargs):
            consume(*args, **kwargs)
            raise KeyboardInterrupt("controlled consumed approval")

        if mode in {"pending", "invalid_margin"}:
            patch.setattr(ReadbackAdapter, "apply", apply_then_stop)
        elif mode == "completed_prefix":
            patch.setattr(Journal, "append", append_then_stop)
        elif mode == "consumed_no_dispatch":
            patch.setattr(service, "_consume_grant", consume_then_stop)
        if mode != "unstarted":
            try:
                service.apply(connection, plan_id, grant=grant, **options)
            except KeyboardInterrupt:
                need(mode != "completed", "completed_interrupted")
            else:
                need(mode == "completed", "fault_did_not_interrupt")
        manifest = read_json(options["store"] / plan_id / "manifest.json")
        need("margin_basis" not in manifest, "old_manifest_has_new_marker")
        state = {"plan_id": plan_id, "grant": grant.approval.payload(), "request": request,
                 "connection": connection_state(connection), "decision": decision,
                 "service_file_sha256": sha(Path(service.__file__)),
                 "change_set_file_sha256": sha(Path(scope_guard.__file__).with_name("change_set.py"))}
        json_write(root / "old-state.json", state)


def _rollback_worker(config):
    import pytest
    import test_product_versions as product
    import test_recovery_service as recovery

    from forgeflow.recovery import service
    from forgeflow.recovery.storage import digest, read_json

    root, state = Path(config["workspace"]), json.loads(Path(config["state"]).read_text())
    with pytest.MonkeyPatch.context() as patch:
        # Build the authentic old runtime in a separate directory; retained state
        # is only read. No invocation of old apply is permitted for an old plan.
        system = recovery.system.__wrapped__(patch, root)
        connection, _, options, _ = system
        restore_connection(connection, state["connection"])
        historical = Path(config["historical"])
        manifest = read_json(historical / "manifest.json")
        need(digest(manifest) == state["plan_id"], "rollback_history_changed")
        writes = list(connection.writes)
        try:
            service.apply(connection, config["new_plan_id"], store=Path(config["store"]),
                          bundle=options["bundle"], grant=product.grant_from(state["grant"]))
        except service.RecoveryError as error:
            need(str(error) == "original_source_binding_changed", "rollback_wrong_rejection")
        else:
            raise RuntimeError("rollback_dispatched_current_plan")
        need(connection.writes == writes, "rollback_wrote_business_state")
        json_write(root / "rollback.json", {"historical_read_only": True,
                   "current_plan_rejected": "original_source_binding_changed", "additional_writes": 0})


def archived_call(old_root, worker, config):
    script = ("import json,runpy,sys; sys.path[:0]=sys.argv[1:3]; "
              "ns=runpy.run_path(sys.argv[3],run_name='upgrade_worker'); "
              "ns[sys.argv[4]](json.loads(sys.argv[5]))")
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script,
        str(old_root / "src"), str(old_root / "tests"), str(Path(__file__).resolve()), worker,
        json.dumps({**config, "old_root": str(old_root)})], cwd=old_root,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"old_subprocess_failed: {result.stderr[-4000:]}")


def run(output, workspace, source_commit):
    import pytest
    import test_product_versions as product
    import test_recovery_service as recovery
    from test_recovery_change_set import stock_margin_fixture

    from forgeflow.recovery import compatibility, service
    from forgeflow.recovery.margin import MARGIN_BASIS
    from forgeflow.recovery.storage import digest, read_json

    output.mkdir(parents=True)
    workspace.mkdir(parents=True)
    old_root = workspace / "old-source"
    old_root.mkdir()
    archive = subprocess.run(["git", "archive", OLD_COMMIT], cwd=ROOT,
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as handle:
        handle.extractall(old_root, filter="data")
    # Generic upstream dependencies are intentionally untracked. Both builders
    # verify this copy against the pinned provenance before using any byte.
    (old_root / "vendor").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "vendor/erp-bench.source.json", old_root / "vendor/erp-bench.source.json")
    provenance = json.loads((old_root / "vendor/erp-bench.source.json").read_text())
    from forgeflow.planning.bundle import UPSTREAM_FILES

    for relative in UPSTREAM_FILES:
        source, destination = ROOT / "vendor/erp-bench" / relative, old_root / "vendor/erp-bench" / relative
        need(sha(source) == provenance["files"][relative]["sha256"], "upstream_dependency_changed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    invalid = workspace / "invalid-fixture.json"
    json_write(invalid, stock_margin_fixture(unit_cost=76, floor=0.5))
    report = {"category": "actual_previous_release_artifacts_synthetic_erp_upgrade",
              "old_commit": OLD_COMMIT, "old_archive_sha256": hashlib.sha256(archive).hexdigest(),
              "source_commit": subprocess.run(["git", "rev-parse", source_commit], cwd=ROOT,
                  check=True, capture_output=True, text=True).stdout.strip(),
              "upstream_commit": provenance["commit"],
              "driver_sha256": sha(Path(__file__)),
              "real_erp": False, "original_benchmark_run": False,
              "rollback_policy": "historical read-only; no execution of old plans under old code",
              "stages": []}
    json_write(output / "report.json", report)
    for mode in MODES:
        directory = workspace / mode
        directory.mkdir()
        archived_call(old_root, "_old_worker", {"workspace": str(directory), "mode": mode,
                                                  "fixture": str(invalid)})
        state = read_json(directory / "old-state.json")
        plan_id = state["plan_id"]
        original_dir = directory / "state" / plan_id
        originals = {str(path.relative_to(directory)): sha(path)
                     for path in original_dir.rglob("*") if path.is_file() and path.name != "journal.jsonl"}
        originals.update({str(path.relative_to(directory)): sha(path)
                          for path in (directory / "approvals").glob("*") if path.is_file()})
        journal = original_dir / "journal.jsonl"
        prefix = journal.read_bytes() if journal.exists() else None
        old_manifest = read_json(original_dir / "manifest.json")
        need(digest(old_manifest) == plan_id and "margin_basis" not in old_manifest, "old_manifest_invalid")
        stage = {"mode": mode, "old_plan_id": plan_id, "original_hashes": originals,
                 "original_journal_sha256": hashlib.sha256(prefix).hexdigest() if prefix else None,
                 "old_service_sha256": state["service_file_sha256"],
                 "old_change_set_sha256": state["change_set_file_sha256"]}
        current = directory / "current-runtime"
        current.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            system = recovery.system.__wrapped__(patch, current)
            connection, _, options, current_record = system
            if "current_bundle_sha256" not in report:
                report["current_bundle_sha256"] = digest(current_record)
            need(digest(current_record) == report["current_bundle_sha256"], "current_source_changed")
            restore_connection(connection, state["connection"])
            options["store"] = directory / "state"
            grant = product.grant_from(state["grant"])
            before = list(connection.writes)
            review = compatibility.review_legacy_plan(plan_id, **options)
            stage.update(review_state=review["state"], margin_basis=review["margin_basis"],
                         review_id=review["review_id"], current_sources_sha256=review["current_sources_sha256"],
                         original_sources_sha256=review["original_sources_sha256"])
            if mode in {"unstarted", "consumed_no_dispatch"}:
                need(review["state"] == "requires_reprepare", "unstarted_not_retired")
                retired = compatibility.retire_legacy_unstarted(plan_id, approver=product.ACTOR, **options)
                fresh = service.prepare_product(connection, state["request"], run_id=product.RUN,
                    owner_sha256=product.OWNER, expected_revision=retired["revision"],
                    operation_id="prepare-corrected", contract=recovery._contract(), **options)
                fresh_id = fresh["selected_plan_id"]
                need(read_json(options["store"] / fresh_id / "manifest.json")["margin_basis"] == MARGIN_BASIS,
                     "fresh_plan_basis_missing")
                stage["fresh_plan_id"] = fresh_id
                rollback = directory / "rollback-runtime"
                rollback.mkdir()
                archived_call(old_root, "_rollback_worker", {"workspace": str(rollback),
                    "state": str(directory / "old-state.json"), "historical": str(original_dir),
                    "store": str(options["store"]), "new_plan_id": fresh_id})
                stage["rollback"] = read_json(rollback / "rollback.json")
            elif mode in {"pending", "completed_prefix"}:
                need(review["state"] == "requires_recovery_authorization", "partial_not_resumable")
                token = compatibility.authorize_legacy_resume(plan_id, review["review_id"],
                    approver=product.ACTOR, **options)
                result = service.apply(connection, plan_id, grant=grant, legacy_authorization=token, **options)
                need(result["execution_validated"] is True, "resumed_execution_unverified")
                need(len(connection.writes) == len(set(connection.writes)), "duplicate_execution")
                stage.update(execution_validated=True, completed_steps_before=review["completed_steps"],
                             pending_dispatch_before=review["pending_dispatch"],
                             independently_checked_margin=review["checks"]["combined_new_spend_margin"])
            else:
                expected = "completed_history" if mode == "completed" else "recovery_blocked"
                need(review["state"] == expected, "history_or_invalid_margin_not_blocked")
                try:
                    compatibility.authorize_legacy_resume(plan_id, review["review_id"],
                        approver=product.ACTOR, **options)
                except service.RecoveryError as error:
                    need(str(error) == "legacy_recovery_not_resumable", "unexpected_rejection")
                else:
                    raise RuntimeError("history_or_invalid_margin_authorized")
                if mode == "invalid_margin":
                    stage.update(old_reported_margin=read_json(original_dir / "change-set.json")
                        ["projected_state"]["checks"]["combined_new_spend_margin"],
                        corrected_revenue="500", corrected_cost="400", corrected_margin=0.2, required_margin=0.5)
            if mode not in {"pending", "completed_prefix"}:
                need(connection.writes == before, "unexpected_upgrade_writes")
            need(all(sha(directory / name) == value for name, value in originals.items()), "original_evidence_changed")
            if prefix is not None:
                need(journal.read_bytes().startswith(prefix), "original_journal_rewritten")
            stage.update(original_bytes_preserved=True, journal_prefix_preserved=True,
                         additional_fake_writes=len(connection.writes) - len(before))
        report["stages"].append(stage)
        json_write(output / "report.json", report)
        print(json.dumps({key: stage[key] for key in ("mode", "review_state", "original_bytes_preserved",
                                                     "additional_fake_writes")}), flush=True)
    report["result"] = "passed"
    need(sha(Path(__file__)) == report["driver_sha256"], "acceptance_driver_changed")
    json_write(output / "report.json", report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--source-commit", default="HEAD", help="release reference; exact bundle hash is also recorded")
    args = parser.parse_args()
    if args.output.exists() or args.workspace.exists():
        parser.error("preserve prior evidence; output and workspace must be new")
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    try:
        run(args.output.resolve(), args.workspace.resolve(), args.source_commit)
    except Exception as error:
        report_path = args.output / "report.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text())
            report.update(result="failed", error=type(error).__name__)
            json_write(report_path, report)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
