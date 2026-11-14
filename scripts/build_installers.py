#!/usr/bin/env python3
"""Build auditable download bundles from a committed public source tree.

No keys, local volumes, Git metadata or untracked files are copied. Every bundle
contains source for an authentication-free Docker build fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {
    "Windows": ("Start-ForgeFlow.cmd", "Start-ForgeFlow.ps1", "Stop-ForgeFlow.cmd", "Stop-ForgeFlow.ps1"),
    "macOS": ("Start-ForgeFlow.command", "start.sh", "stop.sh"),
    "Linux": ("start.sh", "stop.sh"),
}


def _safe_relative(name: str) -> PurePosixPath:
    raw = name.removesuffix("/")
    path = PurePosixPath(raw)
    if (not raw or "\\" in raw or ":" in raw or path.is_absolute()
            or any(part in ("", ".", "..") for part in raw.split("/"))):
        raise ValueError("unsafe_archive_path")
    return path


def committed_source(root: Path, destination: Path, *, modes: dict[str, int] | None = None) -> str:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    content = subprocess.check_output(["git", "archive", "--format=tar", commit], cwd=root)
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(content)) as archive:
        for member in archive.getmembers():
            relative = _safe_relative(member.name)
            if relative in seen or not (member.isdir() or member.isfile()):
                raise ValueError("source_archive_contains_unsafe_entry")
            seen.add(relative)
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("source_archive_contains_unsafe_entry")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as stream:
                target.write_bytes(stream.read())
            mode = 0o755 if member.mode & 0o111 else 0o644
            target.chmod(mode)
            if modes is not None:
                modes[relative.as_posix()] = mode
    return commit


def image_reference(value: str) -> str:
    if not re.fullmatch(r"ghcr\.io/kevinxia69/forgeflow-workbench(?:-odoo)?@sha256:[a-f0-9]{64}", value):
        raise argparse.ArgumentTypeError("a published immutable workbench image digest is required")
    return value


def verify_bundle(path: Path, *, expected_sha256: str | None = None) -> dict:
    """Verify content and paths without extracting or executing archive entries.

    Supply a checksum obtained from a trusted release to authenticate the whole
    file. Internal hashes alone detect damage, not an attacker replacing both
    source files and the manifest.
    """
    if expected_sha256 and hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("bundle_checksum_mismatch")
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if sum(entry.file_size for entry in entries) > 128 * 1024 * 1024:
            raise ValueError("bundle_size_exceeded")
        files = {}
        seen = set()
        roots = set()
        for entry in entries:
            relative = _safe_relative(entry.filename)
            mode = entry.external_attr >> 16
            if (relative in seen or stat.S_ISLNK(mode) or entry.flag_bits & 1
                    or entry.file_size > 32 * 1024 * 1024):
                raise ValueError("unsafe_bundle_entry")
            seen.add(relative)
            roots.add(relative.parts[0])
            if not entry.is_dir():
                if len(relative.parts) < 2:
                    raise ValueError("bundle_root_missing")
                files[PurePosixPath(*relative.parts[1:]).as_posix()] = entry
        if len(roots) != 1 or "release.json" not in files:
            raise ValueError("bundle_manifest_missing")
        manifest = json.loads(archive.read(files["release.json"]))
        if (not isinstance(manifest, dict)
                or manifest.get("schema_version") != 1 or manifest.get("platform") not in PLATFORMS
                or not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-z0-9.]+)?", manifest.get("version", ""))
                or not re.fullmatch(r"[a-f0-9]{40}", manifest.get("commit", ""))
                or manifest.get("api_keys_included") is not False
                or manifest.get("source_fallback") is not True):
            raise ValueError("bundle_manifest_invalid")
        expected_root = f"ForgeFlow-v{manifest['version']}-{manifest['platform']}"
        if roots != {expected_root}:
            raise ValueError("bundle_root_mismatch")
        wanted = manifest.get("files")
        if not isinstance(wanted, dict) or set(wanted) != set(files) - {"release.json"}:
            raise ValueError("bundle_file_manifest_mismatch")
        for name, digest in wanted.items():
            _safe_relative(name)
            if not isinstance(digest, str) or hashlib.sha256(archive.read(files[name])).hexdigest() != digest:
                raise ValueError("bundle_file_checksum_mismatch")
        for launcher in PLATFORMS[manifest["platform"]]:
            if launcher not in files:
                raise ValueError("bundle_launcher_missing")
        return manifest


def build(root: Path, output: Path, version: str, *, app_image: str | None = None,
          odoo_image: str | None = None) -> dict:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-z0-9.]+)?", version):
        raise ValueError("invalid_release_version")
    if bool(app_image) != bool(odoo_image):
        raise ValueError("both_image_digests_are_required")
    if app_image:
        image_reference(app_image)
        image_reference(odoo_image)
        if "/forgeflow-workbench@" not in app_image or "/forgeflow-workbench-odoo@" not in odoo_image:
            raise ValueError("image_role_mismatch")
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="forgeflow-installer-") as temporary:
        source = Path(temporary) / "source"
        source_modes = {}
        commit = committed_source(root, source, modes=source_modes)
        if tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"] != version:
            raise ValueError("release_version_mismatch")
        subprocess.run([sys.executable, str(source / "scripts/check_distribution.py"), str(source)], check=True)
        base = yaml.safe_load((source / "deploy/workbench/compose.yaml").read_text())
        if app_image:
            for name in ("setup", "api", "supervisor", "docmind", "bootstrap"):
                base["services"][name]["image"] = app_image
            for name in ("odoo", "odoo-init"):
                base["services"][name]["image"] = odoo_image
        fallback = yaml.safe_load((source / "deploy/workbench/compose.source.yaml").read_text())
        for service in fallback["services"].values():
            service["build"]["context"] = "./source"
        for platform, launchers in PLATFORMS.items():
            folder_name = f"ForgeFlow-v{version}-{platform}"
            bundle = Path(temporary) / folder_name
            bundle.mkdir()
            shutil.copytree(source, bundle / "source")
            for launcher in launchers:
                shutil.copy2(source / "installer" / launcher, bundle / launcher)
            for name in ("README.md", "README.zh-CN.md", "LICENSE", "NOTICE"):
                shutil.copy2(source / name, bundle / name)
            shutil.copytree(source / "docs", bundle / "docs")
            (bundle / "compose.yaml").write_text(yaml.safe_dump(base, sort_keys=False))
            (bundle / "compose.source.yaml").write_text(yaml.safe_dump(fallback, sort_keys=False))
            metadata = {"schema_version": 1, "version": version, "platform": platform,
                        "commit": commit, "api_keys_included": False,
                        "app_image": app_image, "odoo_image": odoo_image,
                        "source_fallback": True,
                        "files": {path.relative_to(bundle).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in sorted(bundle.rglob("*")) if path.is_file()}}
            (bundle / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")
            target = output / (folder_name + ".zip")
            if target.exists():
                raise ValueError("release_asset_already_exists")
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for path in sorted(bundle.rglob("*")):
                    if path.is_file():
                        relative = path.relative_to(bundle).as_posix()
                        original = (relative.removeprefix("source/") if relative.startswith("source/")
                                    else f"installer/{relative}" if relative in launchers else relative)
                        mode = source_modes.get(original, 0o644)
                        info = zipfile.ZipInfo(path.relative_to(bundle.parent).as_posix())
                        info.date_time = (2020, 1, 1, 0, 0, 0)
                        info.create_system = 3
                        info.external_attr = (stat.S_IFREG | mode) << 16
                        archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
            results.append({"file": target.name, "bytes": target.stat().st_size,
                            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
            verify_bundle(target, expected_sha256=results[-1]["sha256"])
    (output / "SHA256SUMS").write_text("".join(f"{r['sha256']}  {r['file']}\n" for r in results))
    result = {"version": version, "commit": commit, "assets": results,
              "app_image": app_image, "odoo_image": odoo_image}
    (output / "release-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/installers")
    parser.add_argument("--app-image", type=image_reference)
    parser.add_argument("--odoo-image", type=image_reference)
    args = parser.parse_args()
    print(json.dumps(build(ROOT, args.output, args.version,
                           app_image=args.app_image, odoo_image=args.odoo_image), indent=2))


if __name__ == "__main__":
    main()
