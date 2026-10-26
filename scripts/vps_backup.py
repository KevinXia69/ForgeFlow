#!/usr/bin/env python3
"""Manual, stopped-writer VPS backups; encrypted archives are never public.

create requires an operator-owned JSON configuration and already stopped writer
services. It never stops/starts the application or claims Drive upload succeeded
without both transfer and remote checksum verification. verify checks a complete
archive; restore-staging extracts to a NEW directory, never a live deployment.
"""
from __future__ import annotations

import argparse
import configparser
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAGIC = b"FORGEFLOW-BACKUP-AES256GCM-V1\n"
CHUNK = 1024 * 1024


class BackupError(RuntimeError):
    pass


def run(argv, *, input_text=None, timeout=600):
    result = subprocess.run(argv, input=input_text, text=True, capture_output=True,
                            check=False, timeout=timeout)
    if result.returncode:
        # Child stderr can contain PostgreSQL credentials or Drive tokens.
        raise BackupError("command_failed:" + Path(argv[0]).name)
    return result.stdout.strip()


def secure_file(path: Path) -> bytes:
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (os.name == "posix" and (info.st_uid != 0 or info.st_mode & 0o077))):
        raise BackupError("private_root_owned_file_required")
    return path.read_bytes()


def read_key(path: Path) -> bytes:
    key = secure_file(path)
    if len(key) != 32:
        raise BackupError("encryption_key_must_be_32_random_bytes")
    return key


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def private_json(path: Path, value: dict) -> None:
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def clean_path(value) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise BackupError("absolute_paths_required")
    path = Path(value)
    if path.is_symlink() or path.resolve() != path:
        raise BackupError("symlink_or_noncanonical_path_refused")
    return path


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict):
        raise BackupError("invalid_config")
    result = dict(config)
    for key in ("project_root", "odoo_state", "runtime_root", "case_root", "api_data_root",
                "encryption_key_file", "output_root"):
        result[key] = clean_path(config.get(key))
    name = config.get("odoo_container", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name):
        raise BackupError("invalid_container_name")
    units = config.get("maintenance_units")
    if (not isinstance(units, list) or not units
            or any(not isinstance(u, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", u)
                   for u in units)):
        raise BackupError("explicit_writer_units_required")
    extras = config.get("extra_state_roots", {})
    if not isinstance(extras, dict):
        raise BackupError("invalid_extra_state_roots")
    result["extra_state_roots"] = {}
    for label, value in extras.items():
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", label)
                or label in {"odoo", "runtime", "cases", "api-data", "database", "secrets"}):
            raise BackupError("invalid_extra_state_label")
        result["extra_state_roots"][label] = clean_path(value)
    roots = [result[k] for k in ("odoo_state", "runtime_root", "case_root", "api_data_root")]
    roots.extend(result["extra_state_roots"].values())
    for root in roots:
        if root == Path(root.anchor) or inside(result["project_root"], root):
            raise BackupError("backup_root_too_broad")
        if inside(result["output_root"], root) or inside(result["encryption_key_file"], root):
            raise BackupError("backup_output_and_key_must_be_outside_state")
    if inside(result["encryption_key_file"], result["project_root"]):
        raise BackupError("encryption_key_must_be_outside_repository")
    secrets = config.get("secret_files", {})
    if not isinstance(secrets, dict):
        raise BackupError("explicit_secret_files_required")
    result["secret_files"] = {}
    for label, value in secrets.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", label):
            raise BackupError("invalid_secret_label")
        path = clean_path(value)
        if path == result["encryption_key_file"] or ".ssh" in path.parts:
            raise BackupError("backup_key_and_ssh_keys_are_not_archive_content")
        result["secret_files"][label] = path
    return result


