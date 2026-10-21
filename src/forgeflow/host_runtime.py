"""Measured host Python + wheel copies, isolated from the development venv.

This is reproducibility isolation, not an OS security boundary. Native system
libraries and the Docker client remain host capabilities and are reported as such.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

from forgeflow.runtime_lifecycle import exclusive_lease, read_record, write_record

SCHEMA = 1
MAX_BYTES = 3 * 1024**3
MAX_FILES = 100000
HARBOR_BOOTSTRAP = """
import sys
sys.path[:0] = [sys.argv.pop(1), sys.argv.pop(1)]
sys.argv[0] = 'harbor'
from harbor.cli.main import app
app()
"""
PATH_BOOTSTRAP = "import sys; sys.path[:0] = [sys.argv.pop(1), sys.argv.pop(1)];\n"
PYTHON_PROBE = """
import json, pathlib, sys
print(json.dumps({'base': str(pathlib.Path(sys.base_prefix).resolve()),
    'binary': str(pathlib.Path(sys.executable).resolve()),
    'version': sys.version, 'minor': f'{sys.version_info.major}.{sys.version_info.minor}'}))
"""
DEPENDENCY_PROBE = """
import importlib, importlib.metadata, json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
if pathlib.Path(sys.base_prefix).resolve() != root / 'python':
    raise RuntimeError('Python escaped the frozen runtime')
sys.path.insert(0, str(root / 'packages'))
modules = {}
for name in ('harbor.cli.main', 'httpx', 'dotenv', 'yaml', 'cryptography'):
    module = importlib.import_module(name)
    origin = pathlib.Path(module.__file__).resolve()
    if not origin.is_relative_to(root / 'packages'):
        raise RuntimeError('Dependency escaped the frozen runtime')
    modules[name] = origin.relative_to(root).as_posix()
