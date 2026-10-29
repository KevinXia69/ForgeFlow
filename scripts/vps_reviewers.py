#!/usr/bin/env python3
"""Install fixed reviewer instances without changing the legacy ERP or routing.

Root-only operator tool. Preparation creates private credentials and isolated
identities; starting a NEW instance explicitly seeds its own ERP exactly once.
No command clears a lease, resets a database or edits the public reverse proxy.
"""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import re
import secrets
import shlex
import stat
import subprocess
import time
from pathlib import Path

from dotenv import dotenv_values

from forgeflow.vps_runtime import configuration, trusted_json

CONFIG_ROOT = Path("/etc/forgeflow")
STATE_ROOT = Path("/var/lib/forgeflow/reviewers")
INDEX_PATH = CONFIG_ROOT / "reviewers.json"
REVIEWER_NAMES = (*(f"judge{number}" for number in range(1, 6)), "qa1", "qa2", "qa3")
NAME = re.compile(r"(?:judge[1-5]|qa[1-3])")
ENV_KEYS = ("FORGEFLOW_MODEL_PROFILE", "FORGEFLOW_MODEL_BASE_URL", "FORGEFLOW_MODEL_NAME",
            "FORGEFLOW_MODEL_API_KEY", "NEBIUS_API_KEY", "FORGEFLOW_SESSION_TTL_SEC")


def command(argv: list[str], *, timeout: int = 60) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
    if result.returncode:
        raise ValueError("reviewer_operator_command_failed:" + Path(argv[0]).name)
    return result.stdout.strip()


def directory(path: Path, mode: int = 0o755) -> None:
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError("canonical_operator_directory_required")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if not path.is_dir() or path.stat().st_uid != 0:
        raise ValueError("root_owned_operator_directory_required")
    path.chmod(mode)


def write(path: Path, content: str, *, mode: int = 0o600, gid: int = 0) -> None:
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_uid != 0)):
        raise ValueError("unsafe_operator_output")
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchown(fd, 0, gid)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        os.close(fd)
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: dict, **kwargs) -> None:
    write(path, json.dumps(value, indent=2, sort_keys=True) + "\n", **kwargs)


def private_env(path: Path) -> dict[str, str]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("private_root_environment_required")
    value = dotenv_values(path)
    if any(not isinstance(v, str) or any(c in v for c in "\r\n\x00") for v in value.values()):
        raise ValueError("invalid_environment_value")
    return dict(value)


def system_user(name: str) -> pwd.struct_passwd:
    try:
        identity = pwd.getpwnam(name)
    except KeyError:
        command(["useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", name])
        identity = pwd.getpwnam(name)
    if identity.pw_uid == 0 or identity.pw_gid == 0 or not identity.pw_shell.endswith("/nologin"):
        raise ValueError("isolated_system_identity_required")
    if any(name in group.gr_mem and group.gr_gid != identity.pw_gid for group in grp.getgrall()):
        raise ValueError("supplementary_groups_refused")
    return identity


def definition(name: str, base: dict) -> dict:
    if NAME.fullmatch(name) is None:
        raise ValueError("unknown_reviewer_instance")
    offset = int(name[-1]) + (5 if name.startswith("qa") else 0)
    root = STATE_ROOT / name
    return configuration({
        "instance_name": name, "repo_root": base["repo_root"], "python": base["python"],
        "sandbox_root": str(root / "sandbox"), "case_root": str(root / "api/cases"),
        "data_root": str(root / "api/runs"), "socket": f"/run/forgeflow-{name}/executor.sock",
        "container": "forgeflow-odoo-" + name, "scenario": "A",
        "odoo_port": 18100 + offset, "api_port": 8100 + offset,
        "odoo_image": base["odoo_image"], "allow_initialize": True,
        "odoo_memory_mb": 1024, "odoo_cpus": 1,
        "api_user": "forgeflow-api-" + name, "dsh_user": "forgeflow-dsh-" + name,
        "dsh_cli": base["dsh_cli"], "dsh_node": base["dsh_node"],
        "dsh_root": str(root / "assistant"),
        "dsh_patch": str(CONFIG_ROOT / "instances" / (name + ".dsh.patch.json")),
        "dsh_launcher": "/usr/local/bin/forgeflow-dsh-" + name,
    })


def load_index() -> dict:
    if not INDEX_PATH.exists():
        return {"schema": 1, "instances": {}}
    value = trusted_json(INDEX_PATH)
    if value.get("schema") != 1 or not isinstance(value.get("instances"), dict):
        raise ValueError("invalid_reviewer_index")
    return value


