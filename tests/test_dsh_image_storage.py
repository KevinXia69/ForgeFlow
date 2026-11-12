"""Owner image storage, including the distinct Unix identities used by VPS."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from forgeflow.api.dsh_images import save_scoped_image


def test_private_images_stay_private_under_restrictive_umask(tmp_path):
    root = tmp_path / "uploads" / "owner"
    previous = os.umask(0o077)
    try:
        image = save_scoped_image(tmp_path, root, b"synthetic-image", ".png")
    finally:
        os.umask(previous)
    assert image.read_bytes() == b"synthetic-image"
    assert stat.S_IMODE(image.stat().st_mode) == 0o600
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


@pytest.mark.parametrize("linked", ["uploads", "owner"])
def test_image_save_rejects_symlinked_directories_without_chmod_target(tmp_path, linked):
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir(mode=0o700)
    uploads = workspace / "uploads"
    if linked == "uploads":
        uploads.symlink_to(outside, target_is_directory=True)
    else:
        uploads.mkdir()
        (uploads / "owner").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        save_scoped_image(workspace, uploads / "owner", b"synthetic-image", ".png")
    assert list(outside.iterdir()) == []
    assert stat.S_IMODE(outside.stat().st_mode) == 0o700


def test_image_save_rejects_scope_outside_workspace(tmp_path):
    outside = tmp_path.parent / "outside-image-scope"
    with pytest.raises(ValueError, match="image_scope_unavailable"):
        save_scoped_image(tmp_path, outside, b"synthetic-image", ".png")


def test_shared_images_require_a_trusted_setgid_layout(tmp_path):
    workspace, evidence = tmp_path / "workspace", tmp_path / "evidence"
    workspace.mkdir()
    evidence.mkdir()
    with pytest.raises(ValueError, match="invalid_image_share"):
        save_scoped_image(workspace, workspace / "uploads" / "owner", b"image", ".png",
                          evidence_root=str(evidence))
    assert not (workspace / "uploads").exists()


@pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0,
                    reason="requires Linux root to exercise isolated API and DSH identities")
def test_linux_isolated_dsh_can_read_images_but_cannot_write_and_unrelated_user_is_denied(tmp_path):
    # pytest's own ancestors may be private. A separate traversable scratch root
    # models the provisioned VPS shared root without weakening the test runner.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="forgeflow-image-identities-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        workspace, evidence = root / "workspace", root / "evidence"
        for path in (workspace, evidence):
            path.mkdir()
            os.chown(path, 11001, 11002)
            path.chmod(0o2750)
        script = """
import importlib.util, json, os, stat, sys
from pathlib import Path
workspace, evidence, module = map(Path, sys.argv[1:])
# Load the dependency-free storage helper without importing the HTTP app in
# the isolated worker; its Unix access does not depend on web dependencies.
spec = importlib.util.spec_from_file_location('dsh_image_storage', module)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)
paths = []
for owner in ('owner-a', 'owner-b', 'owner-a'):
    path = storage.save_scoped_image(workspace, workspace / 'uploads' / owner,
                             b'synthetic-image', '.png', evidence_root=str(evidence))
    assert path.stat().st_gid == 11002
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o2750
    paths.append(str(path))
print(json.dumps(paths))
"""
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        module = Path(__file__).resolve().parents[1] / "src/forgeflow/api/dsh_images.py"
        command = [sys.executable, "-c", script, str(workspace), str(evidence), str(module)]
        api = subprocess.run(command, user=11001, group=11001, extra_groups=[11002],
                             umask=0o077, cwd="/tmp", env=env, text=True,
                             capture_output=True, check=False)
        assert api.returncode == 0, api.stderr
        paths = json.loads(api.stdout)
        read = """
import sys
from pathlib import Path
for name in sys.argv[1:]:
    path=Path(name)
    assert path.read_bytes() == b'synthetic-image'
    try: path.write_bytes(b'must not write')
    except PermissionError: pass
    else: raise AssertionError('DSH received image write access')
"""
        dsh = subprocess.run([sys.executable, "-c", read, *paths], user=11002, group=11002,
                             extra_groups=[], cwd="/tmp", text=True, capture_output=True)
        assert dsh.returncode == 0, dsh.stderr
        denied = subprocess.run([sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).read_bytes()", paths[0]],
                                user=11003, group=11003, extra_groups=[], cwd="/tmp",
                                text=True, capture_output=True)
        assert denied.returncode != 0 and "PermissionError" in denied.stderr
        # Missing group membership must fail closed, not clear setgid and break
        # the next owner's directory or publish world-readable images.
        no_group = subprocess.run(command, user=11001, group=11001, extra_groups=[],
                                  umask=0o077, cwd="/tmp", env=env, text=True, capture_output=True)
        assert no_group.returncode != 0 and "invalid_image_share" in no_group.stderr
