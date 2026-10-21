#!/usr/bin/env python3
"""Fetch pinned source snapshots without creating nested Git repositories."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024


class SourceError(RuntimeError):
    """The source cannot be fetched or verified safely."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Finder/Explorer metadata is not part of any pinned upstream tree. Ignoring
# it keeps verification reproducible when the checkout lives inside a synced
# or indexed folder that writes these files.
IGNORED_ENTRY_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def file_manifest(root: Path) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise SourceError(f"Snapshot must be a regular directory: {root}")
    manifest = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.name in IGNORED_ENTRY_NAMES or relative.name.startswith("._"):
            continue
        if path.is_symlink() or ".git" in relative.parts:
            raise SourceError(f"Forbidden snapshot entry: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SourceError(f"Non-regular snapshot entry: {relative}")
        stat = path.stat()
        manifest[relative.as_posix()] = {
            "sha256": file_sha256(path),
            "size": stat.st_size,
            "executable": bool(stat.st_mode & 0o111),
        }
    return manifest


def tree_sha256(manifest: dict) -> str:
    serialized = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()


def load_lock(path: Path) -> dict:
    data = json.loads(path.read_text())
    if data.get("schema_version") != 1 or not isinstance(data.get("sources"), dict):
        raise SourceError("Unsupported source lock format")
    for name, source in data["sources"].items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
            raise SourceError(f"Invalid source name: {name}")
        repository = source.get("repository", "")
        if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", repository):
            raise SourceError(f"Invalid repository URL for {name}")
        if not re.fullmatch(r"[0-9a-f]{40}", source.get("commit", "")):
            raise SourceError(f"Invalid pinned commit for {name}")
        for key in ("archive_sha256", "tree_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", source.get(key, "")):
                raise SourceError(f"Invalid {key} for {name}")
        expected_url = (
            repository.replace("https://github.com/", "https://codeload.github.com/")
            + "/tar.gz/"
            + source["commit"]
        )
        if source.get("source_url") != expected_url:
            raise SourceError(f"Source URL does not match pinned commit for {name}")
    return data["sources"]


def download(source: dict, destination: Path) -> None:
    request = urllib.request.Request(
        source["source_url"], headers={"User-Agent": "ForgeFlow-source-fetch/0.1"}
    )
    size = 0
    with urllib.request.urlopen(request, timeout=120) as response:
        with destination.open("xb") as handle:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise SourceError("Source archive exceeds size limit")
                handle.write(chunk)


def safe_extract(archive: Path, destination: Path, expected_root: str) -> Path:
    """Reject links and special files; extract only one expected top-level tree."""
    destination.mkdir()
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        seen = set()
        total = 0
        for member in members:
            raw_parts = member.name.rstrip("/").split("/")
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or not raw_parts
                or raw_parts[0] != expected_root
                or any(part in ("", ".", "..", ".git") for part in raw_parts)
                or "\\" in member.name
                or not (member.isdir() or member.isfile())
                or path in seen
            ):
                raise SourceError(f"Unsafe archive member: {member.name}")
            if len(raw_parts) == 1 and not member.isdir():
                raise SourceError("Archive root must be a directory")
            seen.add(path)
            total += member.size
            if total > MAX_EXTRACTED_BYTES:
                raise SourceError("Extracted source exceeds size limit")
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source_file = bundle.extractfile(member)
            if source_file is None:
                raise SourceError(f"Unable to read archive member: {member.name}")
            with source_file, target.open("xb") as handle:
                shutil.copyfileobj(source_file, handle)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    root = destination / expected_root
    if not root.is_dir():
        raise SourceError("Source archive is empty")
    return root


def check_provenance(metadata: dict, source: dict) -> None:
    for key in ("repository", "commit", "source_url", "archive_sha256"):
        if metadata.get(key) != source[key]:
            raise SourceError(f"Existing snapshot provenance differs: {key}")


def check_tree(manifest: dict, source: dict) -> None:
    if tree_sha256(manifest) != source["tree_sha256"]:
        raise SourceError("Snapshot file contents or executable permissions differ from lock")


def fetch_source(root: Path, name: str, source: dict, verify_only: bool = False) -> str:
    vendor = root / "vendor"
    destination = vendor / name
    metadata_path = vendor / f"{name}.source.json"
    if vendor.is_symlink() or destination.is_symlink() or metadata_path.is_symlink():
        raise SourceError("Snapshot paths must not be symbolic links")
    metadata = {}
    existing = destination.exists()
    if existing:
        if not metadata_path.is_file():
            raise SourceError(f"Refusing existing snapshot without provenance: {name}")
        metadata = json.loads(metadata_path.read_text())
        check_provenance(metadata, source)
        actual = file_manifest(destination)
        check_tree(actual, source)
        if "files" in metadata:
            if metadata["files"] != actual:
                raise SourceError(f"Snapshot file manifest differs: {name}")
            if metadata.get("tree_sha256") != source["tree_sha256"]:
                raise SourceError(f"Snapshot manifest digest differs: {name}")
            return "verified" if verify_only else "reused"
        if verify_only:
            raise SourceError(f"Snapshot has no file manifest; run fetch first: {name}")
    elif metadata_path.exists():
        raise SourceError(f"Provenance exists without its snapshot: {name}")
    elif verify_only:
        raise SourceError(f"Snapshot has not been fetched: {name}")

    vendor.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fetch-", dir=vendor) as temp:
        staging = Path(temp)
        archive = staging / "source.tar.gz"
        download(source, archive)
        if file_sha256(archive) != source["archive_sha256"]:
            raise SourceError(f"Archive SHA-256 mismatch: {name}")
        expected_root = source["repository"].rsplit("/", 1)[-1] + "-" + source["commit"]
        extracted = safe_extract(archive, staging / "extracted", expected_root)
        manifest = file_manifest(extracted)
        check_tree(manifest, source)
        if existing:
            if file_manifest(destination) != manifest:
                raise SourceError(f"Existing snapshot differs from pinned archive: {name}")
        else:
            if destination.exists() or destination.is_symlink():
                raise SourceError(f"Snapshot destination appeared during fetch: {name}")
            shutil.move(str(extracted), str(destination))
        metadata.update(
            repository=source["repository"],
            commit=source["commit"],
            source_url=source["source_url"],
            archive_sha256=source["archive_sha256"],
            archive_bytes=archive.stat().st_size,
            tree_sha256=source["tree_sha256"],
            files=manifest,
            verified_at_utc=datetime.now(timezone.utc).isoformat(),
            extraction="Pinned tarball; no links, special files, path traversal or .git entries",
            upstream_files_modified=False,
        )
        metadata.setdefault("fetched_at_utc", datetime.now(timezone.utc).isoformat())
        temporary_metadata = staging / "source.json"
        temporary_metadata.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        temporary_metadata.replace(metadata_path)
    return "manifest-added" if existing else "fetched"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", help="Source names; defaults to all locked sources")
    parser.add_argument("--verify-only", action="store_true", help="Verify without network access")
    args = parser.parse_args()
    try:
        sources = load_lock(ROOT / "upstream.lock.json")
        for name in args.sources or sources:
            if name not in sources:
                raise SourceError(f"Unknown source: {name}")
            result = fetch_source(ROOT, name, sources[name], args.verify_only)
            print(f"{name}: {result} ({sources[name]['commit']})")
    except (SourceError, OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"Source fetch failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
