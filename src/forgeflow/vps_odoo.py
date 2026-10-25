"""Persistent lifecycle for the pinned ERP-Bench Odoo task on a Linux VPS.

This file is also copied into the task image as a standalone, stdlib-only script.
Business setup and verifier files remain upstream bytes. A ready manifest binds
the volume, its immutable seed receipt, and the PostgreSQL cluster to one runtime
generation. An interrupted first initialization requires explicit diagnosis.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UPSTREAM_COMMIT = "ceba3880af555129b5278e056a0c20f2fb5a0ba9"
TASK = "2280_medium_repair_plan_medium"
DATA_PROFILE = "planning-metadata-v1"
ARTIFACT_FILES = ("api_key", "repair_seeded_records.json", "master_password",
                  "task-derivation.json", "planning-metadata-v1.diff")
ENTRYPOINT_SHA256 = "17081066295528704b6c9af5c66864884ea682640bcc9a154b03e862fe8ccbc5"
STATE_ROOT = Path("/var/lib/forgeflow")
READY = Path("/tmp/forgeflow_local_ready")
SENTINEL = Path("/tmp/saas_setup_complete")
KEY = Path("/etc/odoo/api_key")
SEED = Path("/tmp/repair_seeded_records.json")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class StateError(RuntimeError):
    """A bounded diagnostic without credential-bearing subprocess output."""


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def regular_bytes(path: Path, limit: int = 4 * 1024 * 1024) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise StateError("state_artifact_invalid")
        data = path.read_bytes()
    except OSError as exc:
        raise StateError("state_artifact_missing") from exc
    if not data:
        raise StateError("state_artifact_empty")
    return data


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def save_manifest(root: Path, state: dict) -> None:
    atomic_write(root / "state.json", (json.dumps(state, sort_keys=True) + "\n").encode())


def expected_binding(instance: str, generation: str) -> dict:
    if not NAME.fullmatch(instance) or not NAME.fullmatch(generation):
        raise StateError("instance_and_generation_required")
    return {"schema": 1, "instance_id": instance, "generation": generation,
            "task": TASK, "upstream_commit": UPSTREAM_COMMIT, "data_profile": DATA_PROFILE}


def validate_seed(data: bytes) -> None:
    try:
        seed = json.loads(data)
        if (not isinstance(seed, dict)
                or not isinstance(seed.get("purchase_orders"), dict)
                or not isinstance(seed.get("manufacturing_orders"), dict)):
            raise ValueError("shape")
    except (ValueError, TypeError) as exc:
        raise StateError("seed_receipt_invalid") from exc


def load_state(root: Path, binding: dict, allow_initialize: bool) -> tuple[dict, bool]:
    """Inspect state before any service start or write to PostgreSQL."""
    if root.is_symlink() or not root.is_dir():
        raise StateError("state_directory_invalid")
    manifest = root / "state.json"
    if not manifest.exists() and not manifest.is_symlink():
        if any(p.name != ".lock" for p in root.iterdir()):
            raise StateError("unrecognized_existing_state")
        if not allow_initialize:
            raise StateError("explicit_initialization_required")
        return {**binding, "phase": "initializing", "nonce": secrets.token_hex(32)}, True
    try:
        state = json.loads(regular_bytes(manifest, 32768))
    except (ValueError, TypeError) as exc:
        raise StateError("manifest_invalid") from exc
    if not isinstance(state, dict) or any(state.get(k) != v for k, v in binding.items()):
        raise StateError("generation_binding_mismatch")
    if state.get("phase") != "ready":
        raise StateError("incomplete_initialization_requires_diagnosis")
    if not re.fullmatch(r"[0-9a-f]{64}", str(state.get("nonce", ""))):
        raise StateError("manifest_invalid")
    if not isinstance(state.get("artifacts"), dict):
        raise StateError("manifest_invalid")
    for name in ("postgresql", "odoo", "logs"):
        path = root / name
        if path.is_symlink() or not path.is_dir():
            raise StateError("persistent_directory_missing")
    if regular_bytes(root / "postgresql" / "PG_VERSION", 8).strip() != b"18":
        raise StateError("postgres_version_mismatch")
    for name in ARTIFACT_FILES:
        value = regular_bytes(root / name)
        if digest(value) != state["artifacts"].get(name):
            raise StateError("persistent_artifact_mismatch")
        if name == "repair_seeded_records.json":
            validate_seed(value)
    return state, False


def preserving_source(source: bytes) -> bytes:
    """Guard only the pinned startup's initialization sections, never its seed."""
    if digest(source) != ENTRYPOINT_SHA256:
        raise StateError("upstream_entrypoint_unrecognized")
    anchors = (b"pg_ctlcluster 18 main start\n", b"su postgres -s /bin/bash -c ",
               b"export HOST=127.0.0.1\n", b"# Create database via HTTP POST",
               b"\nwhile true; do\n", b"python3 /setup/setup_scenario.py")
    if any(source.count(a) != 1 for a in (anchors[0], *anchors[2:])):
        raise StateError("upstream_entrypoint_unrecognized")
    first = b"if [ ! -f /tmp/saas_setup_complete ]; then # ForgeFlow first boot\n"
    close = b"fi # ForgeFlow first boot\n"
    patched = source.replace(anchors[0], b"pg_ctlcluster 18 main status >/dev/null 2>&1 || "
                             + anchors[0])
    patched = patched.replace(anchors[1], first + anchors[1], 1)
    patched = patched.replace(anchors[2], close + anchors[2])
    patched = patched.replace(anchors[3], first + anchors[3])
    patched = patched.replace(anchors[4], close + anchors[4])
    return patched.replace(anchors[5], b"python3 /opt/forgeflow/vps_odoo.py --run-original-setup")


