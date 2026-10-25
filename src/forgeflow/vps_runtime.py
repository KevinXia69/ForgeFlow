"""Root supervisor for a preserving Docker sandbox and an isolated public API.

The sandbox provisioner owns initialization. This process owns host children,
their Unix identities and the small set of configuration visible to each child.
It never adopts a standalone process by PID or starts a Docker daemon.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

API_USER = "forgeflow-api"
DSH_USER = "forgeflow-dsh"
API_ENV_KEYS = frozenset({
    "FORGEFLOW_SESSION_SECRET", "FORGEFLOW_INVITE_CODES", "FORGEFLOW_SESSION_TTL_SEC",
    "FORGEFLOW_TRUSTED_HOSTS", "FORGEFLOW_MODEL_PROFILE", "FORGEFLOW_MODEL_BASE_URL",
    "FORGEFLOW_MODEL_NAME", "FORGEFLOW_MODEL_API_KEY", "NEBIUS_API_KEY",
})
BASE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}


def trusted_json(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022 or info.st_size > 64 * 1024):
            raise ValueError("untrusted_runtime_configuration")
        with os.fdopen(descriptor, "r", closefd=False) as handle:
            value = json.load(handle)
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError("invalid_runtime_configuration")
    return value


def configuration(value: dict) -> dict:
    required = {"repo_root", "python", "sandbox_root", "case_root", "data_root",
                "socket", "container", "odoo_port", "api_port", "scenario"}
    if not required <= set(value) or value["scenario"] not in {"A", "B"}:
        raise ValueError("invalid_runtime_configuration")
    result = dict(value)
    for key in ("repo_root", "python", "sandbox_root", "case_root", "data_root", "socket"):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise ValueError("runtime_paths_must_be_absolute")
    for key in ("api_port", "odoo_port"):
        if type(value[key]) is not int or not 1024 <= value[key] <= 65535:
            raise ValueError("invalid_runtime_port")
    for key, default in {
        "dsh_root": "/var/lib/forgeflow/assistant",
        "dsh_cli": "/opt/forgeflow/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js",
        "dsh_patch": "/etc/forgeflow/dsh.patch.json",
        "dsh_node": "/usr/local/bin/node",
    }.items():
        result.setdefault(key, default)
        if not isinstance(result[key], str) or not Path(result[key]).is_absolute():
            raise ValueError("runtime_paths_must_be_absolute")
    return result


def owned_directory(path: Path, uid: int, gid: int, mode: int) -> None:
    if path.is_symlink():
        raise ValueError("runtime_directory_is_symlink")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if path.resolve() != path.absolute() or not path.is_dir():
        raise ValueError("invalid_runtime_directory")
    os.chown(path, uid, gid)
    path.chmod(mode)


def write_json(path: Path, value: dict | list, *, gid: int = 0, mode: int = 0o644) -> None:
    if path.is_symlink():
        raise ValueError("runtime_configuration_is_symlink")
    temporary = path.with_name("." + path.name + ".new")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.fchown(descriptor, 0, gid)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", closefd=False) as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def dsh_patch(config: dict, model: str, base_url: str) -> list:
    from forgeflow.api.dsh_permissions import DISABLED_MODULES

    return [
        {"id": "agent-default-model", "config": {"provider": "nebius", "model": model}},
        {"id": "llm-pi-ai", "config": {"providers": {"nebius": {
            "apiKeyEnv": "NEBIUS_API_KEY", "api": "openai-completions", "baseURL": base_url,
            "models": [{"id": model, "input": ["text"], "maxTokens": 8192}],
            "timeoutMs": 150000,
        }}}},
        {"id": "system-prompt", "config": {"personaPrefix":
            "You are the ForgeFlow manufacturing recovery assistant. Answer in the user's language. "
            "Use only the scoped ForgeFlow evidence tools for case facts. Clearly distinguish plans, "
            "actual execution and original verification. Never claim a business write occurred unless "
            "the execution evidence confirms it. You cannot approve or execute business operations."}},
        {"id": "agent-instructions", "disabled": True},
        {"id": "skill-filesystem", "disabled": True},
        {"insert": [{"id": "mcp-forgeflow", "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "forgeflow", "transport": "stdio", "command": config["python"],
            "args": [str(Path(config["repo_root"]) / "scripts/assistant_mcp.py")],
            "cwd": config["repo_root"], "env": {}, "failOnStartupError": True,
        }}]},
        *({"id": name, "disabled": True} for name in DISABLED_MODULES),
        {"id": "session-telemetry-otel", "disabled": False, "config": {"mode": "DISABLED"}},
    ]


def prepare_layout(config: dict) -> tuple[dict, dict]:
    api, dsh = pwd.getpwnam(API_USER), pwd.getpwnam(DSH_USER)
    # Only generated public compositions are readable here. Secret inputs keep
    # their individual 0600/0400 modes while DSH can traverse this directory.
    owned_directory(Path("/etc/forgeflow"), 0, 0, 0o755)
    sandbox = Path(config["sandbox_root"])
    owned_directory(sandbox, 0, 0, 0o700)
    for name in ("store", "approvals"):
        owned_directory(sandbox / name, 0, 0, 0o700)
    socket_root = Path(config["socket"]).parent
    owned_directory(socket_root, 0, api.pw_gid, 0o711)
    api_root = Path(config["case_root"]).parent
    owned_directory(api_root, api.pw_uid, api.pw_gid, 0o700)
    for name in (config["case_root"], config["data_root"], str(api_root / "home")):
        owned_directory(Path(name), api.pw_uid, api.pw_gid, 0o700)
    shared = Path(config["dsh_root"])
    owned_directory(shared, api.pw_uid, dsh.pw_gid, 0o2750)
    for name, mode in (("home", 0o2770), ("workspace", 0o2750), ("evidence", 0o2750)):
        owned_directory(shared / name, api.pw_uid, dsh.pw_gid, mode)
    # Copy only public task request facts. The ERP write key and approval store
    # remain inaccessible to both lower-privilege processes.
    public_config = socket_root / "api-config"
    owned_directory(public_config, 0, api.pw_gid, 0o750)
    with (sandbox / "request.json").open(encoding="utf-8") as handle:
        request = json.load(handle)
    write_json(public_config / "request.json", request, gid=api.pw_gid, mode=0o440)
    environment = api_environment(config)
    instance = trusted_json(sandbox / "vps-instance.json")
    generation = instance.get("generation")
    if (instance.get("phase") != "ready" or not isinstance(generation, str)
            or re.fullmatch(r"[a-f0-9]{32}", generation) is None):
        raise ValueError("runtime_generation_unavailable")
    environment["FORGEFLOW_ENVIRONMENT_GENERATION"] = generation
    if not environment.get("NEBIUS_API_KEY"):
        raise ValueError("model_api_key_required")
    model = environment.get("FORGEFLOW_MODEL_NAME", "").strip()
    base_url = environment.get("FORGEFLOW_MODEL_BASE_URL", "").strip()
    if not model or not base_url.startswith("https://"):
        raise ValueError("model_endpoint_required")
    write_json(Path(config["dsh_patch"]), dsh_patch(config, model, base_url))
    write_json(Path("/etc/forgeflow/dsh.json"), {
        "cli": config["dsh_cli"], "node": config["dsh_node"], "home": str(shared / "home"),
        "workspace": str(shared / "workspace"), "evidence_root": str(shared / "evidence"),
        "patch": config["dsh_patch"],
    })
    for name in (config["dsh_cli"], config["dsh_node"], "/usr/local/bin/forgeflow-dsh-node"):
        if not Path(name).is_file():
            raise ValueError("assistant_runtime_missing")
    return {"uid": api.pw_uid, "gid": api.pw_gid}, environment


def api_environment(config: dict, source: dict | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    env = {**BASE_ENV, **{key: value for key, value in source.items() if key in API_ENV_KEYS}}
    root, shared = Path(config["sandbox_root"]), Path(config["dsh_root"])
    env.update({
        "HOME": str(Path(config["case_root"]).parent / "home"),
        "FORGEFLOW_DEPLOYMENT": "hosted", "FORGEFLOW_SECURE_COOKIE": "true",
        "FORGEFLOW_RUNNER_ENABLED": "false", "FORGEFLOW_PUBLIC_RUNS": "",
        "FORGEFLOW_DATA_ROOT": config["data_root"], "FORGEFLOW_APPROVALS_ROOT": config["case_root"],
        "FORGEFLOW_KNOWLEDGE_ROOT": config["repo_root"], "FORGEFLOW_LOCAL_PRODUCT": "1",
        "FORGEFLOW_LOCAL_SCENARIO": config["scenario"], "FORGEFLOW_LOCAL_SOCKET": config["socket"],
        "FORGEFLOW_LOCAL_STORE": str(root / "store"), "FORGEFLOW_LOCAL_APPROVALS": str(root / "approvals"),
        "FORGEFLOW_LOCAL_REQUEST": str(Path(config["socket"]).parent / "api-config/request.json"),
        "FORGEFLOW_LOCAL_CONTAINER": config["container"], "FORGEFLOW_LOCAL_ARTIFACT_RPC": "true",
        "FORGEFLOW_LOCAL_REVISIONS": "1", "FORGEFLOW_LOCAL_CANDIDATES": "0",
        "FORGEFLOW_DSH_ENABLED": "1", "FORGEFLOW_DSH_SDK": "1",
        "FORGEFLOW_DSH_CLI": config["dsh_cli"], "FORGEFLOW_DSH_PATCH": config["dsh_patch"],
        "FORGEFLOW_DSH_NODE": "/usr/local/bin/forgeflow-dsh-node",
        "FORGEFLOW_DSH_HOME": str(shared / "home"), "FORGEFLOW_DSH_WORKSPACE": str(shared / "workspace"),
        "FORGEFLOW_ASSISTANT_EVIDENCE_ROOT": str(shared / "evidence"),
        "FORGEFLOW_DSH_MODEL": source.get("FORGEFLOW_MODEL_NAME", ""),
    })
    env["NEBIUS_API_KEY"] = source.get("FORGEFLOW_MODEL_API_KEY") or source.get("NEBIUS_API_KEY", "")
    return env


def executor_command(config: dict) -> list[str]:
    root = Path(config["sandbox_root"])
    return [config["python"], "-m", "forgeflow.executor.main", "--socket", config["socket"],
            "--store", str(root / "store"), "--bundle", str(root / "planner"),
            "--key", str(root / "api_key"), "--agent-user", API_USER,
            "--approval-store", str(root / "approvals"), "--policy-file", str(root / "task-policy.json"),
            "--product-verifier-container", config["container"], "--database", "bench",
            "--host", "127.0.0.1", "--port", str(config["odoo_port"])]


def stop_child(process: subprocess.Popen, timeout: int) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def container_boot(container: str) -> tuple[str, str, int]:
    """Read a bounded liveness/boot identity; Docker must never restart itself."""
    try:
        result = subprocess.run(["docker", "container", "inspect", container],
                                capture_output=True, text=True, timeout=5, check=False,
                                env={**BASE_ENV, "HOME": "/root"})
        if result.returncode:
            raise ValueError("runtime_container_unavailable")
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("runtime_container_unavailable")
        info = rows[0]
        state = info["State"]
        if (state.get("Running") is not True or state.get("Restarting") is True
                or state.get("OOMKilled") is True
                or info["HostConfig"]["RestartPolicy"].get("Name") != "no"):
            raise ValueError("runtime_container_unavailable")
        boot = info["Id"], state["StartedAt"], info["RestartCount"]
        if (not all(isinstance(value, str) and value for value in boot[:2])
                or type(boot[2]) is not int):
            raise ValueError("runtime_container_unavailable")
        return boot
    except (OSError, subprocess.TimeoutExpired, KeyError, TypeError, ValueError):
        raise ValueError("runtime_container_unavailable") from None


def _run_locked(config: dict, config_path: Path) -> int:
    if not shutil.which("docker"):
        raise ValueError("docker_cli_required")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    provision = subprocess.run([config["python"], str(Path(config["repo_root"]) / "scripts/vps_provision.py"),
                               "--config", str(config_path)], cwd=config["repo_root"],
                              env={**BASE_ENV, "HOME": "/root"}, check=False)
    if provision.returncode or stop.is_set():
        return provision.returncode or 0
    boot = container_boot(config["container"])
    identity, env = prepare_layout(config)
    children = []
    try:
        executor = subprocess.Popen(executor_command(config), cwd=config["repo_root"],
            env={**BASE_ENV, "HOME": "/root", "FORGEFLOW_STRICT_EXECUTOR": "true"},
            start_new_session=True)
        children.append(executor)
        deadline = time.monotonic() + 30
        socket_path = Path(config["socket"])
        while time.monotonic() < deadline and not stop.is_set():
            if executor.poll() is not None:
                raise ValueError("executor_exited_before_ready")
            if socket_path.exists():
                info = socket_path.lstat()
                if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0
                        or info.st_gid != identity["gid"] or stat.S_IMODE(info.st_mode) != 0o660):
                    raise ValueError("executor_socket_untrusted")
                break
            stop.wait(0.2)
        else:
            raise ValueError("executor_not_ready")
        if stop.is_set():
            return 0
        api = subprocess.Popen([config["python"], "-m", "forgeflow.cli", "api", "--host", "127.0.0.1",
                                "--port", str(config["api_port"])],
            cwd=config["repo_root"], env=env, user=identity["uid"], group=identity["gid"],
            extra_groups=[], umask=0o077, start_new_session=True)
        children.append(api)
        print("forgeflow runtime: executor and isolated API started", flush=True)
        next_container_check = 0.0
        while not stop.wait(0.5):
            if any(child.poll() is not None for child in children):
                raise ValueError("runtime_child_exited")
            if time.monotonic() >= next_container_check:
                if container_boot(config["container"]) != boot:
                    raise ValueError("runtime_container_restarted")
                next_container_check = time.monotonic() + 10
        return 0
    finally:
        for child in reversed(children):
            stop_child(child, 30 if child is children[-1] and len(children) > 1 else 900)


def run(config_path: Path) -> int:
    if os.geteuid() != 0:
        raise ValueError("supervisor_requires_root")
    config = configuration(trusted_json(config_path))
    lock_root = Path(config["repo_root"]) / ".runtime/lifecycle"
    owned_directory(lock_root, 0, 0, 0o700)
    descriptor = os.open(lock_root / "workbench.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("untrusted_runtime_lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("runtime_or_maintenance_already_active") from None
        return _run_locked(config, config_path)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/etc/forgeflow/runtime.json"))
    args = parser.parse_args()
    try:
        return run(args.config)
    except (OSError, ValueError, KeyError):
        # Process exceptions can contain paths, endpoint headers or secret values.
        print("forgeflow runtime: startup or child failed; inspect private service diagnostics", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
