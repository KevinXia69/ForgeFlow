"""Private execution copies of reviewed source, never links back to the workspace.

The host Python copy is bound by a separate runtime index. A source-only index
does not itself establish dependency isolation or an OS security boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tarfile
from pathlib import Path, PurePosixPath

from forgeflow.planning.bundle import UPSTREAM_FILES

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_COPY_BYTES = 512 * 1024 * 1024
MAX_FILES = 30000


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not isinstance(value, str) or not value or path.is_absolute()
        or path.as_posix() != value or any(p in {".", "..", ".git"} for p in path.parts)
        or "\\" in value or "\x00" in value
    ):
        raise ValueError("Invalid frozen input path.")
    return value


def _regular(root: Path, name: str) -> Path:
    path = root / _relative(name)
    for parent in (root, *path.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise ValueError("Frozen inputs must not traverse symlinks.")
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Frozen inputs must be regular files.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Frozen input exceeds size limit.")
    return path


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy_dependency_inputs(root: Path, agent: str, planner_profile: str):
    """Yield only pinned Pi and generic ERP sources, never tasks or solutions."""
    if agent != "pi":
        return
    lock = json.loads(_regular(root, "upstream.lock.json").read_bytes())["sources"]
    selections = {"erp-bench": {"agents/pi.py"}, "pi-mono": None}
    if planner_profile != "none":
        selections["erp-bench"].update(UPSTREAM_FILES)
    for source, selected in selections.items():
        provenance_name = f"vendor/{source}.source.json"
        provenance_bytes = _regular(root, provenance_name).read_bytes()
        provenance = json.loads(provenance_bytes)
        pinned = lock[source]
        files = provenance["files"]
        if not isinstance(files, dict) or not 0 < len(files) <= MAX_FILES:
            raise ValueError("Invalid pinned dependency manifest.")
        tree = _digest(json.dumps(files, sort_keys=True, separators=(",", ":")).encode())
        if provenance.get("commit") != pinned["commit"] or tree != pinned["tree_sha256"]:
            raise ValueError("Pinned dependency provenance does not match the lock.")
        yield provenance_name, provenance_bytes, 0o400
        for name in sorted(files if selected is None else selected):
            _relative(name)
            record = files[name]
            relative = f"vendor/{source}/{name}"
            path = _regular(root, relative)
            data = path.read_bytes()
            if _digest(data) != record["sha256"] or len(data) != record["size"]:
                raise ValueError("Pinned dependency changed before execution staging.")
            yield relative, data, 0o500 if record["executable"] else 0o400


def materialize_execution(
    root: Path, run_dir: Path, snapshot: dict, *, agent: str,
    planner_profile: str, secrets: tuple[str, ...] = (),
) -> dict:
    """Build a measured copy, failing closed on drift, extra members or links."""
    if snapshot.get("workspace_matches_at_capture") is not True:
        raise ValueError("Workspace changed during source capture; do not launch.")
    archive_path = _regular(run_dir, snapshot["path"])
    index_path = _regular(run_dir, snapshot["index_path"])
    index_bytes = index_path.read_bytes()
    if (
        _digest(archive_path.read_bytes()) != snapshot["archive_sha256"]
        or _digest(index_bytes) != snapshot["index_sha256"]
    ):
        raise ValueError("Source capture integrity mismatch.")
    index = json.loads(index_bytes)
    expected = index["files"]
    destination = run_dir / "implementation/execution"
    destination.mkdir(mode=0o700, exist_ok=False)
    files: dict[str, dict] = {}
    total_bytes = 0

    def write(name: str, data: bytes, mode: int) -> None:
        nonlocal total_bytes
        _relative(name)
        if name in files or len(files) >= MAX_FILES:
            raise ValueError("Duplicate or excessive frozen inputs.")
        total_bytes += len(data)
        if len(data) > MAX_FILE_BYTES or total_bytes > MAX_COPY_BYTES:
            raise ValueError("Frozen inputs exceed size limits.")
        if any(secret and secret.encode() in data for secret in secrets):
            raise ValueError("Frozen inputs contain a configured credential.")
        path = destination / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
        path.chmod(mode)
        files[name] = {"sha256": _digest(data), "size_bytes": len(data), "mode": mode}

    with tarfile.open(archive_path, "r:gz") as archive:
        seen = set()
        for member in archive:
            name = _relative(member.name)
            if not member.isfile() or name not in expected or name in seen:
                raise ValueError("Unexpected source capture member.")
            if member.size != expected[name]["size_bytes"] or member.size > MAX_FILE_BYTES:
                raise ValueError("Source capture member size mismatch.")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("Unreadable source capture member.")
            data = stream.read()
            if _digest(data) != expected[name]["sha256"]:
                raise ValueError("Source capture member digest mismatch.")
            write(name, data, 0o500 if member.mode & 0o111 else 0o400)
            seen.add(name)
        if seen != set(expected):
            raise ValueError("Source capture is incomplete.")
    for name, data, mode in _copy_dependency_inputs(root, agent, planner_profile):
        write(name, data, mode)
    for directory, _, _ in os.walk(destination, topdown=False):
        Path(directory).chmod(0o500)
    record = {
        "schema_version": 1,
        "root": "implementation/execution",
        "source_archive_sha256": snapshot["archive_sha256"],
        "execution_uses_snapshot": True,
        "dependency_policy": "Pinned Pi and generic ERP sources only; no task solutions or verifiers.",
        "host_dependencies_frozen": False,
        "host_dependency_boundary": "Source-only index; the runner binds a separately verified host runtime.",
        "files": files,
    }
    record_path = run_dir / "implementation/execution-index.json"
    record_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    record_path.chmod(0o400)
    return {
        "root": record["root"],
        "index_path": "implementation/execution-index.json",
        "index_sha256": _digest(record_path.read_bytes()),
        "file_count": len(files),
        "execution_uses_snapshot": True,
        "host_dependencies_frozen": False,
    }


def verify_execution(run_dir: Path, record: dict) -> dict:
    index_path = _regular(run_dir, record["index_path"])
    data = index_path.read_bytes()
    if _digest(data) != record["index_sha256"]:
        raise ValueError("Execution index changed.")
    index = json.loads(data)
    root = run_dir / _relative(record["root"])
    actual = set()
    for directory, children, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        if parent.is_symlink() or parent.stat().st_mode & 0o222:
            raise ValueError("Execution directory is not read-only.")
        if any((parent / name).is_symlink() for name in children):
            raise ValueError("Execution directory contains a symlink.")
        for name in names:
            relative = (parent / name).relative_to(root).as_posix()
            path = _regular(root, relative)
            expected = index["files"].get(relative)
            if (
                expected is None or path.stat().st_mode & 0o777 != expected["mode"]
                or _digest(path.read_bytes()) != expected["sha256"]
            ):
                raise ValueError("Frozen execution files changed.")
            actual.add(relative)
    if actual != set(index["files"]):
        raise ValueError("Frozen execution files are missing.")
    return {"ok": True, "file_count": len(actual), "index_sha256": record["index_sha256"]}


IMPORT_PROBE = """
import importlib.metadata, importlib.util, json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
modules = {}
for name in sys.argv[2:]:
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        raise RuntimeError('Required execution module is unavailable')
    origin = pathlib.Path(spec.origin).resolve()
    if not origin.is_relative_to(root / 'src'):
        raise RuntimeError('Execution import escaped frozen source')
    modules[name] = origin.relative_to(root).as_posix()
print(json.dumps({'python': sys.executable, 'modules': modules,
    'host_distributions': sorted((d.metadata['Name'], d.version)
        for d in importlib.metadata.distributions() if d.metadata['Name'])}))
"""
