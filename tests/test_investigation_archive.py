"""The review archive preserves evidence and excludes credentials by construction."""
import gzip
import hashlib
import importlib.util
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from forgeflow.investigation.fixtures import supplier_cancellation_control
from forgeflow.investigation.rules import run_rules
from forgeflow.recovery.storage import digest

SPEC = importlib.util.spec_from_file_location(
    "investigation_archive", Path(__file__).resolve().parents[1] / "scripts/investigation_archive.py")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


def episode(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text('{"status": "technical_failure", "tokens": null}\n')
    (run / "pi.jsonl").write_text('{"original": "unchanged"}\n')
    (run / "private-inputs").mkdir()
    (run / "private-inputs/bridge.json").write_text('{"token": "private-bearer-credential"}')
    return run


def test_archive_preserves_member_bytes_and_never_includes_private_inputs(tmp_path):
    run = episode(tmp_path)
    result = archive.export_episode(run, tmp_path / "export")
    packed = tmp_path / "export/episode.tar.gz"
    assert hashlib.sha256(packed.read_bytes()).hexdigest() == result["archive_sha256"]
    with tarfile.open(packed) as tar:
        assert set(tar.getnames()) == {"summary.json", "pi.jsonl"}
        for item in result["members"]:
            actual = tar.extractfile(item["path"]).read()
            assert actual == (run / item["path"]).read_bytes()
            assert hashlib.sha256(actual).hexdigest() == item["sha256"]
    saved = json.loads((tmp_path / "export/index.json").read_text())
    assert saved["summary"]["tokens"] is None
    with pytest.raises(ValueError, match="already_exists"):
        archive.export_episode(run, tmp_path / "export")


def test_archive_rejects_known_credential_without_writing_partial_export(tmp_path):
    run = episode(tmp_path)
    (run / "pi.jsonl").write_text("accidentally logged a private-bearer-credential")
    with pytest.raises(ValueError, match="credential_found"):
        archive.export_episode(run, tmp_path / "export", secrets=(b"private-bearer-credential",))
    assert not (tmp_path / "export").exists()


def test_archive_rejects_allowlisted_symlink_escape(tmp_path):
    run = episode(tmp_path)
    (tmp_path / "outside").write_text("sensitive")
    (run / "trace.json").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="outside_episode"):
        archive.export_episode(run, tmp_path / "export")
    assert not (tmp_path / "export").exists()


def test_archive_rejects_nested_bundle_credentials(tmp_path):
    run = episode(tmp_path)
    (run / "trusted").mkdir()
    path = run / "trusted/bundle.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        data = b"credential-to-exclude"
        member = tarfile.TarInfo("planner/config.txt")
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError, match="credential_found"):
        archive.export_episode(run, tmp_path / "export", secrets=(b"credential-to-exclude",))
    assert not (tmp_path / "export").exists()


def test_archive_rejects_local_host_paths_without_rewriting_bound_evidence(tmp_path):
    run = episode(tmp_path)
    original = b'{"path": "/Users/example/private/work"}'
    (run / "trace.json").write_bytes(original)
    with pytest.raises(ValueError, match="private_host_path"):
        archive.export_episode(run, tmp_path / "export")
    assert (run / "trace.json").read_bytes() == original
    assert not (tmp_path / "export").exists()


@pytest.fixture(scope="module")
def real_prepared(tmp_path_factory):
    path = Path(__file__).resolve().parents[1] / "scripts/investigation_evaluate.py"
    spec = importlib.util.spec_from_file_location("archive_real_episode", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    output = tmp_path_factory.mktemp("archive-real-prepare") / "episode"
    case = supplier_cancellation_control()
    control = runner.Episode(case, "investigation", output, 600, 2000000)
    run_rules(case.event, control.dispatch)
    report = control.report()
    assert report["status"] == "prepared" and report["score"] == 1
    return output


def clone_prepared(real_prepared, tmp_path):
    root = tmp_path / "episode"
    root.mkdir()
    for path in archive.selected_files(real_prepared):
        destination = root / path.relative_to(real_prepared)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    return root


def load(path):
    return json.loads(path.read_bytes())


def save(path, value):
    path.write_text(json.dumps(value) + "\n")


def test_real_prepared_archive_preserves_all_original_bytes_and_bound_report(real_prepared, tmp_path):
    result = archive.export_episode(real_prepared, tmp_path / "export")
    assert result["prepared_claim_binding_verified"] is True
    assert result["grading_status"] == "recorded"
    with tarfile.open(tmp_path / "export/episode.tar.gz") as packed:
        for row in result["members"]:
            assert packed.extractfile(row["path"]).read() == (real_prepared / row["path"]).read_bytes()
    assert "外层评测未合格" not in (tmp_path / "export/review.md").read_text()


@pytest.mark.parametrize("name", archive.ARTIFACT_NAMES)
def test_prepared_archive_rejects_each_changed_bound_artifact(real_prepared, tmp_path, name):
    root = clone_prepared(real_prepared, tmp_path)
    path = next((root / "trusted/store").glob("*/manifest.json")).parent / (name + ".json")
    value = load(path)
    value["tampered"] = True
    save(path, value)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="artifact_binding"):
        archive.export_episode(root, tmp_path / "export")
    assert path.read_bytes() == before
    assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("mutation", ["missing", "identity", "writes", "ok"])