def run(command: list[str], *, data: str | None = None) -> str:
    result = subprocess.run(command, input=data, text=True, capture_output=True, check=False)
    if result.returncode:
        raise StateError("runtime_command_failed:" + Path(command[0]).name)
    return result.stdout.strip()


def sql(statement: str) -> str:
    return run(["runuser", "-u", "postgres", "--", "psql", "-X", "-A", "-t",
                "-v", "ON_ERROR_STOP=1", "-d", "postgres"], data=statement)


def database_binding(state: dict) -> dict:
    return {key: state[key] for key in (
        "schema", "instance_id", "generation", "task", "upstream_commit", "data_profile",
        "nonce", "artifacts")}


def check_database_binding(state: dict, payload: str) -> None:
    try:
        actual = json.loads(payload)
    except ValueError as exc:
        raise StateError("database_binding_missing") from exc
    if actual != database_binding(state):
        raise StateError("database_binding_mismatch")


def write_database_binding(state: dict) -> None:
    # All values are constrained identifiers/constants/hex, but quote defensively.
    payload = json.dumps(database_binding(state), sort_keys=True).replace("'", "''")
    sql("CREATE SCHEMA forgeflow_runtime; "
        "CREATE TABLE forgeflow_runtime.binding (singleton boolean PRIMARY KEY "
        "CHECK (singleton), payload text NOT NULL); "
        f"INSERT INTO forgeflow_runtime.binding VALUES (true, '{payload}');")


def configure(root: Path, state: dict, fresh: bool) -> None:
    import pwd

    postgres = pwd.getpwnam("postgres")
    odoo = pwd.getpwnam("odoo")
    # Directory traversal is required by PostgreSQL and Odoo; secrets are 0600.
    os.chmod(root, 0o711)
    if fresh:
        # Commit the incomplete marker before initialization can modify the volume.
        save_manifest(root, state)
        for name, owner in (("postgresql", postgres), ("odoo", odoo)):
            path = root / name
            path.mkdir(mode=0o700)
            os.chown(path, owner.pw_uid, owner.pw_gid)
        (root / "logs" / "agent").mkdir(parents=True)
        (root / "logs" / "verifier").mkdir()
        (root / "logs" / "postgresql").mkdir(mode=0o700)
        os.chown(root / "logs" / "postgresql", postgres.pw_uid, postgres.pw_gid)
        atomic_write(root / "master_password", secrets.token_urlsafe(48).encode())
        atomic_write(root / "task-derivation.json",
                     regular_bytes(Path("/opt/forgeflow/task-derivation/derivation.json")))
        atomic_write(root / "planning-metadata-v1.diff",
                     regular_bytes(Path("/opt/forgeflow/task-derivation/planning-metadata-v1.diff")))
        run(["runuser", "-u", "postgres", "--", "/usr/lib/postgresql/18/bin/initdb",
             "-D", str(root / "postgresql"), "--auth-local=peer",
             "--auth-host=scram-sha-256", "--encoding=UTF8", "--locale=C.UTF-8"])
    pg_config = Path("/etc/postgresql/18/main/postgresql.conf")
    original_pg = Path("/opt/forgeflow/postgresql.conf").read_bytes()
    atomic_write(pg_config, original_pg + b"\ndata_directory = '/var/lib/forgeflow/postgresql'\n"
                 b"listen_addresses = '127.0.0.1'\nlogging_collector = on\n"
                 b"log_directory = '/var/lib/forgeflow/logs/postgresql'\n", 0o644)
    master = regular_bytes(root / "master_password", 256).decode("ascii")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", master):
        raise StateError("master_password_invalid")
    original = Path("/opt/forgeflow/upstream/odoo.conf").read_text()
    config = re.sub(r"(?m)^admin_passwd\s*=.*$", "admin_passwd = " + master, original)
    config += "\ndata_dir = /var/lib/forgeflow/odoo\n"
    if fresh:
        # Installing the original seven modules can exceed a minute on a shared
        # VPS. This changes startup transport/runtime limits, not scenario data.
        config += "limit_time_cpu = 1200\nlimit_time_real = 1800\n"
    atomic_write(Path("/etc/odoo/odoo.conf"), config.encode(), 0o640)
    os.chown("/etc/odoo/odoo.conf", 0, odoo.pw_gid)
    os.environ["ODOO_MASTER_PASSWORD"] = master
    if not fresh:
        if (regular_bytes(root / "task-derivation.json")
                != regular_bytes(Path("/opt/forgeflow/task-derivation/derivation.json"))):
            raise StateError("image_data_profile_changed")
        atomic_write(KEY, regular_bytes(root / "api_key"), 0o400)
        atomic_write(SEED, regular_bytes(root / "repair_seeded_records.json"))


