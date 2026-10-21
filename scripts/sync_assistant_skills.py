#!/usr/bin/env python3
"""Mirror the ForgeFlow assistant skill bundles into a DSH skills root.

DSH discovers a skill as ``<root>/<kebab-name>/SKILL.md`` one level deep. This
script validates each authored bundle and copies it into the target root used
by the read-only assistant workspace. It is safe to run repeatedly:

* content is staged and swapped in, never written through a symlink;
* unchanged bundles keep their files and mtimes;
* only skill names recorded in the local manifest are removed when they
  disappear from the source;
* unrelated skill directories already in the target are preserved;
* secret-looking filenames and credential-like content are refused.

The default target is ``.runtime/assistant-workspace/.dsh/skills``. DSH resolves
its project root at the nearest ``.git`` ancestor, so when that workspace sits
inside the ForgeFlow checkout the harness must also configure
``skill-filesystem.customSkillDirs`` (or sync to ``$DSH_HOME/skills``) for the
root to be discovered. Run this after ``scripts/assistant_workspace.py --sync``,
because that script rebuilds the workspace directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assistant" / "skills"
DEFAULT_TARGET = ROOT / ".runtime" / "assistant-workspace" / ".dsh" / "skills"
EXPECTED_SKILLS = ("case-walkthrough", "demo-rehearsal", "evidence-chase")

MANIFEST_NAME = ".forgeflow-skills.json"
MANIFEST_OWNER = "scripts/sync_assistant_skills.py"
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
MAX_DESCRIPTION = 500  # DSH's default catalog description cap
MAX_BODY = 20_000
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 64
MAX_BUNDLE_BYTES = 1024 * 1024

_ALLOWED_KEYS = frozenset({"name", "description", "whenToUse"})
_JUNK_DIRS = frozenset({"__pycache__", ".git", "node_modules"})
_JUNK_FILES = frozenset({".DS_Store"})
_JUNK_SUFFIXES = (".pyc", ".pyo")

_SECRET_NAMES = re.compile(
    r"(^|/)(\.env(\..*)?|\.npmrc|\.netrc|\.pypirc|\.git-credentials|"
    r"id_rsa.*|id_ed25519.*|credentials(\..*)?|secrets?(\..*)?)$"
    r"|\.(pem|key|p12|pfx|keystore|jks)$",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
)


class SkillError(ValueError):
    """A source bundle or target state cannot be synced safely."""


@dataclass(frozen=True)
class SkillBundle:
    """A validated skill bundle read from the source tree."""

    name: str
    path: Path
    description: str
    when_to_use: str | None
    body: str
    files: tuple[tuple[str, bytes], ...]
    digest: str


@dataclass(frozen=True)
class SyncResult:
    """The observable outcome of one sync or check."""

    target: Path
    written: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    check: bool = False
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        """Whether a check found the target in sync with the source."""
        return not self.problems

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe summary for the CLI."""
        return {
            "target": str(self.target),
            "check": self.check,
            "dry_run": self.dry_run,
            "ok": self.ok,
            "written": list(self.written),
            "unchanged": list(self.unchanged),
            "removed": list(self.removed),
            "preserved": list(self.preserved),
            "problems": list(self.problems),
        }


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse the conservative YAML subset used by these skill bundles.

    Only single-line plain scalars for ``name``, ``description`` and
    ``whenToUse`` are accepted. Rejecting anything else keeps the authored
    files valid for DSH's full YAML parser without adding a PyYAML dependency.
    """
    if not text.startswith("---\n"):
        raise SkillError("SKILL.md must start with a YAML frontmatter block")
    closing = text.find("\n---\n", 4)
    if closing < 0:
        raise SkillError("SKILL.md frontmatter must be closed by ---")
    data: dict[str, str] = {}
    for line in text[4:closing].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise SkillError(f"frontmatter line is not key: value: {stripped!r}")
        key = key.strip()
        value = value.strip()
        if key not in _ALLOWED_KEYS:
            raise SkillError(f"unsupported frontmatter key: {key!r}")
        if key in data:
            raise SkillError(f"duplicate frontmatter key: {key!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            raise SkillError(f"frontmatter key {key!r} needs a non-empty value")
        if any(character in value for character in "\r\n\t"):
            raise SkillError(f"frontmatter key {key!r} must stay on one line")
        if ": " in value or " #" in value:
            raise SkillError(f"frontmatter key {key!r} must use a plain scalar")
        data[key] = value
    for required in ("name", "description"):
        if required not in data:
            raise SkillError(f"frontmatter requires {required!r}")
    if not SKILL_NAME.fullmatch(data["name"]):
        raise SkillError(f"invalid kebab-case skill name: {data['name']!r}")
    if len(data["description"]) > MAX_DESCRIPTION:
        raise SkillError(f"description exceeds {MAX_DESCRIPTION} characters")
    body = text[closing + 5 :].strip()
    if not body:
        raise SkillError("SKILL.md body is empty")
    if body.startswith("---"):
        raise SkillError("SKILL.md body must not start with another frontmatter block")
    if len(body) > MAX_BODY:
        raise SkillError(f"skill body exceeds {MAX_BODY} characters")
    return data, body


def load_bundle(path: Path) -> SkillBundle:
    """Validate and read one directory bundle."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise SkillError(f"{path.name}: skill bundle must be a regular directory")
    skill_file = path / "SKILL.md"
    if skill_file.is_symlink() or not skill_file.is_file():
        raise SkillError(f"{path.name}: missing regular SKILL.md")
    text = _read_text(skill_file, f"{path.name}/SKILL.md")
    data, body = parse_frontmatter(text)
    if data["name"] != path.name:
        raise SkillError(
            f"{path.name}: frontmatter name {data['name']!r} must match the directory"
        )
    files = _collect_files(path, path.name)
    return SkillBundle(
        name=path.name,
        path=path,
        description=data["description"],
        when_to_use=data.get("whenToUse"),
        body=body,
        files=files,
        digest=_file_digest(files),
    )