def test_prepared_archive_requires_the_real_completion_marker(real_prepared, tmp_path, mutation):
    root = clone_prepared(real_prepared, tmp_path)
    path = next((root / "trusted/store").glob("*/prepared.json"))
    if mutation == "missing":
        path.unlink()
    else:
        value = load(path)
        value[{"identity": "plan_id", "writes": "odoo_writes", "ok": "ok"}[mutation]] = {
            "identity": "0" * 64, "writes": True, "ok": False}[mutation]
        save(path, value)
    with pytest.raises(ValueError, match="prepared_(artifact_missing|completion_marker)"):
        archive.export_episode(root, tmp_path / "export")
    assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("field,value", [("case_id", "unrelated-case"), ("actual_status", "technical_failure"),
                                         ("score", 0)])
def test_archive_rejects_foreign_or_stale_grading(real_prepared, tmp_path, field, value):
    root = clone_prepared(real_prepared, tmp_path)
    path = root / "business-grade.json"
    grade = load(path)
    grade[field] = value
    save(path, grade)
    with pytest.raises(ValueError, match="summary_grade_binding"):
        archive.export_episode(root, tmp_path / "export")
    assert not (tmp_path / "export").exists()


def test_summary_terminal_plan_must_match_the_prepared_state(real_prepared, tmp_path):
    root = clone_prepared(real_prepared, tmp_path)
    path = root / "summary.json"
    summary = load(path)
    summary["terminal"]["prepared"]["plan_id"] = "0" * 64
    save(path, summary)
    with pytest.raises(ValueError, match="plan_identity_mismatch"):
        archive.export_episode(root, tmp_path / "export")


def test_rehashed_artifacts_cannot_be_attached_to_a_review_of_another_request(real_prepared, tmp_path):
    root = clone_prepared(real_prepared, tmp_path)
    manifest_path = next((root / "trusted/store").glob("*/manifest.json"))
    directory = manifest_path.parent
    request = load(directory / "request.json")
    request["sales_order_budget_caps"]["1"] = 999
    save(directory / "request.json", request)
    manifest = load(manifest_path)
    manifest["artifacts"]["request"] = digest(request)
    save(manifest_path, manifest)
    new_id = digest(manifest)
    prepared = load(directory / "prepared.json")
    prepared["plan_id"] = new_id
    save(directory / "prepared.json", prepared)
    directory.rename(directory.with_name(new_id))
    investigation = root / "trusted/store/investigation"
    review = load(investigation / "review.json")
    review["plan_id"] = new_id
    save(investigation / "review.json", review)
    state = load(investigation / "state.json")
    state["prepared"].update(plan_id=new_id, review_sha256=digest(review))
    save(investigation / "state.json", state)
    summary = load(root / "summary.json")
    summary["terminal"]["prepared"] = state["prepared"]
    save(root / "summary.json", summary)
    with pytest.raises(ValueError, match="request_binding_mismatch"):
        archive.export_episode(root, tmp_path / "export")
    assert not (tmp_path / "export").exists()


def test_outer_budget_failure_remains_explicit_when_service_prepared(real_prepared, tmp_path):
    root = clone_prepared(real_prepared, tmp_path)
    summary = load(root / "summary.json")
    summary.update(status="budget_exhausted", service_status="prepared", score=0)
    save(root / "summary.json", summary)
    grade = load(root / "business-grade.json")
    grade.update(actual_status="budget_exhausted", score=0)
    save(root / "business-grade.json", grade)
    originals = {str(path.relative_to(root)): path.read_bytes() for path in archive.selected_files(root)}
    index = archive.export_episode(root, tmp_path / "export")
    report = (tmp_path / "export/review.md").read_text()
    assert "外层评测未合格" in report and "budget_exhausted" in report
    assert index["summary"]["status"] == "budget_exhausted"
    assert index["business_grade"]["score"] == 0
    with tarfile.open(tmp_path / "export/episode.tar.gz") as packed:
        for name, data in originals.items():
            assert packed.extractfile(name).read() == data
            assert (root / name).read_bytes() == data