@contextmanager
def lease(path: Path):
    import fcntl

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise BackupError("unsafe_maintenance_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BackupError("writer_or_backup_already_active") from exc
        yield
    finally:
        os.close(fd)


def assert_writers_stopped(config: dict) -> str:
    for unit in config["maintenance_units"]:
        loaded = run(["systemctl", "show", unit, "--property=LoadState", "--value"])
        active = run(["systemctl", "show", unit, "--property=ActiveState", "--value"])
        if loaded != "loaded" or active not in {"inactive", "failed"}:
            raise BackupError("writer_service_not_stopped")
        if run(["systemctl", "show", unit, "--property=MainPID", "--value"]) != "0":
            raise BackupError("writer_service_main_pid_present")
        group = run(["systemctl", "show", unit, "--property=ControlGroup", "--value"])
        if group:
            if not group.startswith("/") or ".." in PurePosixPath(group).parts:
                raise BackupError("invalid_service_cgroup")
            processes = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.procs"
            if processes.exists() and processes.read_text().strip():
                raise BackupError("writer_service_processes_present")
    for name in ("api", "executor"):
        path = config["runtime_root"] / (name + ".pid")
        if path.exists():
            record = json.loads(secure_file(path))
            if not isinstance(record, dict) or not isinstance(record.get("identity"), dict):
                raise BackupError("managed_writer_record_requires_diagnosis")
            pid = record.get("identity", {}).get("pid")
            # Conservatively reject reused PIDs too; an operator must resolve them.
            if isinstance(pid, int) and Path(f"/proc/{pid}").exists():
                raise BackupError("managed_writer_pid_still_present")
    container = config["odoo_container"]
    state = json.loads(run(["docker", "inspect", "--format", "{{json .State}}", container]))
    if state.get("Running") or state.get("Paused") or state.get("Restarting"):
        raise BackupError("odoo_must_be_stopped_for_consistent_filestore")
    mounts = json.loads(run(["docker", "inspect", "--format", "{{json .Mounts}}", container]))
    matched = [m for m in mounts if m.get("Destination") == "/var/lib/forgeflow"]
    if (len(matched) != 1 or matched[0].get("Type") != "bind"
            or matched[0].get("Source") != str(config["odoo_state"])):
        raise BackupError("odoo_mount_binding_mismatch")
    image = run(["docker", "inspect", "--format", "{{.Image}}", container])
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise BackupError("immutable_image_id_required")
    return image


def check_generation_binding(config: dict, state: dict, image: str,
                             runtime_config: dict, instance: dict) -> None:
    expected = {"repo_root": str(config["project_root"]),
                "sandbox_root": str(config["runtime_root"]),
                "case_root": str(config["case_root"]), "data_root": str(config["api_data_root"]),
                "container": config["odoo_container"]}
    if any(runtime_config.get(key) != value for key, value in expected.items()):
        raise BackupError("backup_configuration_stale_after_runtime_change")
    if (instance.get("phase") != "ready" or instance.get("image_id") != image
            or instance.get("volume") != str(config["odoo_state"])
            or instance.get("container") != config["odoo_container"]
            or instance.get("instance_id") != state.get("instance_id")
            or instance.get("generation") != state.get("generation")):
        raise BackupError("runtime_and_odoo_generation_mismatch")


# Executed only inside the same pinned image, without starting the Odoo process.
# The host holds the data-volume flock throughout this controlled PG session.
DUMP_PROGRAM = r'''
import importlib.util, json, pathlib, signal, subprocess
def stop(*unused):
    raise SystemExit("backup_stopped")
signal.signal(signal.SIGTERM, stop)
spec = importlib.util.spec_from_file_location("vps_odoo", "/opt/forgeflow/vps_odoo.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
root = pathlib.Path("/var/lib/forgeflow")
saved = json.loads(m.regular_bytes(root / "state.json"))
binding = m.expected_binding(saved["instance_id"], saved["generation"])
state, fresh = m.load_state(root, binding, False)
try:
    m.configure(root, state, False)
    m.run(["pg_ctlcluster", "18", "main", "start"])
    m.check_database_binding(state, m.sql("SELECT payload FROM forgeflow_runtime.binding;"))
    destination = pathlib.Path("/backup-dumps")
    for database, filename, extra in [
        ("bench", "bench.dump", ["--create"]),
        ("postgres", "runtime-binding.dump", ["--schema=forgeflow_runtime"]),
    ]:
        with (destination / filename).open("wb") as out:
            p = subprocess.run(["runuser", "-u", "postgres", "--", "pg_dump",
                                "--format=custom", "--dbname=" + database, *extra],
                               stdout=out, stderr=subprocess.PIPE)
        if p.returncode:
            raise SystemExit("logical_dump_failed")
    with (destination / "globals.sql").open("wb") as out:
        p = subprocess.run(["runuser", "-u", "postgres", "--", "pg_dumpall", "--globals-only"],
                           stdout=out, stderr=subprocess.PIPE)
    if p.returncode:
        raise SystemExit("globals_dump_failed")
    (destination / "binding.json").write_text(json.dumps(m.database_binding(state), sort_keys=True))
finally:
    subprocess.run(["pg_ctlcluster", "18", "main", "stop", "--mode", "fast"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
'''


def make_dumps(config: dict, image: str, destination: Path):
    name = "forgeflow-backup-" + uuid.uuid4().hex
    command = ["docker", "run", "--rm", "-i", "--name", name, "--network", "none",
               "--mount", f"type=bind,src={config['odoo_state']},dst=/var/lib/forgeflow",
               "--mount", f"type=bind,src={destination},dst=/backup-dumps",
               "--entrypoint", "python3", image, "-"]
    try:
        run(command, input_text=DUMP_PROGRAM, timeout=900)
    finally:
        # A timed-out CLI must not leave a PG container accessing the backup volume.
        subprocess.run(["docker", "stop", "--time", "120", name], capture_output=True,
                       check=False, timeout=135)
    for name in ("bench.dump", "runtime-binding.dump", "globals.sql", "binding.json"):
        if not (destination / name).is_file() or (destination / name).stat().st_size == 0:
            raise BackupError("logical_dump_incomplete")


def gather_files(config: dict, dumps: Path) -> tuple[list[tuple[str, Path]], dict]:
    items = []
    directories_record = {}
    roots = [("odoo", config["odoo_state"]), ("runtime", config["runtime_root"]),
             ("cases", config["case_root"]), ("api-data", config["api_data_root"]),
             ("database", dumps), *config.get("extra_state_roots", {}).items()]
    for label, root in roots:
        if root.is_symlink() or not root.is_dir():
            raise BackupError("required_state_root_missing")
        for current, directories, files in os.walk(root, followlinks=False):
            current = Path(current)
            relative = current.relative_to(root).as_posix()
            directory_name = label if relative == "." else label + "/" + relative
            info = current.stat()
            directories_record[directory_name] = {
                "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
            for name in list(directories):
                path = current / name
                if label == "odoo" and current == root and name == "postgresql":
                    directories.remove(name)
                    continue
                # Each explicitly named root has its own archive namespace. In
                # particular runtime_root may contain the separately handled
                # Odoo volume, whose raw PostgreSQL files must stay excluded.
                if any(path == other for _, other in roots if other != root):
                    directories.remove(name)
                    continue
                if path.is_symlink() or name in {".git", ".ssh"}:
                    raise BackupError("unsafe_directory_in_state")
            for name in files:
                if name.endswith(".lock") or name == ".lock":
                    continue
                path = current / name
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise BackupError("nonregular_state_file")
                items.append((label + "/" + path.relative_to(root).as_posix(), path))
    for label, path in config["secret_files"].items():
        secure_file(path)
        items.append(("secrets/" + label, path))
    return sorted(items), directories_record


class EncryptingWriter:
    def __init__(self, destination, key):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        self.destination = destination
        nonce = os.urandom(12)
        self.encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        self.encryptor.authenticate_additional_data(MAGIC)
        destination.write(MAGIC + nonce)

    def write(self, data):
        self.destination.write(self.encryptor.update(data))
        return len(data)

    def finish(self):
        self.destination.write(self.encryptor.finalize())
        self.destination.write(self.encryptor.tag)
        self.destination.flush()
        os.fsync(self.destination.fileno())

    def flush(self):
        self.destination.flush()


class DigestReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, count=-1):
        data = self.stream.read(count)
        self.digest.update(data)
        return data


def encrypt_archive(destination: Path, key: bytes, files, metadata: dict, *, compress=False):
    records = {}
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        encrypted = EncryptingWriter(output, key)
        compressed = gzip.GzipFile(fileobj=encrypted, mode="wb", compresslevel=1, mtime=0) if compress else None
        with tarfile.open(mode="w|", fileobj=compressed or encrypted) as archive:
            for name, path in files:
                info = archive.gettarinfo(str(path), arcname=name)
                info.mode = stat.S_IMODE(info.mode)
                if not info.isfile() or name in records:
                    raise BackupError("invalid_archive_input")
                with path.open("rb") as source:
                    reader = DigestReader(source)
                    archive.addfile(info, reader)
                records[name] = {"sha256": reader.digest.hexdigest(), "size": info.size,
                                 "mode": info.mode, "uid": info.uid, "gid": info.gid}
            payload = json.dumps({**metadata, "files": records}, sort_keys=True).encode()
            info = tarfile.TarInfo("backup-manifest.json")
            info.size, info.mode = len(payload), 0o600
            archive.addfile(info, io.BytesIO(payload))
        if compressed:
            compressed.close()
        encrypted.finish()


def decrypt_to(archive: Path, key: bytes, plaintext: Path):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    size = archive.stat().st_size
    if size < len(MAGIC) + 12 + 16:
        raise BackupError("invalid_encrypted_archive")
    try:
        with archive.open("rb") as source, plaintext.open("xb") as destination:
            os.chmod(plaintext, 0o600)
            if source.read(len(MAGIC)) != MAGIC:
                raise BackupError("invalid_encrypted_archive")
            nonce = source.read(12)
            source.seek(-16, os.SEEK_END)
            tag = source.read(16)
            source.seek(len(MAGIC) + 12)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(MAGIC)
            remaining = size - len(MAGIC) - 12 - 16
            while remaining:
                chunk = source.read(min(CHUNK, remaining))
                if not chunk:
                    raise BackupError("truncated_archive")
                remaining -= len(chunk)
                destination.write(decryptor.update(chunk))
            destination.write(decryptor.finalize())
    except (InvalidTag, BackupError):
        plaintext.unlink(missing_ok=True)
        raise BackupError("archive_authentication_failed") from None


def verify_plaintext(path: Path) -> dict:
    actual = {}
    metadata = None
    with tarfile.open(path, "r:*") as archive:
        for item in archive:
            name = PurePosixPath(item.name)
            if (not item.isfile() or name.is_absolute() or ".." in name.parts
                    or str(name) != item.name or item.name in actual):
                raise BackupError("unsafe_or_duplicate_archive_member")
            stream = archive.extractfile(item)
            if item.name == "backup-manifest.json":
                if metadata is not None or item.size > 64 * CHUNK:
                    raise BackupError("invalid_archive_manifest")
                metadata = json.load(stream)
                continue
            digest = hashlib.sha256()
            while chunk := stream.read(CHUNK):
                digest.update(chunk)
            actual[item.name] = {"sha256": digest.hexdigest(), "size": item.size,
                                 "mode": item.mode, "uid": item.uid, "gid": item.gid}
    if (not isinstance(metadata, dict) or metadata.get("schema") != 1
            or metadata.get("files") != actual):
        raise BackupError("archive_manifest_mismatch")
    directories = metadata.get("directories", {})
    if not isinstance(directories, dict):
        raise BackupError("invalid_directory_manifest")
    for name, record in directories.items():
        candidate = PurePosixPath(name)
        if (not name or candidate.is_absolute() or ".." in candidate.parts
                or str(candidate) != name or name in actual
                or not isinstance(record, dict)
                or any(type(record.get(k)) is not int for k in ("mode", "uid", "gid"))):
            raise BackupError("invalid_directory_manifest")
    return metadata


def verify_archive(archive: Path, key: bytes, destination: Path | None = None) -> dict:
    if destination is not None and destination.exists():
        raise BackupError("restore_destination_must_not_exist")
    # Authentication completes before any archive member is interpreted/extracted.
    with tempfile.TemporaryDirectory(prefix="forgeflow-restore-") as temporary:
        plaintext = Path(temporary) / "archive.tar"
        decrypt_to(archive, key, plaintext)
        metadata = verify_plaintext(plaintext)
        if destination is not None:
            destination.mkdir(mode=0o700, parents=False)
            for name in metadata.get("directories", {}):
                destination.joinpath(*PurePosixPath(name).parts).mkdir(
                    mode=0o700, parents=True, exist_ok=True)
            with tarfile.open(plaintext, "r:*") as source:
                for item in source:
                    path = destination.joinpath(*PurePosixPath(item.name).parts)
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with path.open("xb") as output:
                        shutil.copyfileobj(source.extractfile(item), output, CHUNK)
                    if os.name == "posix" and os.geteuid() == 0:
                        os.chown(path, item.uid, item.gid)
                    os.chmod(path, item.mode & 0o7777)
                    os.utime(path, (item.mtime, item.mtime))
            for name, record in metadata.get("directories", {}).items():
                path = destination.joinpath(*PurePosixPath(name).parts)
                if os.name == "posix" and os.geteuid() == 0:
                    os.chown(path, record["uid"], record["gid"])
                os.chmod(path, record["mode"] & 0o7777)
    return metadata


def verify_staging(staging: Path) -> dict:
    """Recheck extracted bytes before a database restore test consumes them."""
    metadata = json.loads((staging / "backup-manifest.json").read_text())
    if metadata.get("schema") != 1 or not isinstance(metadata.get("files"), dict):
        raise BackupError("invalid_staging_manifest")
    actual = set()
    for path in staging.rglob("*"):
        if path.is_symlink():
            raise BackupError("staging_symlink_refused")
        if path.is_file() and path != staging / "backup-manifest.json":
            actual.add(path.relative_to(staging).as_posix())
    if actual != set(metadata["files"]):
        raise BackupError("staging_file_set_changed")
    for name, record in metadata["files"].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
            raise BackupError("invalid_staging_member")
        path = staging.joinpath(*relative.parts)
        if path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
            raise BackupError("invalid_staging_member")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(CHUNK):
                digest.update(chunk)
        if path.stat().st_size != record["size"] or digest.hexdigest() != record["sha256"]:
            raise BackupError("staging_content_changed")
    return metadata


RESTORE_PROGRAM = r'''
import importlib.util, json, os, pathlib, pwd, signal, subprocess
def stop(*unused):
    raise SystemExit("restore_check_stopped")
signal.signal(signal.SIGTERM, stop)
spec = importlib.util.spec_from_file_location("vps_odoo", "/opt/forgeflow/vps_odoo.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
root, dumps = pathlib.Path("/var/lib/forgeflow"), pathlib.Path("/restore-dumps")
saved = json.loads(m.regular_bytes(root / "state.json"))
postgres = pwd.getpwnam("postgres")
os.chmod(root, 0o711)
data = root / "postgresql"
data.mkdir(mode=0o700)
os.chown(data, postgres.pw_uid, postgres.pw_gid)
m.run(["runuser", "-u", "postgres", "--", "/usr/lib/postgresql/18/bin/initdb",
       "-D", str(data), "--auth-local=peer", "--auth-host=scram-sha-256",
       "--encoding=UTF8", "--locale=C.UTF-8"])
state, fresh = m.load_state(root, m.expected_binding(saved["instance_id"], saved["generation"]), False)
try:
    m.configure(root, state, False)
    m.run(["pg_ctlcluster", "18", "main", "start"])
    globals_sql = (dumps / "globals.sql").read_text()
    bootstrap = "CREATE ROLE postgres;"
    if globals_sql.splitlines().count(bootstrap) != 1:
        raise SystemExit("unexpected_bootstrap_role_dump")
    # Only this already-existing initdb role is omitted; all remaining SQL is
    # executed with ON_ERROR_STOP, including its original ALTER ROLE properties.
    globals_sql = "\n".join(line for line in globals_sql.splitlines() if line != bootstrap) + "\n"
    m.sql(globals_sql)
    for name, extra in [("bench.dump", ["--create"]), ("runtime-binding.dump", [])]:
        with (dumps / name).open("rb") as source:
            result = subprocess.run(["runuser", "-u", "postgres", "--", "pg_restore",
                                     "--exit-on-error", "--dbname=postgres", *extra],
                                    stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise SystemExit("logical_restore_failed")
    m.check_database_binding(state, m.sql("SELECT payload FROM forgeflow_runtime.binding;"))
    counts = {}
    for table in ("sale_order", "purchase_order", "mrp_production", "stock_picking"):
        counts[table] = int(m.run(["runuser", "-u", "postgres", "--", "psql", "-XAt",
                                   "-v", "ON_ERROR_STOP=1", "-d", "bench", "-c",
                                   "SELECT count(*) FROM " + table]))
    print(json.dumps({"database_restored": True, "binding_verified": True,
                      "business_row_counts": counts, "application_workflow": "not_tested"}))
finally:
    subprocess.run(["pg_ctlcluster", "18", "main", "stop", "--mode", "fast"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
'''


def restore_database_check(staging: Path, destination: Path) -> dict:
    if os.name != "posix" or os.geteuid() != 0:
        raise BackupError("linux_root_required")
    staging, destination = clean_path(str(staging)), clean_path(str(destination))
    if destination.exists() or inside(destination, staging):
        raise BackupError("restore_check_destination_must_be_new")
    metadata = verify_staging(staging)
    image = metadata.get("odoo_image_id", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise BackupError("immutable_image_id_required")
    source_state = staging / "odoo"
    if (source_state / "postgresql").exists():
        raise BackupError("restore_requires_logical_dump_not_raw_postgres")
    destination.mkdir(mode=0o700)
    state = destination / "odoo"
    shutil.copytree(source_state, state)
    for path in [source_state, *source_state.rglob("*")]:
        if path.is_symlink():
            raise BackupError("staging_symlink_refused")
        relative = path.relative_to(source_state)
        target = state / relative
        info = path.stat()
        os.chown(target, info.st_uid, info.st_gid)
        os.chmod(target, stat.S_IMODE(info.st_mode))
    name = "forgeflow-restore-check-" + uuid.uuid4().hex
    try:
        output = run(["docker", "run", "--rm", "-i", "--name", name, "--network", "none",
                      "--mount", f"type=bind,src={state},dst=/var/lib/forgeflow",
                      "--mount", f"type=bind,src={staging / 'database'},dst=/restore-dumps,readonly",
                      "--entrypoint", "python3", image, "-"],
                     input_text=RESTORE_PROGRAM, timeout=900)
        result = json.loads(output.splitlines()[-1])
        if result.get("database_restored") is not True or result.get("binding_verified") is not True:
            raise BackupError("database_restore_check_incomplete")
        result["restored_odoo_state"] = str(state)
        result["odoo_image_id"] = image
        result["instance_id"] = metadata["runtime_binding"]["instance_id"]
        result["generation"] = metadata["runtime_binding"]["generation"]
        receipt = destination / "database-restore-check.json"
        private_json(receipt, result)
        return result
    finally:
        subprocess.run(["docker", "stop", "--time", "120", name], capture_output=True,
                       check=False, timeout=135)


def restore_app_command(name: str, state: Path, image: str, binding: dict) -> list[str]:
    """No initialization permission, host networking, public port or live mount."""
    return ["docker", "create", "--name", name, "--network", "none", "--stop-timeout", "120",
            "--mount", f"type=bind,src={state},dst=/var/lib/forgeflow",
            "--env", "UV_OFFLINE=1", "--env", "FORGEFLOW_INSTANCE_ID=" + binding["instance_id"],
            "--env", "FORGEFLOW_GENERATION=" + binding["generation"], image]


def refuse_attached_restore_state(state: Path) -> None:
    # Also reject stopped containers: a previously live/still-owned volume is not
    # an isolated restore destination. No environment values are inspected.
    state = clean_path(str(state))
    if not state.is_dir():
        raise BackupError("restore_state_directory_missing")
    containers = run(["docker", "ps", "-aq"]).splitlines()
    for container in containers:
        mounts = json.loads(run(["docker", "inspect", "--format", "{{json .Mounts}}", container]))
        for mount in mounts:
            source = mount.get("Source")
            if isinstance(source, str) and Path(source).is_absolute():
                attached = Path(source).resolve()
                if inside(state, attached) or inside(attached, state):
                    raise BackupError("restore_state_already_attached_to_container")


ATTACHMENT_CHECK = r'''
import hashlib, json, pathlib, re
import odoolib
key = pathlib.Path("/etc/odoo/api_key").read_text().strip()
connection = odoolib.get_connection(hostname="127.0.0.1", port=8069,
                                  database="bench", password=key, protocol="json2")
connection.get_model("res.users").context_get()
attachments = connection.get_model("ir.attachment").search_read(
    domain=[["store_fname", "!=", False]], fields=["store_fname", "checksum"])
root = pathlib.Path("/var/lib/forgeflow/odoo/filestore/bench")
checked = {}
for attachment in attachments:
    name, expected = attachment["store_fname"], attachment["checksum"]
    relative = pathlib.PurePosixPath(name)
    path = root.joinpath(*relative.parts)
    if relative.is_absolute() or ".." in relative.parts or path.resolve() != path:
        raise SystemExit("unsafe_attachment_reference")
    if not re.fullmatch(r"[0-9a-f]{40}", expected or "") or not path.is_file():
        raise SystemExit("missing_attachment_file")
    if name not in checked:
        digest = hashlib.sha1(usedforsecurity=False)
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024): digest.update(chunk)
        checked[name] = digest.hexdigest()
    if checked[name] != expected:
        raise SystemExit("attachment_checksum_mismatch")
print(json.dumps({"api_key_authenticated": True, "attachment_rows": len(attachments),
                  "attachment_files_verified": len(checked)}))
'''


def restore_application_check(staging: Path, database_check: Path) -> dict:
    if os.name != "posix" or os.geteuid() != 0:
        raise BackupError("linux_root_required")
    staging, database_check = clean_path(str(staging)), clean_path(str(database_check))
    metadata = verify_staging(staging)
    receipt = json.loads(secure_file(database_check / "database-restore-check.json"))
    state = clean_path(str(database_check / "odoo"))
    if not state.is_dir():
        raise BackupError("restore_state_directory_missing")
    image, binding = metadata["odoo_image_id"], metadata["runtime_binding"]
    if (receipt.get("database_restored") is not True or receipt.get("binding_verified") is not True
            or receipt.get("restored_odoo_state") != str(state)
            or receipt.get("odoo_image_id") != image
            or receipt.get("instance_id") != binding["instance_id"]
            or receipt.get("generation") != binding["generation"]):
        raise BackupError("database_restore_receipt_mismatch")
    saved = json.loads(secure_file(state / "state.json"))
    if saved.get("phase") != "ready" or any(saved.get(k) != v for k, v in binding.items()):
        raise BackupError("restored_generation_binding_mismatch")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from forgeflow.local_entrypoint import READY_CHECK
    from forgeflow.product.verifier import OriginalProductVerifier, project_original_reward

    original_reward = staging / "odoo/logs/verifier/reward.json"
    # Its bytes were authenticated by verify_staging; the upstream output may
    # legitimately be 0644 inside the otherwise private restored directory.
    if original_reward.stat().st_size > CHUNK:
        raise BackupError("original_verifier_baseline_too_large")
    baseline = project_original_reward(json.loads(original_reward.read_text()))
    if baseline.get("strict_success") is not True or baseline.get("evaluation_valid") is not True:
        raise BackupError("backup_has_no_successful_original_verifier_baseline")
    name = "forgeflow-restore-app-" + uuid.uuid4().hex
    result = {"container": name, "state": str(state), "application_recovered": False,
              "original_verifier_passed": False, "public_ports": False,
              "forgeflow_api_and_approval_service_restarted": False}
    with lease(database_check / ".app-check.lock"):
        refuse_attached_restore_state(state)
        run(restore_app_command(name, state, image, binding))
        try:
            run(["docker", "start", name])
            deadline = time.monotonic() + 300
            while True:
                ready = subprocess.run(["docker", "exec", name, "sh", "-c", READY_CHECK],
                                       capture_output=True, check=False, timeout=15)
                if ready.returncode == 0:
                    break
                status = json.loads(run(["docker", "inspect", "--format", "{{json .State}}", name]))
                if not status.get("Running") or time.monotonic() >= deadline:
                    raise BackupError("restored_odoo_not_ready")
                time.sleep(2)
            # The setup files still exist in a restored image. The absence of
            # initialization permission plus persisted bindings forces recovery.
            authentication = json.loads(run(["docker", "exec", name, "python3", "-c",
                                              ATTACHMENT_CHECK], timeout=180))
            result.update(authentication)
            verification = OriginalProductVerifier(name, timeout=300)()
            result.update({"verifier_status": verification.get("status"),
                           "score": verification.get("score"),
                           "strict_success": verification.get("strict_success"),
                           "evaluation_valid": verification.get("evaluation_valid"),
                           "verification_attempt": verification.get("verification_attempt")})
            result["original_verifier_passed"] = (verification.get("strict_success") is True
                                                   and verification.get("evaluation_valid") is True)
            # Credentials and original seeded identities may not change during
            # a successful restore, even if ordinary caches/logs are updated.
            after = json.loads(secure_file(state / "state.json"))
            if after != saved:
                raise BackupError("restored_state_reinitialized")
            result["application_recovered"] = (result["original_verifier_passed"]
                                                and authentication.get("api_key_authenticated") is True)
            if not result["application_recovered"]:
                raise BackupError("restored_original_verifier_did_not_pass")
            return result
        finally:
            stopped = subprocess.run(["docker", "stop", "--time", "120", name], capture_output=True,
                                     check=False, timeout=135)
            result["container_stopped"] = stopped.returncode == 0
            output = database_check / "application-restore-check.json"
            private_json(output, result)
            if stopped.returncode:
                raise BackupError("restore_container_shutdown_failed")


def validate_docker_image_tar(path: Path, image: str) -> None:
    """Check a single digest-selected Docker save archive without extracting it."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise BackupError("immutable_image_id_required")
    names = set()
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            name = PurePosixPath(member.name.rstrip("/"))
            if (not (member.isfile() or member.isdir()) or name.is_absolute()
                    or ".." in name.parts or str(name) != member.name.rstrip("/")
                    or member.name in names or member.size < 0):
                raise BackupError("unsafe_docker_image_tar")
            names.add(member.name)
        try:
            manifest = archive.getmember("manifest.json")
            if manifest.size > CHUNK:
                raise BackupError("invalid_docker_image_manifest")
            entries = json.load(archive.extractfile(manifest))
            if (not isinstance(entries, list) or len(entries) != 1
                    or entries[0].get("RepoTags") not in (None, [])
                    or not isinstance(entries[0].get("Config"), str)):
                raise BackupError("image_archive_must_be_single_untagged_image")
            config = archive.getmember(entries[0]["Config"])
            if not config.isfile() or config.size > 4 * CHUNK:
                raise BackupError("invalid_image_config")
            actual = "sha256:" + hashlib.sha256(archive.extractfile(config).read()).hexdigest()
            if "index.json" in names:
                # Docker's containerd image store identifies images by OCI
                # manifest digest, while the classic store uses config digest.
                index_member = archive.getmember("index.json")
                if index_member.size > CHUNK:
                    raise BackupError("invalid_oci_image_index")
                index = json.load(archive.extractfile(index_member))
                descriptors = index.get("manifests", [])
                if not isinstance(descriptors, list) or len(descriptors) != 1:
                    raise BackupError("single_oci_descriptor_required")
                for node in (index, descriptors[0]):
                    annotations = node.get("annotations", {})
                    if (not isinstance(annotations, dict) or any(key in annotations for key in (
                            "io.containerd.image.name", "org.opencontainers.image.ref.name"))):
                        raise BackupError("tagged_oci_index_refused")
                manifest_id = descriptors[0].get("digest", "")
                if (not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_id)
                        or image not in {actual, manifest_id}):
                    raise BackupError("image_config_digest_mismatch")
                manifest_member = archive.getmember("blobs/sha256/" + manifest_id.split(":", 1)[1])
                if manifest_member.size > CHUNK:
                    raise BackupError("invalid_oci_manifest")
                payload = archive.extractfile(manifest_member).read()
                if "sha256:" + hashlib.sha256(payload).hexdigest() != manifest_id:
                    raise BackupError("image_config_digest_mismatch")
                oci = json.loads(payload)
                annotations = oci.get("annotations", {})
                if (not isinstance(annotations, dict) or any(key in annotations for key in (
                        "io.containerd.image.name", "org.opencontainers.image.ref.name"))):
                    raise BackupError("tagged_oci_manifest_refused")
                if oci.get("config", {}).get("digest") != actual:
                    raise BackupError("image_config_digest_mismatch")
                for layer in oci.get("layers", []):
                    expected = layer.get("digest", "")
                    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
                        raise BackupError("invalid_oci_layer_digest")
                    stream = archive.extractfile("blobs/sha256/" + expected.split(":", 1)[1])
                    digest = hashlib.sha256()
                    while chunk := stream.read(CHUNK):
                        digest.update(chunk)
                    if "sha256:" + digest.hexdigest() != expected:
                        raise BackupError("oci_layer_digest_mismatch")
            elif actual != image:
                raise BackupError("image_config_digest_mismatch")
            layers = entries[0].get("Layers")
            if not isinstance(layers, list) or not layers:
                raise BackupError("image_layers_missing")
            for name in layers:
                if not archive.getmember(name).isfile():
                    raise BackupError("invalid_image_layer")
        except (KeyError, TypeError, AttributeError) as exc:
            raise BackupError("invalid_docker_image_manifest") from exc


def export_image(image_reference: str, key_file: Path, destination: Path) -> dict:
    if os.name != "posix" or os.geteuid() != 0:
        raise BackupError("linux_root_required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,220}", image_reference):
        raise BackupError("invalid_local_image_reference")
    destination = clean_path(str(destination))
    if destination.exists() or not destination.parent.is_dir():
        raise BackupError("new_image_archive_destination_required")
    if destination.parent.stat().st_mode & 0o077:
        raise BackupError("image_archive_directory_must_be_private")
    key = read_key(key_file)
    image = run(["docker", "image", "inspect", "--format", "{{.Id}}", image_reference])
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise BackupError("immutable_image_id_required")
    with lease(destination.parent / ".image-export.lock"):
        with tempfile.TemporaryDirectory(prefix=".image-export-", dir=destination.parent) as tmp:
            image_tar = Path(tmp) / "runtime-image.tar"
            run(["docker", "image", "save", "--output", str(image_tar), image], timeout=1800)
            os.chmod(image_tar, 0o600)
            validate_docker_image_tar(image_tar, image)
            partial = Path(tmp) / "encrypted.ffimage"
            metadata = {"schema": 1, "kind": "docker-image", "odoo_image_id": image,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "directories": {}, "image_format": "docker-save-tar",
                        "compression": "gzip"}
            encrypt_archive(partial, key, [("runtime-image.tar", image_tar)], metadata, compress=True)
            verify_archive(partial, key)
            os.replace(partial, destination)
            sync_directory(destination.parent)
    result = {"archive": str(destination), "odoo_image_id": image,
              "encrypted_archive_verified": True, "live_runtime_modified": False,
              "size_bytes": destination.stat().st_size}
    private_json(destination.with_suffix(".receipt.json"), result)
    return result


def restore_image(archive: Path, key_file: Path) -> dict:
    if os.name != "posix" or os.geteuid() != 0:
        raise BackupError("linux_root_required")
    key = read_key(key_file)
    with tempfile.TemporaryDirectory(prefix="forgeflow-image-restore-") as temporary:
        root = Path(temporary)
        payload = root / "authenticated.tar"
        decrypt_to(archive, key, payload)
        metadata = verify_plaintext(payload)
        if (metadata.get("kind") != "docker-image"
                or set(metadata["files"]) != {"runtime-image.tar"}):
            raise BackupError("not_a_runtime_image_archive")
        image = metadata.get("odoo_image_id", "")
        image_tar = root / "runtime-image.tar"
        with tarfile.open(payload, "r:*") as source, image_tar.open("xb") as target:
            os.chmod(image_tar, 0o600)
            shutil.copyfileobj(source.extractfile("runtime-image.tar"), target, CHUNK)
        validate_docker_image_tar(image_tar, image)
        run(["docker", "image", "load", "--input", str(image_tar)], timeout=1800)
        actual = run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        if actual != image:
            raise BackupError("loaded_image_id_mismatch")
    return {"image_loaded_and_verified": True, "odoo_image_id": image,
            "live_runtime_modified": False}


def upload_drive(archive: Path, upload: dict) -> str:
    """Optional rclone interface; no auth flow and no silent local-only fallback."""
    if not isinstance(upload, dict):
        raise BackupError("invalid_drive_config")
    remote = upload.get("remote", "")
    folder = upload.get("folder", "")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", remote)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,240}", folder)
            or ".." in PurePosixPath(folder).parts):
        raise BackupError("invalid_drive_target")
    config_path = clean_path(upload.get("config_file"))
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(secure_file(config_path).decode())
    if not parser.has_section(remote) or parser.get(remote, "type", fallback="") != "drive":
        raise BackupError("google_drive_remote_required")
    target = f"{remote}:{folder}/{archive.name}"
    common = ["rclone", "--config", str(config_path)]
    run([*common, "copyto", str(archive), target], timeout=1800)
    listing = json.loads(run([*common, "lsjson", "--stat", "--hash", target]))
    md5 = hashlib.md5(usedforsecurity=False)
    with archive.open("rb") as source:
        while chunk := source.read(CHUNK):
            md5.update(chunk)
    hashes = {k.lower(): v.lower() for k, v in listing.get("Hashes", {}).items()}
    if listing.get("Size") != archive.stat().st_size or hashes.get("md5") != md5.hexdigest():
        raise BackupError("drive_upload_not_verified")
    return "uploaded_and_remote_md5_verified"


def create(config: dict) -> dict:
    if os.name != "posix" or os.geteuid() != 0:
        raise BackupError("linux_root_required")
    config = validate_config(config)
    sys.path.insert(0, str(config["project_root"] / "src"))
    from forgeflow import vps_odoo

    key = read_key(config["encryption_key_file"])
    config["output_root"].mkdir(mode=0o700, parents=True, exist_ok=True)
    if config["output_root"].stat().st_mode & 0o077:
        raise BackupError("backup_output_directory_must_be_private")
    with ExitStack() as stack:
        for path in (config["project_root"] / ".runtime/lifecycle/workbench.lock",
                     config["project_root"] / ".runtime/lifecycle/heavy-operation.lock",
                     config["runtime_root"] / ".provision.lock",
                     config["runtime_root"] / "store/scope.lock",
                     config["odoo_state"] / ".lock"):
            stack.enter_context(lease(path))
        image = assert_writers_stopped(config)
        saved = json.loads(secure_file(config["odoo_state"] / "state.json"))
        binding = vps_odoo.expected_binding(saved["instance_id"], saved["generation"])
        state, _ = vps_odoo.load_state(config["odoo_state"], binding, False)
        runtime_config_path = config["secret_files"].get("runtime.json")
        if runtime_config_path is None:
            raise BackupError("runtime_configuration_required_for_matched_backup")
        runtime_config = json.loads(secure_file(runtime_config_path))
        instance = json.loads(secure_file(config["runtime_root"] / "vps-instance.json"))
        check_generation_binding(config, state, image, runtime_config, instance)
        temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix=".forgeflow-backup-", dir=config["output_root"])))
        dumps = temporary / "database"
        dumps.mkdir(mode=0o700)
        make_dumps(config, image, dumps)
        if image != assert_writers_stopped(config):
            raise BackupError("runtime_changed_during_backup")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = config["output_root"] / f"forgeflow-{timestamp}-{uuid.uuid4().hex[:8]}.ffbackup"
        partial = archive.with_suffix(".partial")
        metadata = {"schema": 1, "created_at": timestamp, "runtime_binding":
                    vps_odoo.database_binding(state), "odoo_image_id": image,
                    "database_format": "postgresql-18-custom-plus-globals",
                    "consistency": "writers-stopped-pg-only-logical-dump",
                    "restore_status": "archive_verified_database_restore_not_yet_tested"}
        try:
            files, directories = gather_files(config, dumps)
            metadata["directories"] = directories
            encrypt_archive(partial, key, files, metadata)
            verify_archive(partial, key)
            assert_writers_stopped(config)
            os.replace(partial, archive)
            sync_directory(archive.parent)
        finally:
            partial.unlink(missing_ok=True)
    upload = config.get("google_drive")
    result = {"archive": str(archive), "verified": True,
              "google_drive": "pending" if upload else "not_configured_local_only",
              "services": "left_stopped", "database_restore": "not_tested_by_this_command"}
    receipt = archive.with_suffix(".receipt.json")
    private_json(receipt, result)
    if upload:
        try:
            result["google_drive"] = upload_drive(archive, upload)
        except (BackupError, OSError, ValueError, subprocess.TimeoutExpired):
            result["google_drive"] = "failed_local_archive_retained"
            private_json(receipt, result)
            raise
        private_json(receipt, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create_parser = commands.add_parser("create")
    create_parser.add_argument("--config", type=Path, required=True)
    for name in ("verify", "restore-staging"):
        child = commands.add_parser(name)
        child.add_argument("--archive", type=Path, required=True)
        child.add_argument("--key-file", type=Path, required=True)
        if name == "restore-staging":
            child.add_argument("--destination", type=Path, required=True)
    restore_parser = commands.add_parser("restore-db-check")
    restore_parser.add_argument("--staging", type=Path, required=True)
    restore_parser.add_argument("--destination", type=Path, required=True)
    app_parser = commands.add_parser("restore-app-check")
    app_parser.add_argument("--staging", type=Path, required=True)
    app_parser.add_argument("--database-check", type=Path, required=True)
    export_parser = commands.add_parser("export-image")
    export_parser.add_argument("--image", required=True)
    export_parser.add_argument("--key-file", type=Path, required=True)
    export_parser.add_argument("--destination", type=Path, required=True)
    image_parser = commands.add_parser("restore-image")
    image_parser.add_argument("--archive", type=Path, required=True)
    image_parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create(json.loads(secure_file(args.config)))
        elif args.command == "restore-db-check":
            result = restore_database_check(args.staging, args.destination)
        elif args.command == "restore-app-check":
            result = restore_application_check(args.staging, args.database_check)
        elif args.command == "export-image":
            result = export_image(args.image, args.key_file, args.destination)
        elif args.command == "restore-image":
            result = restore_image(args.archive, args.key_file)
        else:
            result = verify_archive(args.archive, read_key(args.key_file),
                                    getattr(args, "destination", None))
            if result.get("kind") == "docker-image":
                result = {"verified": True, "kind": "docker-image",
                          "odoo_image_id": result["odoo_image_id"]}
            else:
                result = {"verified": True, "instance": result["runtime_binding"]["instance_id"],
                          "generation": result["runtime_binding"]["generation"],
                          "database_restore": "not_tested_by_this_command"}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (BackupError, OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired,
            tarfile.TarError, configparser.Error) as exc:
        reason = str(exc) if isinstance(exc, BackupError) else type(exc).__name__
        print("ForgeFlow backup failed: " + reason, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
