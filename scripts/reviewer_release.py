"""File/version binding for one operator-run QA acceptance; no remote attestation."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path


def command(*args: str, cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("release_probe_failed")
    return result.stdout.strip()


def file_hash(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("release_file_missing_or_symlink")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(configs: dict, revision: str, *, driver: Path) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("full_release_revision_required")
    if set(configs) != {"qa1", "qa2"}:
        raise ValueError("only_disposable_qa_instances_allowed")
    repo = Path(configs["qa1"]["repo_root"]).resolve()
    if driver.resolve().parent.parent != repo:
        raise RuntimeError("acceptance_driver_outside_release")
    if any(Path(config["repo_root"]).resolve() != repo for config in configs.values()):
        raise RuntimeError("qa_release_roots_differ")
    if command("git", "rev-parse", "HEAD", cwd=repo) != revision:
        raise RuntimeError("release_revision_mismatch")
    if command("git", "status", "--porcelain", "--untracked-files=normal", cwd=repo):
        raise RuntimeError("release_worktree_not_clean")
    tracked = command("git", "ls-files", "-z", cwd=repo).split("\0")
    sources = {name: file_hash(repo / name) for name in tracked if name}
    frontend = repo / "src/forgeflow/api/static/app"
    if not (frontend / "index.html").is_file() or frontend.is_symlink():
        raise RuntimeError("release_frontend_not_built")
    assets = {str(path.relative_to(frontend)): file_hash(path)
              for path in sorted(frontend.rglob("*")) if path.is_file() or path.is_symlink()}
    paths = {}
    for name, config in configs.items():
        # -I avoids this tool's PYTHONPATH masking an old installed package.
        package = Path(command(config["python"], "-I", "-c",
                               "import forgeflow; print(forgeflow.__file__)", cwd=repo)).resolve()
        if package != (repo / "src/forgeflow/__init__.py").resolve():
            raise RuntimeError("installed_backend_outside_release")
        paths[name] = {"repo_root": str(repo), "python": config["python"],
                       "package_path": str(package), "target": {
                           key: config.get(key) for key in (
                               "instance_name", "api_port", "container", "sandbox_root", "case_root",
                               "data_root", "socket", "service_unit", "api_user", "dsh_user")}}
    return {"schema": 1, "git_revision": revision, "tracked_worktree_clean": True,
            "source_sha256": sources, "frontend_sha256": assets, "runtime_paths": paths,
            "binding_scope": "configured_checkout_package_and_frontend_files_not_process_attestation"}


def check(expected: dict, actual: dict) -> None:
    if expected != actual:
        raise RuntimeError("acceptance_release_changed")