def checked_names(names: list[str]) -> None:
    if not names or len(set(names)) != len(names) or any(name not in REVIEWER_NAMES for name in names):
        raise ValueError("invalid_reviewer_names")


def check_ports(definitions: dict, base: dict, index: dict) -> None:
    """Fail before provisioning if a fixed port overlaps another retained instance."""
    reserved = {base["api_port"]: "legacy", base["odoo_port"]: "legacy"}
    for name, item in index["instances"].items():
        for field in ("port", "odoo_port"):
            port = item[field]
            if port in reserved and reserved[port] != name:
                raise ValueError("reviewer_port_conflict")
            reserved[port] = name
    for name, config in definitions.items():
        for field in ("api_port", "odoo_port"):
            port = config[field]
            if port in reserved and reserved[port] != name:
                raise ValueError("reviewer_port_conflict")
            reserved[port] = name


def backup_definition(config: dict, base_backup: dict) -> dict:
    name = config["instance_name"]
    return {
        "project_root": config["repo_root"], "runtime_root": config["sandbox_root"],
        "odoo_state": str(Path(config["sandbox_root"]) / "odoo"),
        "odoo_container": config["container"], "case_root": config["case_root"],
        "api_data_root": config["data_root"],
        "encryption_key_file": base_backup["encryption_key_file"],
        "output_root": str(Path(base_backup["output_root"]) / name),
        "maintenance_units": [config["service_unit"]],
        "extra_state_roots": {"assistant": config["dsh_root"]},
        "secret_files": {
            "runtime.json": str(CONFIG_ROOT / "instances" / (name + ".json")),
            "runtime.env": str(CONFIG_ROOT / "instances" / (name + ".env")),
        },
    }