def test_legacy_without_grade_is_explicitly_raw_unscored_evidence(real_prepared, tmp_path):
    root = clone_prepared(real_prepared, tmp_path)
    (root / "business-grade.json").unlink()
    index = archive.export_episode(root, tmp_path / "export")
    assert index["business_grade"] is None
    assert index["grading_status"] == "not_available"
    assert "仅归档原始证据" in (tmp_path / "export/review.md").read_text()


def test_launcher_failure_keeps_interrupted_grade_as_unmodified_separate_evidence(real_prepared, tmp_path):
    root = clone_prepared(real_prepared, tmp_path)
    original_grade = (root / "business-grade.json").read_bytes()
    (root / "interrupted-business-grade.json").write_bytes(original_grade)
    grade = load(root / "business-grade.json")
    grade.update(actual_status="technical_failure", score=0, grading_method="launcher_failure_policy")
    save(root / "business-grade.json", grade)
    summary = load(root / "summary.json")
    summary.update(status="technical_failure", service_status=None, terminal={}, score=0,
                   grading_method="launcher_failure_policy")
    save(root / "summary.json", summary)
    index = archive.export_episode(root, tmp_path / "export")
    assert index["business_grade"]["score"] == 0
    with tarfile.open(tmp_path / "export/episode.tar.gz") as packed:
        assert packed.extractfile("interrupted-business-grade.json").read() == original_grade
    assert "外层评测未合格" in (tmp_path / "export/review.md").read_text()


def bundle(run, name, data):
    (run / "trusted").mkdir(exist_ok=True)
    with tarfile.open(run / "trusted/bundle.tar.gz", "w:gz") as packed:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        packed.addfile(member, io.BytesIO(data))


def test_nested_member_names_are_scanned_for_credentials(tmp_path):
    root = episode(tmp_path)
    bundle(root, "planner/secret-credential-value.py", b"safe body")
    with pytest.raises(ValueError, match="credential_found"):
        archive.export_episode(root, tmp_path / "export", secrets=(b"secret-credential-value",))
    assert not (tmp_path / "export").exists()


def test_nested_member_names_cannot_contain_private_host_paths(tmp_path):
    root = episode(tmp_path)
    bundle(root, "planner/copy/Users/example/private/source.py", b"safe body")
    with pytest.raises(ValueError, match="private_host_path"):
        archive.export_episode(root, tmp_path / "export")
    assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("filename", ["planner/extra.tar.gz", "planner/innocent.txt"])
def test_second_archive_is_rejected_even_when_renamed(tmp_path, filename):
    root = episode(tmp_path)
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w:gz") as nested:
        data = b"secret-credential-value"
        member = tarfile.TarInfo("config.txt")
        member.size = len(data)
        nested.addfile(member, io.BytesIO(data))
    bundle(root, filename, inner.getvalue())
    with pytest.raises(ValueError, match="nested_archive_not_allowed"):
        archive.export_episode(root, tmp_path / "export", secrets=(b"secret-credential-value",))
    assert not (tmp_path / "export").exists()


def test_trailing_expanded_bundle_bytes_cannot_hide_a_credential(tmp_path):
    root = episode(tmp_path)
    bundle(root, "planner/source.py", b"benign")
    path = root / "trusted/bundle.tar.gz"
    path.write_bytes(gzip.compress(gzip.decompress(path.read_bytes()) + b"secret-credential-value"))
    with pytest.raises(ValueError, match="credential_found"):
        archive.export_episode(root, tmp_path / "export", secrets=(b"secret-credential-value",))


def test_bundle_expansion_is_bounded_before_member_reads(tmp_path, monkeypatch):
    root = episode(tmp_path)
    assert archive.MAX_BUNDLE_BYTES == 32 * 1024 * 1024
    monkeypatch.setattr(archive, "MAX_BUNDLE_BYTES", 1024)
    bundle(root, "planner/source.py", b"a" * 2048)
    with pytest.raises(ValueError, match="invalid_nested_bundle"):
        archive.export_episode(root, tmp_path / "export")
    assert not (tmp_path / "export").exists()