def discover_bundles(source: Path = SOURCE) -> tuple[SkillBundle, ...]:
    """Return every direct child directory bundle in a source root."""
    source = Path(source)
    if source.is_symlink():
        raise SkillError("source skills root must not be a symbolic link")
    if not source.is_dir():
        raise SkillError(f"source skills root does not exist: {source}")
    bundles: list[SkillBundle] = []
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        if child.name.startswith(".") or child.name in _JUNK_DIRS:
            continue
        if child.is_symlink():
            raise SkillError(f"{child.name}: skill bundles must not be symbolic links")
        if child.is_dir():
            bundles.append(load_bundle(child))
    if not bundles:
        raise SkillError(f"no skill bundles found in {source}")
    return tuple(sorted(bundles, key=lambda bundle: bundle.name))


def sync(
    source: Path = SOURCE,
    target: Path = DEFAULT_TARGET,
    *,
    check: bool = False,
    dry_run: bool = False,
    required: tuple[str, ...] = EXPECTED_SKILLS,
) -> SyncResult:
    """Copy validated bundles into ``target``, or report drift when checking."""
    if check and dry_run:
        raise SkillError("check and dry_run are mutually exclusive")
    source = _resolve_source(source)
    target = _resolve_target(target)
    if target == source or target.is_relative_to(source) or source.is_relative_to(target):
        raise SkillError("source and target directories must not overlap")

    bundles = discover_bundles(source)
    names = {bundle.name for bundle in bundles}
    missing = sorted(set(required) - names)
    if missing:
        raise SkillError("missing required skill bundles: " + ", ".join(missing))

    if target.exists() and not target.is_dir():
        raise SkillError(f"target is not a directory: {target}")
    manifest_path = target / MANIFEST_NAME
    managed = _load_manifest(manifest_path) if target.exists() else {}

    written: list[str] = []
    unchanged: list[str] = []
    removed: list[str] = []
    preserved: list[str] = []
    problems: list[str] = []
    staged: list[SkillBundle] = []

    if check and not target.exists():
        problems.append("target directory is missing")

    for bundle in bundles:
        destination = target / bundle.name
        current_digest, issue = _target_bundle_state(destination)
        if issue is None and current_digest == bundle.digest:
            unchanged.append(bundle.name)
            continue
        reason = issue or ("missing" if current_digest is None else "outdated")
        if check:
            problems.append(f"{bundle.name}: {reason}")
        elif dry_run:
            written.append(bundle.name)
        else:
            staged.append(bundle)
            written.append(bundle.name)

    stale = sorted(set(managed) - names)
    for name in stale:
        destination = target / name
        if not destination.exists() and not destination.is_symlink():
            continue
        if check:
            problems.append(f"{name}: stale managed skill")
        elif dry_run:
            removed.append(name)
        else:
            _remove_path(destination)
            removed.append(name)

    if target.exists():
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            if child.name == MANIFEST_NAME or child.name.startswith("."):
                continue
            if child.name in names or child.name in managed:
                continue
            preserved.append(child.name)

    desired_manifest = _manifest_text(bundles)
    current_manifest = _read_text(manifest_path, MANIFEST_NAME) if manifest_path.is_file() else ""
    if check and current_manifest != desired_manifest:
        problems.append("manifest is missing or out of date")

    if not check and not dry_run:
        if target.exists() and not target.is_dir():
            raise SkillError(f"target is not a directory: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if staged:
            _install_bundles(staged, target)
        if current_manifest != desired_manifest:
            _write_manifest(manifest_path, desired_manifest)

    return SyncResult(
        target=target,
        written=tuple(written),
        unchanged=tuple(unchanged),
        removed=tuple(removed),
        preserved=tuple(preserved),
        problems=tuple(problems),
        check=check,
        dry_run=dry_run,
    )


def _resolve_source(source: Path) -> Path:
    source = Path(source).expanduser()
    if source.is_symlink():
        raise SkillError("source skills root must not be a symbolic link")
    return source.resolve()


def _resolve_target(target: Path) -> Path:
    target = Path(target).expanduser()
    if target.is_symlink():
        raise SkillError("target skills root must not be a symbolic link")
    return target.resolve()


def _read_text(path: Path, label: str) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f"{label}: not readable UTF-8 text") from exc
    if "\x00" in text:
        raise SkillError(f"{label}: binary content is not allowed")
    return text


