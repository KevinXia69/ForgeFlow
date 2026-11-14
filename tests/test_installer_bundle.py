"""Build and inspect real ZIPs from disposable committed Git fixtures."""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = load("forgeflow_bundle_builder", ROOT / "scripts/build_installers.py")
checker = load("forgeflow_bundle_checker", ROOT / "scripts/check_distribution.py")
APP = "ghcr.io/kevinxia69/forgeflow-workbench@sha256:" + "a" * 64
ODOO = "ghcr.io/kevinxia69/forgeflow-workbench-odoo@sha256:" + "b" * 64
VERSION = "3.10.0"


def write(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def commit(root):
    git(root, "add", ".")
    for path in (root / "installer").glob("*"):
        git(root, "update-index", "--chmod=+x", path.relative_to(root).as_posix())
    git(root, "-c", "user.name=Distribution Test", "-c", "user.email=distribution@example.invalid",
        "commit", "--quiet", "-m", "Synthetic public fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source checkout"
    root.mkdir()
    for relative in checker.REQUIRED:
        write(root, relative, "" if relative.endswith((".py", ".tsx")) else "synthetic fixture\n")
    for relative in ("scripts/check_distribution.py", "deploy/pandoc-reference.docx"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    shutil.copytree(ROOT / checker.PACK, root / checker.PACK, dirs_exist_ok=True)
    from hashlib import sha256
    pin = sha256((root / checker.PACK / "comparisons.json").read_bytes()).hexdigest()
    write(root, "src/forgeflow/knowledge/reference_facts.py",
          f'REVIEWED_COMPARISONS_SHA256 = "{pin}"\n')
    write(root, "pyproject.toml", f'[project]\nname="forgeflow"\nversion="{VERSION}"\n')
    for relative in ("web", "deploy/vps/dsh"):
        write(root, f"{relative}/package.json", '{"dependencies":{}}')
        write(root, f"{relative}/package-lock.json", '{"packages":{"":{"dependencies":{}}}}')
    write(root, "odoo-addons/forgeflow_general/__manifest__.py", "{'data': []}\n")
    for name in ("app", "odoo"):
        write(root, f"deploy/workbench/Dockerfile.{name}", "FROM scratch\nCOPY src /app/src\n")
    roles = {name: {"image": f"forgeflow-workbench:{VERSION}"}
             for name in ("setup", "api", "supervisor", "docmind", "bootstrap")}
    roles.update({name: {"image": f"forgeflow-workbench-odoo:{VERSION}"}
                  for name in ("odoo", "odoo-init")})
    write(root, "deploy/workbench/compose.yaml", yaml.safe_dump({"services": roles}))
    # Exercise the actual reviewed source-build tags, not a test-only overlay.
    shutil.copyfile(ROOT / "deploy/workbench/compose.source.yaml",
                    root / "deploy/workbench/compose.source.yaml")
    # The image is an explicit synthetic fixture, never a product screenshot.
    (root / "docs/images/local-workbench-home.png").write_bytes(b"\x89PNG\r\n\x1a\n\0synthetic")
    git(root, "init", "--quiet")
    git(root, "config", "core.autocrlf", "false")
    commit(root)
    assert not checker.check_distribution(root)[0]
    return root


def archive_files(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_windows_autocrlf_preserves_reviewed_reference_bytes(tmp_path):
    """Exercise Git's real Windows conversion policy without weakening hashes."""
    from hashlib import sha256

    root = tmp_path / "windows checkout policy"
    root.mkdir()
    git(root, "init", "--quiet")
    git(root, "config", "core.autocrlf", "false")
    originals = {
        "knowledge/reference/synthetic/reviewed.md": b"# Synthetic reference\nA reviewed fact.\n",
        "knowledge/reference/synthetic/comparisons.json": b'{\n  "synthetic": true\n}\n',
        "ordinary-source.txt": b"source line one\nsource line two\n",
    }
    for name, data in originals.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    commit(root)
    git(root, "config", "core.autocrlf", "true")
    for name in originals:
        (root / name).unlink()
    git(root, "checkout", "HEAD", "--", *originals)
    # Reproduce the original failure: Windows checkout changes bound bytes.
    for name, data in originals.items():
        assert (root / name).read_bytes() == data.replace(b"\n", b"\r\n")

    (root / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
    git(root, "add", ".gitattributes")
    git(root, "-c", "user.name=Distribution Test", "-c", "user.email=distribution@example.invalid",
        "commit", "--quiet", "-m", "Preserve reviewed bytes")
    for name in originals:
        (root / name).unlink()
    git(root, "checkout", "HEAD", "--", *originals)
    for name, data in originals.items():
        actual = (root / name).read_bytes()
        assert actual == data
        assert sha256(actual).digest() == sha256(data).digest()


def test_three_bundles_include_only_committed_source_and_valid_hashes(source, tmp_path):
    before = git(source, "rev-parse", "HEAD")
    committed_readme = (source / "README.md").read_bytes()
    (source / "README.md").write_text("uncommitted change must not ship")
    write(source, ".env", "untracked private file")
    write(source, ".runtime/config.json", "untracked private state")
    output = tmp_path / "downloads"
    result = builder.build(source, output, VERSION, app_image=APP, odoo_image=ODOO)
    assert result["commit"] == before
    assert {asset["file"] for asset in result["assets"]} == {
        f"ForgeFlow-v{VERSION}-{platform}.zip" for platform in builder.PLATFORMS}
    from hashlib import sha256
    sums = (output / "SHA256SUMS").read_text()
    for asset in result["assets"]:
        path = output / asset["file"]
        assert sha256(path.read_bytes()).hexdigest() == asset["sha256"]
        assert f"{asset['sha256']}  {asset['file']}\n" in sums
        checked = builder.verify_bundle(path)
        assert checked["version"] == VERSION and checked["commit"] == before
        files = archive_files(path)
        prefix = path.stem + "/"
        assert files[prefix + "source/README.md"] == committed_readme
        assert not any(part in {".git", ".env", ".runtime"} for name in files for part in name.split("/"))
        manifest = json.loads(files[prefix + "release.json"])
        assert set(manifest["files"]) == {name.removeprefix(prefix) for name in files
                                         if name != prefix + "release.json"}
        assert all(sha256(files[prefix + name]).hexdigest() == digest
                   for name, digest in manifest["files"].items())
        for launcher in builder.PLATFORMS[manifest["platform"]]:
            with zipfile.ZipFile(path) as archive:
                mode = archive.getinfo(prefix + launcher).external_attr >> 16
            assert stat.S_ISREG(mode)
            if launcher.endswith((".sh", ".command")):
                assert mode & 0o111


def test_fallback_overrides_every_pinned_service_with_a_buildable_tag(source, tmp_path):
    result = builder.build(source, tmp_path / "out", VERSION, app_image=APP, odoo_image=ODOO)
    path = tmp_path / "out" / result["assets"][0]["file"]
    files = archive_files(path)
    prefix = path.stem + "/"
    base = yaml.safe_load(files[prefix + "compose.yaml"])
    fallback = yaml.safe_load(files[prefix + "compose.source.yaml"])
    for name, service in fallback["services"].items():
        assert service["build"]["context"] == "./source"
        assert "@" not in service["image"] and ":" in service["image"]
        assert service["image"].endswith(":source")
        assert base["services"][name]["image"] == (ODOO if name.startswith("odoo") else APP)
        assert prefix + "source/" + service["build"]["dockerfile"] in files


def test_zip_executable_modes_come_from_git_not_host_filesystem(source, tmp_path, monkeypatch):
    original = builder.committed_source

    def without_host_executable_bits(root, destination, **kwargs):
        result = original(root, destination, **kwargs)
        for path in destination.rglob("*"):
            if path.is_file():
                path.chmod(0o644)
        return result

    monkeypatch.setattr(builder, "committed_source", without_host_executable_bits)
    result = builder.build(source, tmp_path / "out", VERSION)
    for asset in result["assets"]:
        path = tmp_path / "out" / asset["file"]
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(("/start.sh", "/stop.sh", "/Start-ForgeFlow.command")):
                    assert (archive.getinfo(name).external_attr >> 16) & 0o111


@pytest.mark.parametrize("version", ["3.10.1", "../unsafe", "3.10.0/other"])
def test_version_must_match_the_committed_project(source, tmp_path, version):
    with pytest.raises(ValueError):
        builder.build(source, tmp_path / "out", version)


@pytest.mark.parametrize("app,odoo", [
    (APP, None), ("untrusted.invalid/app:latest", ODOO), (ODOO, APP),
])
def test_direct_builder_calls_also_validate_both_image_roles(source, tmp_path, app, odoo):
    with pytest.raises((ValueError, __import__("argparse").ArgumentTypeError)):
        builder.build(source, tmp_path / "out", VERSION, app_image=app, odoo_image=odoo)


def test_committed_private_state_is_rejected_by_the_real_distribution_gate(source, tmp_path):
    write(source, ".runtime/private.json", "must not ship")
    commit(source)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        builder.build(source, tmp_path / "out", VERSION)


@pytest.mark.parametrize("unsafe", ["../escape", "/absolute", "C:/escape", "..\\escape"])
def test_source_archive_rejects_posix_and_windows_path_traversal(tmp_path, monkeypatch, unsafe):
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w") as archive:
        item = tarfile.TarInfo(unsafe)
        item.size = 1
        archive.addfile(item, io.BytesIO(b"x"))
    monkeypatch.setattr(builder.subprocess, "check_output",
                        lambda args, **kwargs: "a" * 40 if "rev-parse" in args else content.getvalue())
    with pytest.raises(ValueError):
        builder.committed_source(tmp_path, tmp_path / "destination")
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "duplicate"])
def test_source_archive_rejects_links_and_duplicate_entries(tmp_path, monkeypatch, kind):
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w") as archive:
        item = tarfile.TarInfo("source.txt")
        if kind == "duplicate":
            item.size = 1
            archive.addfile(item, io.BytesIO(b"x"))
            archive.addfile(item, io.BytesIO(b"y"))
        else:
            item.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            item.linkname = "../outside"
            archive.addfile(item)
    monkeypatch.setattr(builder.subprocess, "check_output",
                        lambda args, **kwargs: "a" * 40 if "rev-parse" in args else content.getvalue())
    with pytest.raises(ValueError):
        builder.committed_source(tmp_path, tmp_path / "destination")


@pytest.mark.parametrize("change", ["modify", "missing", "extra", "traversal", "windows-traversal", "symlink"])
def test_verification_rejects_tampered_or_unsafe_archives(source, tmp_path, change):
    result = builder.build(source, tmp_path / "out", VERSION)
    original = tmp_path / "out" / result["assets"][0]["file"]
    files = archive_files(original)
    prefix = original.stem + "/"
    if change == "modify":
        files[prefix + "README.md"] += b"changed"
    elif change == "missing":
        del files[prefix + "README.md"]
    elif change == "extra":
        files[prefix + "unlisted.txt"] = b"extra"
    elif change == "traversal":
        files[prefix + "../escape"] = b"escape"
    elif change == "windows-traversal":
        files[prefix + "..\\escape"] = b"escape"
    altered = tmp_path / change / original.name
    altered.parent.mkdir()
    with zipfile.ZipFile(altered, "w") as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = 0o100755 << 16
            if change == "symlink" and name == prefix + "README.md":
                info.external_attr = 0o120777 << 16
            archive.writestr(info, data)
    with pytest.raises(ValueError):
        builder.verify_bundle(altered)


def test_trusted_release_checksum_is_required_to_detect_a_rewritten_manifest(source, tmp_path):
    from hashlib import sha256

    result = builder.build(source, tmp_path / "out", VERSION)
    asset = result["assets"][0]
    path = tmp_path / "out" / asset["file"]
    files = archive_files(path)
    prefix = path.stem + "/"
    files[prefix + "README.md"] = b"replaced source"
    manifest = json.loads(files[prefix + "release.json"])
    manifest["files"]["README.md"] = sha256(files[prefix + "README.md"]).hexdigest()
    files[prefix + "release.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    # Internal hashes provide consistency; the trusted outer checksum is the
    # authentication boundary when both content and manifest can be replaced.
    assert builder.verify_bundle(path)["version"] == VERSION
    with pytest.raises(ValueError, match="bundle_checksum_mismatch"):
        builder.verify_bundle(path, expected_sha256=asset["sha256"])
