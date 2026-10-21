import hashlib
import json
import subprocess
import sys

import pytest
from test_runner import make_fake_workspace

from forgeflow.frozen_execution import IMPORT_PROBE, materialize_execution, verify_execution
from forgeflow.runner import capture_implementation_snapshot


def staged(tmp_path, *, agent="pi", planner="cp-sat-recovery-v1"):
    make_fake_workspace(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    snapshot = capture_implementation_snapshot(tmp_path, run)
    record = materialize_execution(tmp_path, run, snapshot, agent=agent, planner_profile=planner)
    return run, record


def test_private_copy_has_only_generic_dependencies_and_no_task_answers(tmp_path):
    run, record = staged(tmp_path)
    root = run / record["root"]
    assert verify_execution(run, record)["ok"]
    assert (root / "vendor/erp-bench/agents/pi.py").is_file()
    assert (root / "vendor/erp-bench/erp_bench/procurement/solver.py").is_file()
    assert not (root / "vendor/erp-bench/tasks").exists()
    assert not (root / ".env").exists()
    assert not (root / ".venv").exists()
    assert record["host_dependencies_frozen"] is False


def test_later_workspace_edits_do_not_change_imported_execution_bytes(tmp_path):
    run, record = staged(tmp_path, agent="oracle")
    original = tmp_path / "src/forgeflow/runner.py"
    original.write_text("raise RuntimeError('workspace drift')\n")
    root = run / record["root"]
    result = subprocess.run(
        [sys.executable, "-c", "import forgeflow.runner; print(forgeflow.runner.__file__)"],
        cwd=root, env={"PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == str(root / "src/forgeflow/runner.py")
    assert verify_execution(run, record)["ok"]


@pytest.mark.parametrize("mutation", ["bytes", "mode", "missing", "extra", "symlink"])
def test_copy_corruption_is_not_accepted_as_success(tmp_path, mutation):
    run, record = staged(tmp_path, agent="oracle")
    root = run / record["root"]
    path = root / "src/forgeflow/runner.py"
    if mutation in {"bytes", "mode"}:
        path.chmod(0o600)
        if mutation == "bytes":
            path.write_text("# changed\n")
            path.chmod(0o400)
    else:
        path.parent.chmod(0o700)
        if mutation == "missing":
            path.unlink()
        elif mutation == "extra":
            (path.parent / "extra.py").write_text("# added")
        else:
            path.unlink()
            path.symlink_to(tmp_path / "src/forgeflow/runner.py")
        path.parent.chmod(0o500)
    with pytest.raises(ValueError):
        verify_execution(run, record)


@pytest.mark.parametrize("change", ["drift", "provenance", "dependency", "index", "secret"])
def test_staging_rejects_untrusted_or_changed_inputs(tmp_path, change):
    make_fake_workspace(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    snapshot = capture_implementation_snapshot(tmp_path, run)
    secrets = ()
    if change == "drift":
        snapshot["workspace_matches_at_capture"] = False
    elif change == "provenance":
        path = tmp_path / "vendor/pi-mono.source.json"
        payload = json.loads(path.read_text())
        payload["commit"] = "wrong"
        path.write_text(json.dumps(payload))
    elif change == "dependency":
        (tmp_path / "vendor/erp-bench/agents/pi.py").write_text("# changed")
    elif change == "index":
        snapshot["index_sha256"] = hashlib.sha256(b"different").hexdigest()
    else:
        secrets = ("synthetic dependency",)
    with pytest.raises(ValueError):
        materialize_execution(tmp_path, run, snapshot, agent="pi", planner_profile="none", secrets=secrets)


def test_probe_refuses_fallback_to_a_workspace_package(tmp_path):
    run, record = staged(tmp_path, agent="oracle")
    root = run / record["root"]
    result = subprocess.run(
        [sys.executable, "-c", IMPORT_PROBE, str(root), "forgeflow.runner"],
        cwd=tmp_path, env={"PYTHONPATH": str(tmp_path / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "escaped frozen source" in result.stderr