def _decode(data: bytes, label: str) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError(f"{label}: not UTF-8 text") from exc
    if "\x00" in text:
        raise SkillError(f"{label}: binary content is not allowed")
    return text


def _is_junk(relative: str) -> bool:
    parts = relative.split("/")
    if any(part in _JUNK_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    return name in _JUNK_FILES or name.endswith(_JUNK_SUFFIXES)


def _collect_files(bundle: Path, name: str) -> tuple[tuple[str, bytes], ...]:
    files: list[tuple[str, bytes]] = []
    total = 0
    for root, directories, filenames in os.walk(bundle, followlinks=False):
        root_path = Path(root)
        for directory in list(directories):
            if (root_path / directory).is_symlink():
                raise SkillError(f"{name}: symbolic links are not allowed")
            if directory in _JUNK_DIRS:
                directories.remove(directory)
        for filename in sorted(filenames):
            source = root_path / filename
            relative = source.relative_to(bundle).as_posix()
            if _is_junk(relative):
                continue
            if source.is_symlink():
                raise SkillError(f"{name}/{relative}: symbolic links are not allowed")
            if not source.is_file():
                raise SkillError(f"{name}/{relative}: only regular files are allowed")
            if _SECRET_NAMES.search(relative):
                raise SkillError(f"{name}/{relative}: refusing a credential-like file")
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise SkillError(f"{name}/{relative}: unreadable") from exc
            if len(data) > MAX_FILE_BYTES:
                raise SkillError(f"{name}/{relative}: file is too large")
            total += len(data)
            if total > MAX_BUNDLE_BYTES:
                raise SkillError(f"{name}: bundle is too large")
            text = _decode(data, f"{name}/{relative}")
            if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
                raise SkillError(f"{name}/{relative}: credential-like content is not allowed")
            files.append((relative, data))
            if len(files) > MAX_FILES:
                raise SkillError(f"{name}: too many files")
    if not any(relative == "SKILL.md" for relative, _ in files):
        raise SkillError(f"{name}: missing SKILL.md")
    return tuple(sorted(files))


def _file_digest(files: tuple[tuple[str, bytes], ...]) -> str:
    digest = hashlib.sha256()
    for relative, data in files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _target_bundle_state(path: Path) -> tuple[str | None, str | None]:
    """Return the target bundle digest and an optional safety issue."""
    if path.is_symlink():
        return None, "is a symbolic link"
    if not path.exists():
        return None, None
    if not path.is_dir():
        return None, "is not a directory"
    digest = hashlib.sha256()
    for root, directories, filenames in os.walk(path, followlinks=False):
        root_path = Path(root)
        for directory in directories:
            if (root_path / directory).is_symlink():
                return None, "contains a symbolic link"
        for filename in sorted(filenames):
            item = root_path / filename
            relative = item.relative_to(path).as_posix()
            if _is_junk(relative):
                continue
            if item.is_symlink():
                return None, "contains a symbolic link"
            try:
                data = item.read_bytes()
            except OSError:
                return None, "is unreadable"
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest(), None


def _load_manifest(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise SkillError(f"{MANIFEST_NAME}: must be a regular file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillError(f"{MANIFEST_NAME}: not valid JSON") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SkillError(f"{MANIFEST_NAME}: unsupported manifest version")
    skills = data.get("skills")
    if not isinstance(skills, dict):
        raise SkillError(f"{MANIFEST_NAME}: skills must be an object")
    manifest: dict[str, str] = {}
    for name, digest in skills.items():
        if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
            raise SkillError(f"{MANIFEST_NAME}: invalid skill name")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise SkillError(f"{MANIFEST_NAME}: invalid digest for {name}")
        manifest[name] = digest
    return manifest


def _manifest_text(bundles: tuple[SkillBundle, ...]) -> str:
    data = {
        "version": 1,
        "managed_by": MANIFEST_OWNER,
        "skills": {bundle.name: bundle.digest for bundle in bundles},
    }
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _write_manifest(path: Path, text: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _install_bundles(bundles: list[SkillBundle], target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".forgeflow-skills-", dir=target.parent))
    try:
        for bundle in bundles:
            destination = staging / bundle.name
            destination.mkdir()
            destination.chmod(0o755)
            for relative, data in bundle.files:
                item = destination / relative
                item.parent.mkdir(parents=True, exist_ok=True)
                item.parent.chmod(0o755)
                item.write_bytes(data)
                item.chmod(0o644)
        for bundle in bundles:
            destination = target / bundle.name
            _assert_inside(destination, target)
            if destination.exists() or destination.is_symlink():
                _remove_path(destination)
            (staging / bundle.name).replace(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _assert_inside(path: Path, root: Path) -> None:
    if path != root and root not in path.parents:
        raise SkillError(f"refusing to touch a path outside the target: {path}")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _print_result(result: SyncResult) -> None:
    if result.check:
        if result.ok:
            count = len(result.unchanged)
            print(f"assistant skills: up to date ({count} skills)")
        else:
            print("assistant skills: drift detected")
            for problem in result.problems:
                print(f"  - {problem}")
        return
    action = "would sync" if result.dry_run else "synced"
    print(
        f"assistant skills: {action} {len(result.written)} written, "
        f"{len(result.unchanged)} unchanged, {len(result.removed)} removed "
        f"-> {_display(result.target)}"
    )
    if result.preserved:
        print(f"  preserved unmanaged: {', '.join(result.preserved)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync ForgeFlow assistant skills into a DSH root")
    parser.add_argument("--source", type=Path, default=SOURCE, help="authored skill bundles")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="DSH skills root")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report drift without writing")
    mode.add_argument("--dry-run", action="store_true", help="show changes without writing")
    parser.add_argument("--json", action="store_true", help="print a JSON summary")
    args = parser.parse_args(argv)
    try:
        result = sync(
            source=args.source,
            target=args.target,
            check=args.check,
            dry_run=args.dry_run,
        )
    except (SkillError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        _print_result(result)
    if result.check and not result.ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
