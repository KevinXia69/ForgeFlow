import json
import os
from pathlib import Path

import pytest

from forgeflow import host_runtime as runtime


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nname="fixture"\n')
    (source / "uv.lock").write_text("version=1\n")
    base = tmp_path / "standalone"
    (base / "bin").mkdir(parents=True)
    (base / "lib/python3.13").mkdir(parents=True)
    (base / "bin/python3.13").write_text("synthetic interpreter bytes")
    (base / "bin/python3.13").chmod(0o700)
    (base / "bin/python").symlink_to("python3.13")
    (base / "lib/python3.13/inspect.py").write_text("original stdlib bytes")
    commands = []

    def run(command, *, cwd, timeout=180):
        commands.append(command)
        if runtime.PYTHON_PROBE in command:
            current = base if cwd == source else cwd / "python"
            return json.dumps({"base": str(current), "binary": str(current / "bin/python3.13"),
                               "minor": "3.13", "version": "fixture"})
        if "export" in command:
            assert "--locked" in command and "--offline" in command
            return "fixture==1.0 --hash=sha256:" + "a" * 64 + "\n"
        if "sync" in command:
            assert all(flag in command for flag in ("--offline", "--require-hashes", "--only-binary"))
            assert command[command.index("--link-mode") + 1] == "copy"
            packages = Path(command[command.index("--target") + 1])
            packages.mkdir()
            (packages / "fixture.py").write_text("installed wheel bytes")
            return ""
        if runtime.DEPENDENCY_PROBE in command:
            return '{"modules":{"harbor":"packages/harbor.py"}}'
        raise AssertionError(command)

    monkeypatch.setattr(runtime, "_run", run)
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/synthetic/uv")
    cache = tmp_path / "cache"
    record = runtime.ensure_host_runtime(source, cache, base / "bin/python")
    try:
        yield source, base, cache, record, commands
    finally:
        # Leave only this test's directories writable for pytest retention cleanup;
        # do not follow a deliberately planted external link or chmod its target.
        for current, _, _ in os.walk(tmp_path, followlinks=False):
            Path(current).chmod(0o700)


def test_frozen_copy_survives_original_changes_and_cached_reuse_is_verified(prepared):
    source, base, cache, record, commands = prepared
    assert runtime.verify_host_runtime(record)["ok"]
    before = len([c for c in commands if "sync" in c])
    assert runtime.ensure_host_runtime(source, cache, base / "bin/python") == record
    assert len([c for c in commands if "sync" in c]) == before
    (base / "lib/python3.13/inspect.py").write_text("new development Python")
    assert runtime.verify_host_runtime(record)["ok"]
    assert (Path(record["root"]) / "python/lib/python3.13/inspect.py").read_text() == "original stdlib bytes"
    next_record = runtime.ensure_host_runtime(source, cache, base / "bin/python")
    assert next_record["cache_key"] != record["cache_key"]
    assert next_record["root"] != record["root"]


@pytest.mark.parametrize("mutation", ["bytes", "extra", "mode", "hardlink", "escape", "entrypoint"])
def test_runtime_drift_prevents_launch(prepared, mutation, tmp_path):
    _, _, _, record, _ = prepared
    packages = Path(record["packages"])
    path = packages / "fixture.py"
    if mutation == "bytes":
        path.chmod(0o600)
        path.write_text("changed")
        path.chmod(0o400)
    elif mutation == "mode":
        path.chmod(0o600)
    elif mutation == "entrypoint":
        record["python"] = "/unrelated/python"
    else:
        packages.chmod(0o700)
        if mutation == "extra":
            (packages / "extra.py").write_text("unexpected module")
            (packages / "extra.py").chmod(0o400)
        elif mutation == "hardlink":
            os.link(path, tmp_path / "shared.py")
        else:
            path.unlink()
            path.symlink_to(tmp_path / "outside.py")
            (tmp_path / "outside.py").write_text("outside")
        packages.chmod(0o500)
    with pytest.raises(ValueError):
        runtime.verify_host_runtime(record)


def test_copy_never_traverses_external_links(tmp_path):
    root = tmp_path / "base"
    root.mkdir()
    (root / "lib").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        runtime._inventory(root)


def test_base_site_packages_are_excluded_from_python_copy(prepared):
    source, base, cache, record, _ = prepared
    packages = base / "lib/python3.13/site-packages"
    packages.mkdir()
    (packages / "sitecustomize.py").write_text("raise RuntimeError('development hook')")
    # Unrelated global installs do not change the captured interpreter inputs.
    assert runtime.ensure_host_runtime(source, cache, base / "bin/python") == record
    assert not (Path(record["root"]) / "python/lib/python3.13/site-packages").exists()


def test_runner_selects_runtime_entrypoint_and_requires_source_copy():
    from forgeflow.runner import build_run_command

    record = {"python": "/frozen/python", "packages": "/frozen/packages"}
    command = build_run_command("/workspace", "A", "nop", "/evidence",
                                execution_root="/frozen/source", host_runtime=record)
    assert command[:5] == ["/frozen/python", "-I", "-B", "-S", "-c"]
    assert "/workspace/.venv/bin/harbor" not in command
    with pytest.raises(ValueError, match="requires a frozen source"):
        build_run_command("/workspace", "A", "nop", "/evidence", host_runtime=record)


def test_builder_environment_omits_credentials_and_uv_overrides(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "never-forward")
    monkeypatch.setenv("UV_INDEX_URL", "https://private.example/never-forward")
    monkeypatch.setenv("PYTHONPATH", "/development/site")
    env = runtime._build_environment()
    assert "NEBIUS_API_KEY" not in env and "UV_INDEX_URL" not in env and "PYTHONPATH" not in env
    assert env["UV_PYTHON_DOWNLOADS"] == "never"


def test_command_uses_copied_interpreter_without_site_or_environment_hooks():
    command = runtime.harbor_command({"python": "/frozen/python", "packages": "/frozen/packages"},
                                     Path("/frozen/source"))
    assert command[:5] == ["/frozen/python", "-I", "-B", "-S", "-c"]
    assert command[-2:] == ["/frozen/source/src", "/frozen/packages"]
    assert "harbor.cli.main" in command[5]