def receipt() -> bytes:
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    # comm can contain spaces; field 22 is token 19 following its closing ')'.
    start = Path("/proc/1/stat").read_text().rsplit(")", 1)[1].split()[19]
    return f"{boot}\n{start}\n".encode()


def http_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8069/web/database/selector",
                                    timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def serve() -> int:
    import fcntl

    if os.geteuid() != 0:
        raise StateError("root_required")
    root = STATE_ROOT
    if not root.is_mount():
        # Bind mounts are not always os.path.ismount() on Linux; mountinfo is authoritative.
        mounted = any(line.split()[4] == str(root)
                      for line in Path("/proc/self/mountinfo").read_text().splitlines())
        if not mounted:
            raise StateError("persistent_mount_required")
    if root.is_symlink() or not root.is_dir():
        raise StateError("state_directory_invalid")
    lock_fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StateError("state_directory_already_running") from exc
        READY.unlink(missing_ok=True)
        SENTINEL.unlink(missing_ok=True)
        binding = expected_binding(os.getenv("FORGEFLOW_INSTANCE_ID", ""),
                                   os.getenv("FORGEFLOW_GENERATION", ""))
        state, fresh = load_state(root, binding, os.getenv("FORGEFLOW_ALLOW_INITIALIZE") == "1")
        child = None
        stop_requested = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal stop_requested
            stop_requested = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            configure(root, state, fresh)
            run(["pg_ctlcluster", "18", "main", "start"])
            if not fresh:
                check_database_binding(state, sql("SELECT payload FROM forgeflow_runtime.binding;"))
                SENTINEL.touch(mode=0o600)
            child = subprocess.Popen(["/usr/local/bin/harbor-entrypoint.sh"],
                                     start_new_session=True)
            deadline = time.monotonic() + 1800
            while not stop_requested:
                if child.poll() is not None:
                    raise StateError("upstream_entrypoint_exited")
                if SENTINEL.is_file() and http_ready():
                    break
                if time.monotonic() > deadline:
                    raise StateError("initialization_timeout")
                time.sleep(1)
            if stop_requested:
                return 0
            if fresh:
                seed = regular_bytes(SEED)
                validate_seed(seed)
                atomic_write(root / "api_key", regular_bytes(KEY, 4096), 0o400)
                os.chmod(KEY, 0o400)
                atomic_write(root / "repair_seeded_records.json", seed)
                state["artifacts"] = {name: digest(regular_bytes(root / name))
                                      for name in ARTIFACT_FILES}
                write_database_binding(state)
                state["phase"] = "ready"
                save_manifest(root, state)
            atomic_write(READY, receipt())
            print("ForgeFlow persistent Odoo generation ready", flush=True)
            while not stop_requested:
                if child.poll() is not None:
                    raise StateError("upstream_entrypoint_exited")
                time.sleep(1)
            return 0
        finally:
            READY.unlink(missing_ok=True)
            SENTINEL.unlink(missing_ok=True)
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)
            subprocess.run(["pg_ctlcluster", "18", "main", "stop", "--mode", "fast"],
                           capture_output=True, check=False, timeout=60)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--patch-entrypoint", type=Path)
    parser.add_argument("--run-original-setup", action="store_true")
    args = parser.parse_args()
    try:
        if args.patch_entrypoint:
            path = args.patch_entrypoint
            atomic_write(path, preserving_source(path.read_bytes()), 0o755)
            return 0
        if args.run_original_setup:
            import runpy

            from odoolib import json2

            # The pinned setup client hardcodes 60 seconds for module-install
            # RPC. Override only this setup process; tests run in another Python
            # process with the original client behavior and original source bytes.
            json2.DEFAULT_TIMEOUT = 900
            sys.argv = ["/setup/setup_scenario.py"]
            runpy.run_path(sys.argv[0], run_name="__main__")
            return 0
        return serve()
    except (StateError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        reason = str(exc) if isinstance(exc, StateError) else type(exc).__name__
        print("ForgeFlow Odoo refused startup: " + reason, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
