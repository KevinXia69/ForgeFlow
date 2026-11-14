"""Exercise host-native launchers with a fake Docker executable, never a daemon.

The same assertions run under Bash on Unix and real PowerShell on Windows.
These are launcher-flow checks, not container/provider/ERP acceptance.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELLS = ["pwsh", "powershell"] if os.name == "nt" else ["bash"]
FAKE_DOCKER = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ['FF_COMMANDS'], 'a', encoding='utf-8') as log:
    log.write(json.dumps(args) + '\n')
if args[:2] == ['compose', 'version']:
    sys.exit(0)
if args[:2] == ['context', 'inspect']:
    print(os.environ.get('FF_ENDPOINT', 'npipe:////./pipe/docker_engine' if os.name == 'nt' else 'unix:///synthetic/docker.sock'))
    sys.exit(0)
if args[:1] == ['info']:
    print('linux' if 'OSType' in ' '.join(args) else '28.5.0')
    sys.exit(0)
if args[:1] != ['compose']:
    sys.exit(2)
cmd = args[5:]
if cmd[:2] == ['-f', 'compose.source.yaml']:
    cmd = cmd[2:]
if cmd[:1] == ['ps']:
    print('api\nsupervisor' if os.environ.get('FF_RUNNING') else '')
    sys.exit(0)
if 'quiesce' in cmd:
    sys.exit(int(os.environ.get('FF_QUIESCE_FAIL', '0')))
if 'doctor' in cmd:
    sys.exit(0)
if cmd[:1] == ['pull']:
    sys.exit(int(os.environ.get('FF_PULL_FAIL', '0')))
if 'forgeflow.local_workbench.setup' in cmd:
    if 'url' in cmd:
        print('http://127.0.0.1:17801/#synthetic-setup-capability')
        sys.exit(0)
    if '--saved' in cmd:
        sys.exit(0)
    if 'status' in cmd:
        sys.exit(0 if os.environ.get('FF_CONFIGURED') else 1)
sys.exit(0)
'''


@pytest.fixture(params=SHELLS)
def native_bundle(tmp_path, request):
    shell = shutil.which(request.param)
    assert shell is not None, f"Required native shell {request.param} is missing"
    folder = tmp_path / "bundle with spaces and 中文"
    folder.mkdir()
    for name in ("start.sh", "stop.sh", "Start-ForgeFlow.ps1", "Stop-ForgeFlow.ps1"):
        shutil.copy2(ROOT / "installer" / name, folder / name)
    (folder / "compose.yaml").write_text("services: {}\n")
    (folder / "compose.source.yaml").write_text("services: {}\n")
    (folder / "source").mkdir()
    binaries = tmp_path / "fake binaries"
    binaries.mkdir()
    python = binaries / "fake_docker.py"
    python.write_text(FAKE_DOCKER)
    if os.name == "nt":
        (binaries / "docker.cmd").write_text(
            f'@echo off\n"{sys.executable}" "%~dp0fake_docker.py" %*\nexit /b %errorlevel%\n')
    else:
        executable = binaries / "docker"
        executable.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
        executable.chmod(0o755)
    log = tmp_path / "commands.jsonl"
    env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
           "FF_COMMANDS": str(log), "PYTHONUTF8": "1"}
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT"):
        env.pop(name, None)

    def run(*, stop=False, configure=False, **extra):
        if os.name == "nt":
            name = "Stop-ForgeFlow.ps1" if stop else "Start-ForgeFlow.ps1"
            args = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(folder / name)]
            if not stop:
                args += ["-NoOpen"] + (["-Configure"] if configure else [])
        else:
            args = [shell, str(folder / ("stop.sh" if stop else "start.sh"))]
            if not stop:
                args += ["--no-open"] + (["--configure"] if configure else [])
        process = subprocess.run(args, cwd=tmp_path, env={**env, **extra}, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=45)
        commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        assert not any(arg in {"down", "prune", "--volumes"} for cmd in commands for arg in cmd)
        return process, commands
    return run


def test_native_first_start_configures_then_starts_without_destroying_state(native_bundle):
    process, commands = native_bundle()
    assert process.returncode == 0, process.stderr
    assert "synthetic-setup-capability" in process.stdout
    assert any("--saved" in cmd for cmd in commands)
    assert any("--wait" in cmd for cmd in commands)
    assert commands[-1][-2:] == ["stop", "setup"]


def test_native_running_start_only_checks_and_opens(native_bundle):
    process, commands = native_bundle(FF_RUNNING="1", FF_CONFIGURED="1")
    assert process.returncode == 0, process.stderr
    assert any("doctor" in cmd for cmd in commands)
    assert not any("pull" in cmd or "up" in cmd for cmd in commands)


def test_native_configure_drains_before_replacing_application_services(native_bundle):
    process, commands = native_bundle(configure=True, FF_RUNNING="1", FF_CONFIGURED="1")
    assert process.returncode == 0, process.stderr
    stopped_api = next(i for i, cmd in enumerate(commands) if cmd[-4:] == ["stop", "-t", "-1", "api"])
    quiesce = next(i for i, cmd in enumerate(commands) if cmd[-1:] == ["quiesce"])
    idle = next(i for i, cmd in enumerate(commands) if "--require-idle" in cmd)
    stopped_workers = next(i for i, cmd in enumerate(commands) if cmd[-4:] == ["-t", "-1", "supervisor", "docmind"])
    assert stopped_api < quiesce < idle < stopped_workers
    recreate = next(cmd for cmd in commands if "--force-recreate" in cmd and "--wait" in cmd)
    assert recreate[-3:] == ["api", "supervisor", "docmind"]


def test_native_quiesce_failure_does_not_kill_workers_or_mutate_configuration(native_bundle):
    process, commands = native_bundle(configure=True, FF_RUNNING="1", FF_QUIESCE_FAIL="1")
    assert process.returncode != 0
    assert not any("up" in cmd for cmd in commands)
    assert not any("stop" in cmd and "supervisor" in cmd for cmd in commands)


@pytest.mark.parametrize("endpoint", ["ssh://another-machine", "tcp://127.0.0.1:2375"])
def test_native_remote_context_refused_before_mutation(native_bundle, endpoint):
    process, commands = native_bundle(DOCKER_CONTEXT="selected", DOCKER_HOST="unix:///local.sock",
                                      FF_ENDPOINT=endpoint)
    assert process.returncode != 0
    assert not any("up" in cmd or "stop" in cmd or "pull" in cmd for cmd in commands)


def test_native_source_fallback_keeps_overlay_for_subsequent_commands(native_bundle):
    process, commands = native_bundle(FF_PULL_FAIL="1")
    assert process.returncode == 0, process.stderr
    index = next(i for i, cmd in enumerate(commands) if "build" in cmd)
    assert commands[index][-5:] == ["-f", "compose.source.yaml", "build", "setup", "odoo-init"]
    assert all("compose.source.yaml" in cmd for cmd in commands[index:] if cmd[0] == "compose")


def test_native_stop_preserves_named_volumes(native_bundle):
    process, commands = native_bundle(stop=True, FF_RUNNING="1")
    assert process.returncode == 0, process.stderr
    assert commands[-1][-3:] == ["stop", "-t", "-1"]
