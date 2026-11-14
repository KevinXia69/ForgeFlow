"""Execute distribution shell entrypoints with an isolated fake Docker CLI."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKE = '''#!PYTHON
import json, os, pathlib, sys
args=sys.argv[1:]
with open(os.environ['FF_COMMANDS'], 'a') as out:
    out.write(json.dumps(args)+'\\n')
if args[:2]==['compose','version']: sys.exit(0)
if args[:2]==['context','inspect']:
    print(os.environ.get('FF_ENDPOINT','unix:///fake/docker.sock')); sys.exit(0)
if args[:1]==['info']: print('linux'); sys.exit(0)
if args[:1]==['compose']:
    cmd=args[5:]
    if cmd[:2]==['-f','compose.source.yaml']: cmd=cmd[2:]
    if cmd[:1]==['ps']: print('api\\nsupervisor' if os.environ.get('FF_RUNNING') else ''); sys.exit(0)
    if 'doctor' in cmd: sys.exit(int(os.environ.get('FF_BUSY','0')))
    if 'quiesce' in cmd: sys.exit(int(os.environ.get('FF_QUIESCE_FAIL','0')))
    if cmd[:1]==['pull']: sys.exit(int(os.environ.get('FF_PULL_FAIL','0')))
    if 'forgeflow.local_workbench.setup' in cmd:
        if 'url' in cmd: print('http://127.0.0.1:17801/#synthetic-only-capability'); sys.exit(0)
        if '--saved' in cmd: sys.exit(0)
        if 'status' in cmd: sys.exit(0 if os.environ.get('FF_CONFIGURED') else 1)
    sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / 'directory with spaces and 中文'
    directory.mkdir()
    for name in ('start.sh', 'stop.sh'):
        shutil.copy2(ROOT / 'installer' / name, directory / name)
    (directory / 'compose.yaml').write_text('services: {}\n')
    (directory / 'compose.source.yaml').write_text('services: {}\n')
    (directory / 'source').mkdir()
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    executable = binaries / 'docker'
    executable.write_text(FAKE.replace('PYTHON', sys.executable))
    executable.chmod(0o755)
    log = tmp_path / 'commands.jsonl'
    env = {**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH'],
           'FF_COMMANDS': str(log)}
    for key in ('DOCKER_CONTEXT', 'DOCKER_HOST'):
        env.pop(key, None)
    def run(script='start.sh', args=('--no-open',), **extra):
        process = subprocess.run(['bash', str(directory / script), *args], env={**env, **extra},
                                 capture_output=True, text=True, timeout=15)
        commands = [json.loads(line) for line in log.read_text().splitlines()]
        return process, commands
    return run


def test_first_start_runs_setup_then_waits_and_preserves_data(bundle):
    process, commands = bundle()
    assert process.returncode == 0, process.stderr
    assert 'http://127.0.0.1:17801/#synthetic-only-capability' in process.stdout
    assert any('--saved' in args for args in commands)
    assert any('--wait' in args for args in commands)
    assert not any('down' in args or 'prune' in args or '-v' in args for args in commands)
    assert commands[-1][-2:] == ['stop', 'setup']


def test_existing_installation_does_not_ask_for_keys_again(bundle):
    process, commands = bundle(FF_CONFIGURED='1')
    assert process.returncode == 0, process.stderr
    assert 'synthetic-only-capability' not in process.stdout
    assert not any('--saved' in args for args in commands)


def test_configure_checks_idle_and_recreates_only_application_services(bundle):
    process, commands = bundle(args=('--configure', '--no-open'), FF_RUNNING='1', FF_CONFIGURED='1')
    assert process.returncode == 0, process.stderr
    idle = next(i for i, args in enumerate(commands) if '--require-idle' in args)
    api_stop = next(i for i, args in enumerate(commands) if args[-4:] == ['stop','-t','-1','api'])
    quiesce = next(i for i, args in enumerate(commands) if args[-1:] == ['quiesce'])
    pause = next(i for i, args in enumerate(commands) if args[-4:] == ['-t','-1','supervisor','docmind'])
    assert api_stop < quiesce < idle < pause
    recreate = next(args for args in commands if '--force-recreate' in args and '--wait' in args)
    assert recreate[-3:] == ['api', 'supervisor', 'docmind']


def test_busy_work_prevents_configuration_mutation_or_stop(bundle):
    process, commands = bundle(args=('--configure','--no-open'), FF_RUNNING='1', FF_QUIESCE_FAIL='1')
    assert process.returncode != 0
    assert not any('up' in args for args in commands)
    assert not any('stop' in args and 'supervisor' in args for args in commands)


@pytest.mark.parametrize('endpoint', ['ssh://remote-host', 'tcp://127.0.0.1:2375'])
def test_remote_context_rejected_even_with_local_docker_host(bundle, endpoint):
    process, commands = bundle(DOCKER_CONTEXT='selected', DOCKER_HOST='unix:///local.sock', FF_ENDPOINT=endpoint)
    assert process.returncode != 0
    assert not any('up' in args or 'stop' in args for args in commands)
    assert any(args[:3] == ['context','inspect','selected'] for args in commands)


def test_stop_preserves_every_volume(bundle):
    process, commands = bundle(script='stop.sh', args=(), FF_RUNNING='1')
    assert process.returncode == 0, process.stderr
    assert commands[-1][-3:] == ['stop','-t','-1']
    assert not any('down' in args or 'prune' in args or '--volumes' in args for args in commands)


def test_existing_running_installation_does_not_pull_or_recreate(bundle):
    process, commands = bundle(FF_RUNNING='1', FF_CONFIGURED='1')
    assert process.returncode == 0, process.stderr
    assert not any('pull' in args or 'up' in args for args in commands)
    assert any('doctor' in args for args in commands)


def test_registry_failure_builds_only_bundled_sources(bundle):
    process, commands = bundle(FF_PULL_FAIL='1')
    assert process.returncode == 0, process.stderr
    assert any(args[-5:] == ['-f','compose.source.yaml','build','setup','odoo-init'] for args in commands)
    assert 'base images still require internet access' in process.stdout
    build = next(i for i,args in enumerate(commands) if 'build' in args)
    assert all('compose.source.yaml' in args for args in commands[build:] if args[0]=='compose')