def prepare(names: list[str]) -> dict:
    checked_names(names)
    base = configuration(trusted_json(CONFIG_ROOT / "runtime.json"))
    if re.fullmatch(r"sha256:[0-9a-f]{64}", base.get("odoo_image", "")) is None:
        raise ValueError("legacy_verified_immutable_image_required")
    if command(["docker", "image", "inspect", "--format", "{{.Id}}", base["odoo_image"]]) != base["odoo_image"]:
        raise ValueError("immutable_image_missing")
    source = private_env(CONFIG_ROOT / "runtime.env")
    codes = source["FORGEFLOW_INVITE_CODES"].split(",")
    if len(codes) != 6 or len(set(codes)) != 6 or any(len(code) < 24 for code in codes):
        raise ValueError("six_distinct_legacy_invites_required")
    trusted_hosts = [host.strip() for host in source["FORGEFLOW_TRUSTED_HOSTS"].split(",")
                     if host.strip() and host.strip() not in {"127.0.0.1", "localhost", "::1"}]
    if not trusted_hosts or any("*" in host for host in trusted_hosts):
        raise ValueError("exact_trusted_hosts_required")
    definitions = {name: definition(name, base) for name in names}
    base_backup = trusted_json(CONFIG_ROOT / "backup.json")
    key = Path(base_backup["encryption_key_file"])
    info = key.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077 or info.st_size != 32:
        raise ValueError("private_existing_backup_key_required")
    index = load_index()
    check_ports(definitions, base, index)
    legacy = {"id": "legacy", "port": base["api_port"], "invite_code": codes[0]}
    if index.get("legacy", legacy) != legacy:
        raise ValueError("legacy_invite_binding_changed")
    index.update(legacy=legacy, trusted_hosts=trusted_hosts)
    directory(CONFIG_ROOT)
    directory(CONFIG_ROOT / "instances")
    directory(CONFIG_ROOT / "assistants")
    directory(STATE_ROOT)
    for name, config in definitions.items():
        existing = index["instances"].get(name)
        config_path = CONFIG_ROOT / "instances" / (name + ".json")
        env_path = config_path.with_suffix(".env")
        if existing:
            retained = configuration(trusted_json(config_path))
            for field in ("instance_name", "api_user", "dsh_user", "api_port", "odoo_port", "socket", "case_root", "data_root"):
                if retained[field] != config[field]:
                    raise ValueError("existing_reviewer_binding_changed")
            if private_env(env_path).get("FORGEFLOW_INVITE_CODES") != existing["invite_code"]:
                raise ValueError("existing_reviewer_invite_changed")
            if name.startswith("judge") and existing["invite_code"] != codes[int(name[-1])]:
                raise ValueError("legacy_reviewer_invite_binding_changed")
            config = retained
        else:
            if config_path.exists() or env_path.exists() or (STATE_ROOT / name).exists():
                raise ValueError("unregistered_reviewer_state_refused")
            if command(["docker", "ps", "-aq", "--filter", "name=^/" + config["container"] + "$"]):
                raise ValueError("unregistered_reviewer_container_refused")
        api, dsh = system_user(config["api_user"]), system_user(config["dsh_user"])
        if api.pw_uid == dsh.pw_uid or api.pw_gid == dsh.pw_gid:
            raise ValueError("isolated_reviewer_identities_required")
        directory(STATE_ROOT / name)
        wrapper = ("#!/bin/sh\nexec /usr/bin/sudo -n -u " + config["dsh_user"] +
            " --preserve-env=NEBIUS_API_KEY,FORGEFLOW_ASSISTANT_EVIDENCE " + shlex.quote(config["python"]) +
            " -I -m forgeflow.dsh_launcher \"$@\"\n")
        if re.fullmatch(r"/[A-Za-z0-9_./-]+", config["python"]) is None:
            raise ValueError("sudo_python_path_must_be_literal")
        write(Path(config["dsh_launcher"]), wrapper, mode=0o755)
        sudo = (f"Defaults:{config['api_user']} env_reset\n"
                f"Defaults:{config['api_user']} env_keep += \"NEBIUS_API_KEY FORGEFLOW_ASSISTANT_EVIDENCE\"\n"
                f"{config['api_user']} ALL=({config['dsh_user']}) NOPASSWD: {config['python']} -I -m forgeflow.dsh_launcher *\n")
        sudo_path = Path("/etc/sudoers.d") / ("forgeflow-" + name)
        candidate = CONFIG_ROOT / "instances" / (name + ".sudoers.pending")
        write(candidate, sudo, mode=0o440)
        command(["visudo", "-cf", str(candidate)])
        os.replace(candidate, sudo_path)
        if not existing:
            code = codes[int(name[-1])] if name.startswith("judge") else secrets.token_urlsafe(36)
            environment = {key: source[key] for key in ENV_KEYS if source.get(key)}
            environment.update(FORGEFLOW_SESSION_SECRET=secrets.token_urlsafe(48),
                FORGEFLOW_INVITE_CODES=code, FORGEFLOW_TRUSTED_HOSTS=",".join(trusted_hosts))
            write(env_path, "".join(key + "=" + json.dumps(value) + "\n" for key, value in environment.items()))
            write_json(config_path, config)
            index["instances"][name] = {"id": name, "port": config["api_port"],
                "odoo_port": config["odoo_port"], "invite_code": code,
                "config": str(config_path), "service_unit": config["service_unit"]}
            # Persist each credential binding before an independently retryable start.
            write_json(INDEX_PATH, index)
        backup = backup_definition(config, base_backup)
        write_json(CONFIG_ROOT / "instances" / (name + ".backup.json"), backup)
    unit = Path(base["repo_root"]) / "deploy/vps/forgeflow-runtime@.service"
    write(Path("/etc/systemd/system/forgeflow-runtime@.service"), unit.read_text(), mode=0o644)
    command(["systemctl", "daemon-reload"])
    write_json(INDEX_PATH, index)
    return {"prepared": names, "legacy_unchanged": True, "started": False}


def start(names: list[str]) -> dict:
    import httpx

    checked_names(names)
    index = load_index()
    ready = []
    for name in names:
        item = index["instances"][name]
        path = Path(item["config"])
        config = configuration(trusted_json(path))
        command(["systemctl", "start", config["service_unit"]])
        deadline = time.monotonic() + 1800
        host = index["trusted_hosts"][0]
        with httpx.Client(timeout=5, headers={"Host": host, "X-Forwarded-Proto": "https",
                                              "Origin": "https://" + host}) as client:
            while time.monotonic() < deadline:
                try:
                    response = client.get(f"http://127.0.0.1:{config['api_port']}/api/health")
                    healthy = response.status_code == 200 and response.json().get("ok") is True
                except (httpx.HTTPError, ValueError):
                    healthy = False
                if healthy:
                    record = trusted_json(Path(config["sandbox_root"]) / "vps-instance.json")
                    if record.get("phase") != "ready":
                        raise ValueError("reviewer_started_without_ready_generation")
                    login = client.post(f"http://127.0.0.1:{config['api_port']}/api/auth/session",
                                        json={"invite_code": item["invite_code"]})
                    if login.status_code != 201 or not login.cookies.get("ff_session"):
                        raise ValueError("reviewer_authenticated_readiness_failed")
                    response = client.get(f"http://127.0.0.1:{config['api_port']}/api/environment",
                                          headers={"Cookie": "ff_session=" + login.cookies["ff_session"]})
                    if response.status_code != 200 or response.json().get("generation") != record["generation"]:
                        raise ValueError("reviewer_api_generation_binding_failed")
                    config["allow_initialize"] = False
                    write_json(path, config)
                    ready.append({"id": name, "generation": record["generation"]})
                    if name.startswith("judge"):
                        command(["systemctl", "enable", config["service_unit"]])
                    print(json.dumps({"instance_ready": ready[-1]}), flush=True)
                    break
                if command(["systemctl", "show", config["service_unit"], "--property=ActiveState", "--value"]) == "failed":
                    raise ValueError("reviewer_runtime_failed")
                time.sleep(2)
            else:
                raise ValueError("reviewer_start_timed_out")
    return {"ready": ready, "legacy_unchanged": True}


