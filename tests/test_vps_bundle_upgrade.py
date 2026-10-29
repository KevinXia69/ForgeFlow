"""Actual previous-release bundles, fake Docker, and explicit root-layout simulation."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from test_vps_provision import invoke, provisioner, write_json
from test_vps_provision import sandbox as provision_sandbox

from forgeflow import vps_bundle, vps_odoo
from forgeflow.planning.bundle import UPSTREAM_FILES, build_planner_bundle
from forgeflow.recovery.service import RecoveryError, verify_sources
from forgeflow.recovery.storage import digest, read_json
from forgeflow.vps_runtime import configuration, executor_command

ROOT = Path(__file__).resolve().parents[1]
OLD_COMMIT = "c42063a95f6bb7104d1c37e3e0be83d843f3b112"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    return provision_sandbox.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture(scope="module")
def release_bundles(tmp_path_factory):
    directory = tmp_path_factory.mktemp("release-bundles")
    old = directory / "old-source"
    old.mkdir()
    archive = subprocess.run(["git", "archive", OLD_COMMIT], cwd=ROOT,
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as handle:
        handle.extractall(old, filter="data")
    (old / "vendor").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "vendor/erp-bench.source.json", old / "vendor/erp-bench.source.json")
    for name in UPSTREAM_FILES:
        target = old / "vendor/erp-bench" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "vendor/erp-bench" / name, target)
    old_tar = directory / "old.tar.gz"
    script = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "from forgeflow.planning import bundle; "
        "assert Path(bundle.__file__).is_relative_to(Path(sys.argv[1])); "
        "bundle.build_planner_bundle(Path(sys.argv[2]),Path(sys.argv[3]),recovery=True)"
    )
    subprocess.run([sys.executable, "-I", "-B", "-c", script, str(old / "src"), str(old), str(old_tar)],
                   cwd=old, check=True, capture_output=True)
    current_tar = directory / "current.tar.gz"
    build_planner_bundle(ROOT, current_tar, recovery=True)
    return old_tar, current_tar


@pytest.fixture
def upgrade(sandbox, release_bundles, monkeypatch):
    old_tar, current_tar = release_bundles
    with tarfile.open(old_tar) as handle:
        handle.extractall(sandbox.root, filter="data")
    release = sandbox.root.parent / "releases/current"
    release.mkdir(parents=True)
    with tarfile.open(current_tar) as handle:
        handle.extractall(release, filter="data")
    sandbox.active = release / "planner"
    sandbox.workbench._prepare_local_policy()
    sandbox.workbench._write_request(None)
    sandbox.record.update(phase="ready", bindings=provisioner.artifact_bindings(sandbox.root))
    write_json(sandbox.root / "vps-instance.json", sandbox.record)
    sandbox.config["active_bundle"] = str(sandbox.active)
    sandbox.container["State"]["Running"] = True
    monkeypatch.setattr(vps_odoo, "load_state", lambda *_args: ({}, False))
    sandbox.workbench._valid_planner = lambda _path: False
    sandbox.workbench._planner = lambda: pytest.fail("ready instance must never rebuild planner")
    # Files are owned by the test runner on macOS. Model a root-owned /var/lib
    # tree while preserving actual permissions/link counts inside this fixture.
    # Production always uses os.lstat and requires UID 0 at every ancestor.
    sandbox.owners = {}
    original_stat = vps_bundle._lstat

    def root_layout(path):
        info = list(original_stat(path))
        info[4] = sandbox.owners.get(Path(path), 0)
        if not Path(path).is_relative_to(sandbox.root.parent):
            info[0] &= ~0o022
        return os.stat_result(info)

    monkeypatch.setattr(vps_bundle, "_lstat", root_layout)
    return sandbox


def bytes_under(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def rewrite_provenance(bundle, name):
    provenance = read_json(bundle / "provenance.json")
    provenance["files_sha256"][name] = hashlib.sha256((bundle / name).read_bytes()).hexdigest()
    write_json(bundle / "provenance.json", provenance)


def test_actual_c420_bundle_upgrade_preserves_bound_history_and_data(upgrade):
    old = upgrade.root / "planner"
    provenance = read_json(old / "provenance.json")
    assert "forgeflow/recovery/margin.py" not in provenance["files_sha256"]
    actual_old = subprocess.run(["git", "show", f"{OLD_COMMIT}:src/forgeflow/recovery/service.py"],
                                cwd=ROOT, capture_output=True, check=True).stdout
    assert (old / "forgeflow/recovery/service.py").read_bytes() == actual_old
    with pytest.raises(RecoveryError, match="incomplete_source_manifest"):
        verify_sources(old)
    old_bytes = bytes_under(old)
    for name in ("store/old/manifest.json", "store/old/journal.jsonl", "approvals/old.json",
                 "odoo/business-state.json"):
        write_json(upgrade.root / name, {"retained": name})
    protected = {name: (upgrade.root / name).read_bytes() for name in (
        "task-policy.json", "request.json", "store/old/manifest.json", "store/old/journal.jsonl",
        "approvals/old.json", "odoo/business-state.json")}
    invoke(upgrade)
    assert bytes_under(old) == old_bytes
    assert all((upgrade.root / name).read_bytes() == value for name, value in protected.items())
    saved = read_json(upgrade.root / "vps-instance.json")
    for key in ("bindings", "instance_id", "generation", "volume", "image_id"):
        assert saved[key] == upgrade.record[key]
    activation, = (upgrade.root / "bundle-activations").glob("*.json")
    original_activation = activation.read_bytes()
    record = read_json(activation)
    assert record["original_bindings_sha256"] == digest(upgrade.record["bindings"])
    assert record["active_provenance_sha256"] == digest(verify_sources(upgrade.active))
    invoke(upgrade)
    assert activation.read_bytes() == original_activation
    assert not any(args[:2] in (("docker", "create"), ("docker", "start"), ("docker", "update"))
                   for args, _ in upgrade.calls)


def test_old_bundle_without_explicit_selection_still_rejected(upgrade):
    del upgrade.config["active_bundle"]
    with pytest.raises(RuntimeError, match="preserved_planner_invalid"):
        invoke(upgrade)
    assert len(upgrade.calls) == 1
    assert not (upgrade.root / "bundle-activations").exists()


@pytest.mark.parametrize("kind", ["old_source", "old_provenance", "policy", "request", "new_source"])
def test_changed_artifacts_rejected_before_container_inspection(upgrade, kind):
    if kind in {"old_source", "new_source"}:
        bundle = upgrade.root / "planner" if kind == "old_source" else upgrade.active
        path = bundle / "forgeflow/recovery/service.py"
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b"\n# changed\n")
    elif kind == "old_provenance":
        write_json(upgrade.root / "planner/provenance.json", {"replacement": True})
    else:
        write_json(upgrade.root / ("task-policy.json" if kind == "policy" else "request.json"), {})
    with pytest.raises((RuntimeError, RecoveryError), match="changed"):
        invoke(upgrade)
    assert len(upgrade.calls) == 1


def test_old_source_with_rewritten_provenance_still_fails_original_binding(upgrade):
    old = upgrade.root / "planner"
    path = old / "forgeflow/recovery/service.py"
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"\n# changed\n")
    rewrite_provenance(old, "forgeflow/recovery/service.py")
    with pytest.raises(RuntimeError, match="vps_policy_or_planner_binding_changed"):
        invoke(upgrade)


def test_activated_path_cannot_be_reused_for_a_new_self_consistent_bundle(upgrade):
    invoke(upgrade)
    activation, = (upgrade.root / "bundle-activations").glob("*.json")
    previous = activation.read_bytes()
    path = upgrade.active / "forgeflow/recovery/service.py"
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"\n# changed\n")
    rewrite_provenance(upgrade.active, "forgeflow/recovery/service.py")
    with pytest.raises(RuntimeError, match="bundle_activation_binding_changed"):
        invoke(upgrade)
    assert activation.read_bytes() == previous


@pytest.mark.parametrize("part", ["generation", "instance_id", "original_bindings_sha256"])
def test_activation_is_bound_to_original_instance(upgrade, part):
    invoke(upgrade)
    activation, = (upgrade.root / "bundle-activations").glob("*.json")
    value = read_json(activation)
    value[part] = "another-instance"
    write_json(activation, value)
    with pytest.raises(RuntimeError, match="bundle_activation_binding_changed"):
        invoke(upgrade)


@pytest.mark.parametrize("kind", ["source_symlink", "source_hardlink", "directory_symlink",
                                  "writable_source", "writable_parent", "wrong_owner"])
def test_unsafe_selected_bundle_is_rejected(upgrade, kind):
    source = upgrade.active / "forgeflow/recovery/service.py"
    if kind == "source_symlink":
        other = upgrade.active.parent / "source-copy.py"
        source.rename(other)
        source.symlink_to(other)
    elif kind == "source_hardlink":
        os.link(source, upgrade.active.parent / "linked.py")
    elif kind == "directory_symlink":
        alias = upgrade.active.parent / "alias"
        alias.symlink_to(upgrade.active, target_is_directory=True)
        upgrade.config["active_bundle"] = str(alias)
    elif kind == "writable_source":
        source.chmod(0o666)
    elif kind == "writable_parent":
        upgrade.active.parent.chmod(0o777)
    else:
        upgrade.owners[source] = 12345
    with pytest.raises(RuntimeError, match="unsafe_bundle_path"):
        invoke(upgrade)
    assert len(upgrade.calls) == 1


def test_selected_old_incomplete_bundle_does_not_gain_current_execution_rights(upgrade):
    shutil.rmtree(upgrade.active)
    shutil.copytree(upgrade.root / "planner", upgrade.active)
    with pytest.raises(RecoveryError, match="incomplete_source_manifest"):
        invoke(upgrade)


@pytest.mark.parametrize("field,value", [("upstream_commit", "0" * 40),
                                         ("profile", "cp-sat-live-v1")])
def test_selected_bundle_must_preserve_pinned_solver_profile(upgrade, field, value):
    provenance = read_json(upgrade.active / "provenance.json")
    provenance[field] = value
    write_json(upgrade.active / "provenance.json", provenance)
    with pytest.raises(RuntimeError, match="invalid_bundle_profile"):
        invoke(upgrade)
    assert len(upgrade.calls) == 1


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "writable", "unsafe_directory"])
def test_activation_record_cannot_be_redirected_or_made_untrusted(upgrade, kind):
    invoke(upgrade)
    activation, = (upgrade.root / "bundle-activations").glob("*.json")
    if kind == "symlink":
        target = upgrade.root / "other-activation.json"
        activation.rename(target)
        activation.symlink_to(target)
    elif kind == "hardlink":
        os.link(activation, upgrade.root / "other-activation.json")
    elif kind == "writable":
        activation.chmod(0o666)
    else:
        activation.parent.chmod(0o777)
    with pytest.raises(RuntimeError, match="unsafe_bundle_path"):
        invoke(upgrade)


def test_original_missing_source_cannot_be_replaced_by_valid_current_bundle(upgrade):
    (upgrade.root / "planner/forgeflow/recovery/service.py").unlink()
    with pytest.raises(RuntimeError, match="historical_source_missing"):
        invoke(upgrade)
    assert len(upgrade.calls) == 1


def test_old_bundle_unsafe_directory_is_not_accepted_as_a_historical_exception(upgrade):
    (upgrade.root / "planner/forgeflow").chmod(0o777)
    with pytest.raises(RuntimeError, match="unsafe_bundle_path"):
        invoke(upgrade)


def test_separately_selected_current_bundle_also_preserves_current_history(upgrade):
    shutil.rmtree(upgrade.root / "planner")
    shutil.copytree(upgrade.active, upgrade.root / "planner")
    upgrade.record["bindings"] = provisioner.artifact_bindings(upgrade.root)
    write_json(upgrade.root / "vps-instance.json", upgrade.record)
    previous = bytes_under(upgrade.root / "planner")
    invoke(upgrade)
    assert bytes_under(upgrade.root / "planner") == previous


@pytest.mark.parametrize("suffix", ["", "/nested"])
def test_selection_cannot_replace_original_bundle_path(upgrade, suffix):
    upgrade.config["active_bundle"] = str(upgrade.root / "planner") + suffix
    with pytest.raises(RuntimeError, match="active_bundle_must_be_separate"):
        invoke(upgrade)


def test_explicit_selection_requires_existing_ready_instance(sandbox):
    sandbox.config["active_bundle"] = "/var/lib/forgeflow/bundles/current"
    with pytest.raises(RuntimeError, match="active_bundle_requires_ready_instance"):
        invoke(sandbox)
    assert not (sandbox.root / "vps-instance.json").exists()


def test_executor_selects_current_bundle_without_changing_store_identity_or_approval_paths():
    from test_vps_runtime import config

    original = config()
    selected = configuration({**original, "active_bundle": "/var/lib/forgeflow/bundles/release-2"})
    before, after = executor_command(original), executor_command(selected)
    at = before.index("--bundle") + 1
    assert before[at] == "/var/lib/forgeflow/sandbox/planner"
    assert after[at] == selected["active_bundle"]
    assert before[:at] + before[at + 1:] == after[:at] + after[at + 1:]


@pytest.mark.parametrize("value", [None, "", "relative", "/a/../b", "/a/./b", "/a//b", 3])
def test_invalid_selection_configuration_is_rejected(value):
    from test_vps_runtime import config

    with pytest.raises(ValueError, match="invalid_active_bundle_path"):
        configuration({**config(), "active_bundle": value})