print(json.dumps({'modules': modules, 'distributions': sorted(
    (d.metadata['Name'], d.version) for d in importlib.metadata.distributions(
        path=[str(root / 'packages')]) if d.metadata['Name'])}))
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _inventory(root: Path, *, sealed: bool = False, skip_bytecode: bool = False,
               exclude: frozenset[str] = frozenset()) -> dict:
    """Hash actual bytes; internal links are bounded, no cache hardlinks accepted."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Unsafe host runtime root.")
    root = root.resolve()
    records, total = {}, 0
    for current, directories, names in os.walk(root, followlinks=False):
        parent = Path(current)
        directories[:] = [n for n in directories if (parent / n).relative_to(root).as_posix() not in exclude]
        names = [n for n in names if (parent / n).relative_to(root).as_posix() not in exclude]
        if sealed and parent.stat().st_mode & 0o222:
            raise ValueError("Host runtime directory is writable.")
        if skip_bytecode:
            directories[:] = [n for n in directories if n != "__pycache__"]
            names = [n for n in names if not n.endswith((".pyc", ".pyo"))]
        for name in sorted([*directories, *names]):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve(strict=True)
                if not target.is_relative_to(root):
                    raise ValueError("Host runtime link escapes its copy.")
                records[relative] = {"link": target.relative_to(root).as_posix()}
            elif stat.S_ISDIR(info.st_mode):
                continue
            elif stat.S_ISREG(info.st_mode):
                if sealed and (info.st_mode & 0o222 or info.st_nlink != 1):
                    raise ValueError("Host runtime file is mutable or shared.")
                total += info.st_size
                if total > MAX_BYTES or info.st_size > 512 * 1024**2:
                    raise ValueError("Host runtime exceeds size limit.")
                with path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                records[relative] = {"sha256": digest, "size": info.st_size,
                                     "executable": bool(info.st_mode & 0o111)}
            else:
                raise ValueError("Host runtime input is not a regular file.")
            if len(records) > MAX_FILES:
                raise ValueError("Host runtime exceeds file limit.")
    return records


def _copy_python(source: Path, destination: Path, inventory: dict) -> None:
    destination.mkdir(mode=0o700)
    for name, expected in inventory.items():
        target = destination / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if "link" in expected:
            target.symlink_to(os.path.relpath(destination / expected["link"], target.parent))
        else:
            data = (source / name).read_bytes()
            if _sha(data) != expected["sha256"] or len(data) != expected["size"]:
                raise ValueError("Python distribution changed during capture.")
            with target.open("xb") as handle:
                handle.write(data)
            target.chmod(0o700 if expected["executable"] else 0o600)


def _build_environment() -> dict[str, str]:
    # Offline build subprocesses do not receive provider credentials or uv overrides.
    env = {k: os.environ[k] for k in ("HOME", "PATH", "TMPDIR", "LANG") if k in os.environ}
    return {**env, "PYTHONDONTWRITEBYTECODE": "1", "UV_NO_CONFIG": "1",
            "UV_PYTHON_DOWNLOADS": "never"}


def _run(command: list[str], *, cwd: Path, timeout: int = 180) -> str:
    result = subprocess.run(command, cwd=cwd, env=_build_environment(),
                            capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        # Do not echo arbitrary installer output into a run manifest.
        raise RuntimeError("Offline frozen host runtime preparation failed.")
    return result.stdout


def verify_host_runtime(record: dict) -> dict:
    root = Path(record["root"])
    data = (root.parent / f"{root.name}.json").read_bytes()
    if _sha(data) != record["index_sha256"]:
        raise ValueError("Host runtime index changed.")
    index = json.loads(data)
    if (index.get("schema_version") != SCHEMA or index.get("cache_key") != record.get("cache_key")
            or record["packages"] != str(root / "packages")
            or record["python"] != str(root / index["python"])):
        raise ValueError("Host runtime entrypoint binding changed.")
    actual = _inventory(root, sealed=True)
    if index["files"] != actual or _sha(_canonical(actual)) != record["tree_sha256"]:
        raise ValueError("Frozen host runtime files changed.")
    return {"ok": True, "tree_sha256": record["tree_sha256"], "file_count": len(actual)}


def ensure_host_runtime(source: Path, cache: Path, host_python: Path) -> dict:
    """Use only hashed locked wheels in an independent copy, never editable installs.

    A failed build is retained under its unique name. No complete-cache pointer
    is written until successful sealing; subsequent attempts use a fresh folder.
    """
    for parent in (cache, *cache.parents):
        if parent.is_symlink():
            raise ValueError("Unsafe host runtime cache path.")
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to prepare the frozen host runtime.")
    probe = json.loads(_run([str(host_python), "-I", "-B", "-S", "-c", PYTHON_PROBE], cwd=source))
    base = Path(probe["base"])
    # System/Homebrew Python distributions are not portable by copying a prefix.
    # Require a bounded standalone distribution; validate relocation below.
    binary = Path(probe["binary"])
    if not binary.is_relative_to(base) or not (base / "lib" / f"python{probe['minor']}").is_dir():
        raise ValueError("A relocatable standalone Python distribution is required.")
    inputs = {name: (source / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    # Base installers or user-added packages are not runtime inputs. Every third-
    # party dependency comes from the hashed lock into the separate packages tree.
    original = _inventory(base, skip_bytecode=True,
                          exclude=frozenset({f"lib/python{probe['minor']}/site-packages"}))
    key = _sha(_canonical({"schema": SCHEMA, "python": original,
                           "inputs": {k: _sha(v) for k, v in inputs.items()}}))
    with exclusive_lease(cache / "build.lock"):
        saved = read_record(cache / f"{key}.json")
        if saved:
            if Path(saved["root"]).parent != cache or saved.get("cache_key") != key:
                raise ValueError("Host runtime cache binding mismatch.")
            verify_host_runtime(saved)
            return saved
        root = cache / f"{key[:16]}-{uuid.uuid4().hex[:12]}"
        root.mkdir(mode=0o700)
        project = root / "inputs"
        project.mkdir(mode=0o700)
        for name, content in inputs.items():
            (project / name).write_bytes(content)
        _copy_python(base, root / "python", original)
        python = root / "python" / binary.relative_to(base)
        relocated = json.loads(_run([str(python), "-I", "-B", "-S", "-c", PYTHON_PROBE], cwd=root))
        if Path(relocated["base"]) != root / "python":
            raise ValueError("Python did not relocate into the frozen runtime.")
        requirements = _run([uv, "export", "--locked", "--offline", "--no-dev",
                             "--no-default-groups", "--no-emit-project", "--no-annotate",
                             "--no-header", "--python", str(python)], cwd=project)
        # Local/editable/direct-URL requirements are not reproducible locked wheels.
        if any(token in requirements for token in (" @ ", "://", "-e ", "file:", "--index")):
            raise ValueError("Frozen host dependencies must be hashed registry wheels.")
        requirements_path = root / "requirements.txt"
        requirements_path.write_text(requirements)
        _run([uv, "pip", "sync", str(requirements_path), "--target", str(root / "packages"),
              "--python", str(python), "--offline", "--require-hashes", "--only-binary", ":all:",
              "--link-mode", "copy", "--strict"], cwd=project, timeout=300)
        dependency_probe = json.loads(_run(
            [str(python), "-I", "-B", "-S", "-c", DEPENDENCY_PROBE, str(root)], cwd=root,
        ))
        for current, directories, names in os.walk(root, topdown=False, followlinks=False):
            for name in names:
                path = Path(current) / name
                if not path.is_symlink():
                    path.chmod(0o500 if path.stat().st_mode & 0o111 else 0o400)
            Path(current).chmod(0o500)
        files = _inventory(root, sealed=True)
        index_path = cache / f"{root.name}.json"
        write_record(index_path, {"schema_version": SCHEMA, "cache_key": key,
                                  "python": python.relative_to(root).as_posix(), "files": files})
        record = {"schema_version": SCHEMA, "root": str(root), "python": str(python),
                  "packages": str(root / "packages"), "cache_key": key,
                  "tree_sha256": _sha(_canonical(files)),
                  "index_sha256": _sha(index_path.read_bytes()),
                  "lock_sha256": _sha(inputs["uv.lock"]),
                  "requirements_sha256": _sha(requirements.encode()),
                  "host_dependencies_frozen": True,
                  "import_probe": dependency_probe,
                  "boundary": "Python and locked wheels copied; OS libraries and Docker remain host dependencies."}
        write_record(cache / f"{key}.json", record)
        return record


def harbor_command(record: dict, source: Path) -> list[str]:
    return [record["python"], "-I", "-B", "-S", "-c", HARBOR_BOOTSTRAP,
            str(source / "src"), record["packages"]]