def tenant(item: dict) -> dict:
    return {"id": item["id"], "port": item["port"],
            "invite_sha256": [hashlib.sha256(item["invite_code"].encode()).hexdigest()]}


def gateway(public_tenant: str, *, include_qa: bool = False) -> dict:
    from forgeflow.judge_gateway import configuration as gateway_configuration

    index = load_index()
    path = CONFIG_ROOT / "gateway.json"
    previous = trusted_json(path) if path.exists() else {}
    if previous:
        gateway_configuration(previous)
    selected = [index["legacy"], *(item for name, item in sorted(index["instances"].items())
        if name.startswith("judge") or include_qa)]
    if public_tenant not in {item["id"] for item in selected}:
        raise ValueError("public_reviewer_instance_missing")
    tenants = [tenant(item) for item in selected]
    value = {**previous, "routing_secret": previous.get("routing_secret") or secrets.token_urlsafe(48),
        "trusted_hosts": index["trusted_hosts"], "public_tenant": public_tenant, "tenants": tenants}
    if previous and (previous["trusted_hosts"] != value["trusted_hosts"] or any(
        row not in tenants for row in previous["tenants"]
    )):
        raise ValueError("existing_gateway_routes_would_change_use_append")
    gateway_configuration(value)
    identity = system_user("forgeflow-gateway")
    write_json(path, value, mode=0o640, gid=identity.pw_gid)
    return {"gateway_config_ready": True, "public_tenant": public_tenant,
            "tenant_ids": [item["id"] for item in tenants], "public_proxy_unchanged": True}


def gateway_append(names: list[str]) -> dict:
    """Append registered routes without replacing existing routing or credentials."""
    from forgeflow.judge_gateway import configuration as gateway_configuration

    checked_names(names)
    index = load_index()
    path = CONFIG_ROOT / "gateway.json"
    if not path.exists():
        raise ValueError("existing_gateway_required")
    previous = trusted_json(path)
    gateway_configuration(previous)
    if previous["trusted_hosts"] != index["trusted_hosts"]:
        raise ValueError("gateway_trusted_hosts_changed")
    tenants = list(previous["tenants"])
    existing = {row["id"]: row for row in tenants}
    added = []
    for name in names:
        item = index["instances"][name]
        if item["id"] != name:
            raise ValueError("reviewer_route_binding_changed")
        row = tenant(item)
        if name in existing:
            if row != existing[name]:
                raise ValueError("reviewer_route_binding_changed")
        else:
            tenants.append(row)
            added.append(name)
    value = {**previous, "tenants": tenants}
    gateway_configuration(value)
    if added:
        identity = system_user("forgeflow-gateway")
        write_json(path, value, mode=0o640, gid=identity.pw_gid)
    return {"gateway_config_ready": True, "public_tenant": previous["public_tenant"],
            "tenant_ids": [row["id"] for row in tenants], "added": added,
            "changed": bool(added), "public_proxy_unchanged": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "start", "gateway-append"):
        child = commands.add_parser(name)
        child.add_argument("names", nargs="+", choices=REVIEWER_NAMES)
    child = commands.add_parser("gateway")
    child.add_argument("--public-tenant", required=True)
    child.add_argument("--include-qa", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("root operator required")
    os.umask(0o077)
    try:
        if args.command == "prepare":
            result = prepare(args.names)
        elif args.command == "start":
            result = start(args.names)
        elif args.command == "gateway-append":
            result = gateway_append(args.names)
        else:
            result = gateway(args.public_tenant, include_qa=args.include_qa)
        print(json.dumps(result), flush=True)
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        # Files and child diagnostics may include credentials. Do not print them.
        print(json.dumps({"error": "reviewer_operation_failed", "state_preserved": True}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
