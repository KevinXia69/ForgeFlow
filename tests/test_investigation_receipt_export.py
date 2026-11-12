"""Only state-bound, digest-checked query pages survive export/archive."""
import json
import shutil
import tarfile

import pytest
from test_investigation_archive import archive
from test_investigation_service import RUN_ID, setup_service
from test_pi_nebius import _recovery_export_fixture, _run_recovery_export

from forgeflow.recovery.storage import digest, read_json


def page_fixture(directory):
    directory.mkdir(parents=True)
    page = {"status": "investigating", "records": [{"name": "长文本"}], "complete": True}
    identity = digest("operation-one")
    filename = "query-page-" + identity + ".json"
    (directory / filename).write_text(json.dumps(page, ensure_ascii=True) + "\n")
    (directory / "state.json").write_text(json.dumps({"query_receipts": {
        identity: {"request_sha256": digest({}), "response_sha256": digest(page)}}}))
    orphan = "query-page-" + "f" * 64 + ".json"
    (directory / orphan).write_text('{"must_not_be_exported":true}')
    return filename, orphan


def test_production_export_uses_committed_receipt_and_preserves_raw_bytes(tmp_path):
    source, destination, _ = _recovery_export_fixture(tmp_path)
    directory = source / "recovery/investigation"
    filename, orphan = page_fixture(directory)
    report = _run_recovery_export(source, destination)
    assert report["complete"] is True
    assert (destination / "recovery/investigation" / filename).read_bytes() == (directory / filename).read_bytes()
    assert not (destination / "recovery/investigation" / orphan).exists()


@pytest.mark.parametrize("damage", ["changed", "missing", "symlink"])
def test_production_export_reports_corrupt_or_missing_committed_page(tmp_path, damage):
    source, destination, _ = _recovery_export_fixture(tmp_path)
    directory = source / "recovery/investigation"
    filename, _ = page_fixture(directory)
    page = directory / filename
    if damage == "changed":
        page.write_text('{"changed":true}')
    else:
        page.unlink()
        if damage == "symlink":
            page.symlink_to(directory / "state.json")
    report = _run_recovery_export(source, destination)
    assert report["complete"] is False
    assert not (destination / "recovery/investigation" / filename).exists()


@pytest.fixture
def receipt_episode(tmp_path):
    service, _, _ = setup_service(tmp_path / "service")
    service.dispatch(RUN_ID, "query", {"kind": "policy", "arguments": {}, "operation_id": "page-one"})
    service.dispatch(RUN_ID, "finish", {"status": "technical_failure", "reason": "test_stop", "question": ""})
    run = tmp_path / "run"
    shutil.copytree(service.store, run / "trusted/store")
    (run / "summary.json").write_text(json.dumps({"status": "technical_failure", "tokens": None}))
    return run


def test_archive_checks_the_exact_captured_receipt_bytes(receipt_episode, tmp_path):
    directory = receipt_episode / "trusted/store/investigation"
    identity, receipt = next(iter(read_json(directory / "state.json")["query_receipts"].items()))
    filename = "query-page-" + identity + ".json"
    orphan = directory / ("query-page-" + "f" * 64 + ".json")
    orphan.write_text('{"private_orphan":true}')
    archive.export_episode(receipt_episode, tmp_path / "archive")
    with tarfile.open(tmp_path / "archive/episode.tar.gz") as tar:
        name = "trusted/store/investigation/" + filename
        raw = tar.extractfile(name).read()
        assert raw == (directory / filename).read_bytes()
        assert digest(json.loads(raw)) == receipt["response_sha256"]
        assert "trusted/store/investigation/" + orphan.name not in tar.getnames()


@pytest.mark.parametrize("damage", ["changed", "missing", "symlink"])
def test_archive_rejects_bad_receipt_without_creating_archive(receipt_episode, tmp_path, damage):
    page = next((receipt_episode / "trusted/store/investigation").glob("query-page-*.json"))
    if damage == "changed":
        page.write_text('{"changed":true}')
    else:
        page.unlink()
        if damage == "symlink":
            page.symlink_to(receipt_episode / "summary.json")
    with pytest.raises(ValueError, match="receipt|outside_episode"):
        archive.export_episode(receipt_episode, tmp_path / "archive")
    assert not (tmp_path / "archive").exists()
