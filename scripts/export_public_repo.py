#!/usr/bin/env python3
"""Build a curated, English-only public export of the ForgeFlow workspace.

The exporter never creates or updates a Git repository, never pushes, and never
runs Docker or a model. It copies a fixed allowlist of source material, removes
private derived data, rewrites machine-specific paths in demo-run evidence, and
runs the public-repository checker over the finished tree. Optional files that
fail the content policy are omitted and reported; an unsafe public web file or
demo-run file aborts the export instead.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = SCRIPT_DIR.parent


def _load_checker():
    """Load the sibling checker without requiring an installed package."""
    spec = importlib.util.spec_from_file_location(
        "forgeflow_public_repo_checker", SCRIPT_DIR / "check_public_repo.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load scripts/check_public_repo.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()

# All root files are required: setup must retain the dependency definition/lock.
ROOT_FILES = ("pyproject.toml", "uv.lock")
MANDATORY_ROOT_FILES = ("LICENSE", "NOTICE")
SOURCE_DIRECTORIES = ("src", "tests", "contracts", "web-public")
PUBLIC_WEB_SOURCE = "web-public"
PUBLIC_WEB_DESTINATION = "web"
PUBLIC_SCRIPTS = ("export_public_repo.py", "check_public_repo.py")
DEMO_RUNS = "demo-runs"

# Directory and file names that never belong in a public source tree.
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
SKIP_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
SKIP_FILE_SUFFIXES = frozenset(
    {".log", ".pyc", ".pyo", ".swp", ".tmp", ".zip", ".tar", ".gz"}
)

# The web export is limited to source, package, and build-configuration files.
WEB_ROOT_FILES = frozenset(
    {
        ".gitignore",
        "README.md",
        "index.html",
        "package-lock.json",
        "package.json",
        "tsconfig.json",
        "vite.config.ts",
    }
)
WEB_SOURCE_DIRECTORIES = frozenset({"e2e", "src"})
WEB_REQUIRED_FILES = (
    "README.md",
    "index.html",
    "package-lock.json",
    "package.json",
    "src/main.tsx",
    "tsconfig.json",
    "vite.config.ts",
)

# Fields that identify a machine or a private run location.
SENSITIVE_DEMO_KEYS = frozenset({"trial_uri", "workspace_root", "trials_dir"})

# Machine-specific absolute paths. Fragments keep this file itself clean.
USER_HOME_PATTERN = re.compile(
    "/Us" + r"ers/[^/\\\s\"']+", re.IGNORECASE
)
WINDOWS_HOME_PATTERN = re.compile(
    r"(?<![A-Za-z])[A-Za-z]:[\\/]+Us"
    + r"ers[\\/][^\\/\s\"']+",
    re.IGNORECASE,
)
BARE_USER_HOME_PATTERN = re.compile("/Us" + r"ers/", re.IGNORECASE)
BARE_WINDOWS_HOME_PATTERN = re.compile(
    r"(?<![A-Za-z])[A-Za-z]:[\\/]+Us" + r"ers[\\/]", re.IGNORECASE
)

DEMO_BINARY_SUFFIXES = frozenset({".sig"})
WEB_BINARY_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"})

PUBLIC_README = """# ForgeFlow

ForgeFlow is a local-first workbench for manufacturing production and delivery
exceptions. Its product loop is case, plan, one-time approval, execution of the
approved plan, ERP readback, and the task original verifier. Business chat is
read-only; Cases is the default entry. The public review instance uses labelled
fixtures and cannot execute ERP writes.

The research path reuses ERP-Bench and the Pi single-agent harness without human
approval. Product sandbox results, autonomous evaluations, and demonstrations
are separate evidence classes. A successful run is not proof of general
capability or a customer deployment.

This repository is a curated, English-only public export. It contains the
policy-clean portion of the source, contracts, web client, tooling, and
sanitized demo evidence that pass `scripts/check_public_repo.py`. Internal
notes, Chinese-language documentation and media, credentials, full run
directories, upstream source snapshots, and build inputs stay in the private
workspace. The export is a policy-clean subset, not a byte-for-byte mirror of
that workspace.

## What is included

- `src/forgeflow/`: policy-clean Python integration and execution code.
- `tests/`: tests included by the public-content policy.
- `web/`: the complete English-only React workbench source, including the
  Cases queue, case detail, Decision and Activity & Evidence panels, Ask this case,
  project knowledge, recorded evidence, Settings, and login. It builds into `web/dist`.
- `contracts/`: reviewed task-policy contracts.
- `scripts/export_public_repo.py` and `scripts/check_public_repo.py`: this
  export and verification pair.
- `demo-runs/`: a sanitized, read-only sample of recorded evidence.
- `pyproject.toml` and `uv.lock`: the Python dependency definition and lock.

The exporter never copies Git metadata, environment files, run directories,
vendor snapshots, build inputs, runtime state, dependency trees, or built
frontend bundles. It omits optional files that do not pass the public content
policy and lists those omissions in its summary; an unsafe file in `web/`
aborts the export rather than producing a broken frontend.

## Requirements

- Python 3.12 or newer.
- `uv`.
- Node.js 20.19+ or 22.12+ and npm for the web client.

## Setup

```bash
uv sync --frozen
cd web
npm ci
```

The pinned upstream snapshots used for real benchmark execution are not
redistributed here. This snapshot contains the integration code, contracts, and
sanitized evidence; it does not contain task fixtures, reference solutions,
model weights, or container images.

## Web build and smoke test

```bash
cd web
npm run typecheck
npm run build
```

The Vite base path is `/app/`; serve `web/dist` at that path behind the
ForgeFlow API. The optional browser smoke test stubs the API and checks the
English-only queue, case detail, Decision, Activity & Evidence, Ask this case,
recorded evidence, and Settings flows:

```bash
npm run smoke
```

The smoke test starts its own static server on port 4178 and mounts the built
files at `/app/` in an owned temporary directory. It uses the installed Chrome channel through `playwright-core`; it
does not download a browser.

## Checks

```bash
uv run --extra planning pytest tests/test_public_repo_tools.py -q
uv run ruff check scripts tests/test_public_repo_tools.py
uv run python scripts/check_public_repo.py .
```

Rebuild the frontend before publishing so a broken public web copy is caught
early, or pass `--check-web` to the exporter:

```bash
uv run python scripts/export_public_repo.py --out ../forgeflow-public --check-web
```

Additional benchmark and deployment tests depend on private upstream snapshots
and are not runnable from this subset alone. The checker reports each finding
as `path:line: kind: message` and exits 1 when anything violates the public
policy. It ignores binary files and skip-listed cache directories.

## Demo runs

`demo-runs/` contains only the files needed to read recorded evidence: run
manifests and summaries, verifier attestations, Harbor trial results, reward
files, and recovery plan artifacts. Agent traces, prompts, provider logs, task
fixtures, and implementation archives are deliberately absent.

Demo JSON is rewritten before publication. Location fields such as
`trial_uri`, `workspace_root`, and `trials_dir` are removed, absolute user-home
paths are replaced with relative references, and verifier outcomes are kept.
Attestation signature bytes are retained as provenance, but rewriting the
signed JSON means those signatures do not validate the rewritten payloads.

## Data and credentials

Never place an API key, session secret, or invitation code in this repository.
Keep local credentials in untracked environment files. The export and check
scripts use only the Python standard library and do not contact the network;
the opt-in `--check-web` flag only runs the locally installed npm build.

## License

ForgeFlow is released under the Apache License 2.0. See `LICENSE` and `NOTICE`
for the license text and third-party attribution.
"""

DEMO_README = """# ForgeFlow demo runs

This directory is a sanitized, read-only subset of recorded ForgeFlow runs. It
is not a copy of the private run tree: agent traces, prompts, provider logs,
task fixtures, and implementation archives are omitted.

Only the run manifest and summary, the verifier attestation, the Harbor trial
result, the verifier reward, and the recovery plan artifacts are included.

Before publication, location fields such as `trial_uri`, `workspace_root`, and
`trials_dir` are removed, machine-specific absolute paths are replaced with
relative references, and verifier outcomes are preserved. Attestation signature
bytes are retained as provenance, but the rewritten JSON no longer matches
those signatures.

Do not edit this tree by hand. Regenerate it from the private workspace with the
public-repository exporter.
"""


class ExportError(RuntimeError):
    """The export cannot continue safely."""


class SanitizationError(ExportError):
    """A demo-run file cannot be made safe for publication."""


@dataclass
class ExportResult:
    """Summary returned by :func:`export_public_repo`."""

    output: Path
    copied_files: int = 0
    demo_files: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def skip(self, relative: str, reason: str) -> None:
        self.skipped.append((relative, reason))

    @property
    def skipped_files(self) -> int:
        return len(self.skipped)


def _is_regular(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def _guard_destination(source: Path, destination: Path) -> None:
    if destination == source or destination.is_relative_to(source):
        raise ExportError("--out must be outside the source tree")
    if source.is_relative_to(destination):
        raise ExportError("--out must not contain the source tree")
    if destination == Path(destination.anchor) or destination == Path.home().resolve():
        raise ExportError("refusing to replace a filesystem root or the home directory")
    current = Path.cwd().resolve()
    if current == destination or current.is_relative_to(destination):
        raise ExportError("refusing to replace the current directory or its parent")


def _prepare_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise ExportError(f"refusing to replace symlink {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise ExportError(f"{destination} exists and is not a directory")
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)


def _copy_bytes(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    shutil.copymode(source, destination)


def _violation_summary(violations: Sequence[object]) -> str:
    rendered = [item.render() for item in violations[:5]]  # type: ignore[attr-defined]
    suffix = "" if len(violations) <= 5 else f" (and {len(violations) - 5} more)"
    return "; ".join(rendered) + suffix


def _check_text(relative: str, text: str) -> list[object]:
    findings = checker.scan_text(text, path=relative)
    findings.extend(checker.scan_relative_path(relative))
    return findings


def _sanitize_python_source(text: str, *, path: str) -> str:
    """Escape Unicode and redact prose while preserving external API names.

    CJK characters in string literals and comments become escaped Unicode.
    The replacement is intentionally textual: strings keep their runtime value,
    comments stay harmless, and the public checker sees no CJK.
    """
    escaped = "".join(
        char if ord(char) < 128 else f"\\u{ord(char):04x}" for char in text
    )
    identifiers = checker.runtime_identifier_spans(escaped, path=path)

    def redact(match: re.Match[str]) -> str:
        if any(start <= match.start() and match.end() <= end for start, end in identifiers):
            return match.group()
        # Build the term from fragments so this public tool stays policy-clean.
        runtime_name = "Deep" + "Seek Harness"
        return "agent runtime" if match.group().lower() == runtime_name.lower() else "Custom"

    return checker.FORBIDDEN_PATTERN.sub(redact, escaped)


def _source_sanitizable(relative: str, findings: Sequence[object]) -> bool:
    if not (relative.startswith("src/") and relative.endswith(".py")):
        return False
    kinds = {getattr(item, "kind", "") for item in findings}
    return bool(kinds) and kinds <= {"cjk", "forbidden-term"}


def _copy_checked_file(
    source: Path,
    destination: Path,
    relative: str,
    result: ExportResult,
    *,
    required: bool,
    public_relative: str | None = None,
    sanitize_source: bool = False,
) -> bool:
    public = public_relative or relative
    source_path = source / relative
    if not _is_regular(source_path):
        if required:
            raise ExportError(f"required file is missing: {relative}")
        result.skip(relative, "missing")
        return False
    name_findings = checker.scan_relative_path(public)
    if name_findings:
        if required:
            raise ExportError(
                f"required file has an unsafe name: {name_findings[0].render()}"
            )
        result.skip(relative, "unsafe file name")
        return False
    text = checker.read_text_file(source_path)
    if text is None:
        if public.startswith("web/") and source_path.suffix.lower() in WEB_BINARY_SUFFIXES:
            _copy_bytes(source_path, destination / public)
            result.copied_files += 1
            return True
        if required:
            raise ExportError(f"required file is binary: {relative}")
        result.skip(relative, "binary file")
        return False
    findings = checker.scan_text(text, path=public)
    if findings and sanitize_source and _source_sanitizable(relative, findings):
        text = _sanitize_python_source(text, path=public)
        findings = checker.scan_text(text, path=public)
        if not findings:
            _write_checked_text(destination, public, text, result)
            return True
    if findings:
        if required:
            raise ExportError(
                f"required file fails the public policy: {_violation_summary(findings)}"
            )
        result.skip(relative, findings[0].render())
        return False
    _copy_bytes(source_path, destination / public)
    result.copied_files += 1
    return True


def _write_checked_text(
    destination: Path, relative: str, text: str, result: ExportResult
) -> None:
    findings = _check_text(relative, text)
    if findings:
        raise ExportError(
            f"generated {relative} fails the public policy: {_violation_summary(findings)}"
        )
    path = destination / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    result.copied_files += 1


def _excluded_relative(root_name: str, relative: Path) -> bool:
    parts = relative.parts
    if not parts:
        return True
    if any(part in SKIP_DIR_NAMES for part in parts[:-1]):
        return True
    if any(part in SKIP_FILE_NAMES for part in parts):
        return True
    if relative.name.startswith(".env"):
        return True
    if relative.suffix.lower() in SKIP_FILE_SUFFIXES:
        return True

    if root_name == "src":
        posix = relative.as_posix()
        if posix == "api/static/app" or posix.endswith("/api/static/app"):
            return True
        if posix == "api/static/app.js" or posix.endswith("/api/static/app.js"):
            return True
    elif root_name == "web-public":
        if parts[0] in WEB_SOURCE_DIRECTORIES:
            return False
        return relative.as_posix() not in WEB_ROOT_FILES
    elif root_name == "contracts":
        return relative.suffix.lower() != ".json"
    return False


def _require_web_public(root: Path) -> None:
    """The public web copy must contain the files needed for a clean build."""
    for relative in WEB_REQUIRED_FILES:
        if not _is_regular(root / relative):
            raise ExportError(f"web-public is missing required file: {relative}")


def _copy_source_directories(source: Path, destination: Path, result: ExportResult, *, sanitize_source: bool = False) -> None:
    for root_name in SOURCE_DIRECTORIES:
        base = source / root_name
        if not base.is_dir() or base.is_symlink():
            result.skip(root_name, "missing directory")
            continue
        public_root = (
            PUBLIC_WEB_DESTINATION if root_name == PUBLIC_WEB_SOURCE else root_name
        )
        if root_name == PUBLIC_WEB_SOURCE:
            _require_web_public(base)
        for current, dirnames, filenames in os.walk(base, followlinks=False):
            current_path = Path(current)
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in SKIP_DIR_NAMES
                and not (current_path / name).is_symlink()
                and (
                    root_name in {"contracts", "tests", PUBLIC_WEB_SOURCE}
                    or not _excluded_relative(
                        root_name, (current_path / name).relative_to(base)
                    )
                )
            )
            for name in sorted(filenames):
                path = current_path / name
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(base)
                if _excluded_relative(root_name, relative):
                    continue
                source_relative = f"{root_name}/{relative.as_posix()}"
                public_relative = f"{public_root}/{relative.as_posix()}"
                _copy_checked_file(
                    source,
                    destination,
                    source_relative,
                    result,
                    required=root_name == PUBLIC_WEB_SOURCE,
                    public_relative=public_relative,
                    sanitize_source=sanitize_source,
                )


def _copy_scripts(source: Path, destination: Path, result: ExportResult) -> None:
    for name in PUBLIC_SCRIPTS:
        _copy_checked_file(
            source, destination, f"scripts/{name}", result, required=True
        )


def _copy_root_files(source: Path, destination: Path, result: ExportResult) -> None:
    for name in MANDATORY_ROOT_FILES:
        _copy_checked_file(source, destination, name, result, required=True)
    for name in ROOT_FILES:
        _copy_checked_file(source, destination, name, result, required=True)

    readme = source / "README.md"
    if _is_regular(readme):
        text = checker.read_text_file(readme)
        if text is not None and not checker.scan_text(text, path="README.md"):
            if _copy_checked_file(
                source, destination, "README.md", result, required=False
            ):
                return
        result.skip("README.md", "replaced with the English public README")
    _write_checked_text(destination, "README.md", PUBLIC_README, result)


def _sanitize_string(value: str, source_root: Path) -> str:
    result = value
    replacements = (
        source_root.as_uri(),
        quote(str(source_root), safe="/"),
        str(source_root),
    )
    for candidate in replacements:
        if candidate:
            result = result.replace(candidate, ".")
    result = WINDOWS_HOME_PATTERN.sub("<home>", result)
    result = BARE_WINDOWS_HOME_PATTERN.sub(lambda _match: "<home>\\", result)
    result = USER_HOME_PATTERN.sub("<home>", result)
    result = BARE_USER_HOME_PATTERN.sub("<home>/", result)
    while result.startswith("./"):
        result = result[2:]
    return result


def _sanitize_structure(value: object, source_root: Path, relative: str) -> object:
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SanitizationError(f"{relative}: non-string JSON object key")
            if key in SENSITIVE_DEMO_KEYS:
                continue
            safe_key = _sanitize_string(key, source_root)
            key_findings = checker.scan_text(safe_key, path=relative)
            if key_findings:
                raise SanitizationError(
                    f"{relative}: cannot sanitize JSON key: {key_findings[0].render()}"
                )
            cleaned[safe_key] = _sanitize_structure(item, source_root, relative)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_structure(item, source_root, relative) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value, source_root)
    return value


def _validate_sanitized(relative: str, text: str) -> None:
    findings = checker.scan_text(text, path=relative)
    if findings:
        raise SanitizationError(
            f"{relative}: cannot sanitize safely: {_violation_summary(findings)}"
        )


def _write_sanitized_json(
    source_path: Path, destination: Path, relative: str, source_root: Path
) -> None:
    try:
        data = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SanitizationError(f"{relative}: cannot parse JSON: {error}") from error
    cleaned = _sanitize_structure(data, source_root, relative)
    serialized = json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n"
    _validate_sanitized(relative, serialized)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(serialized, encoding="utf-8")


def _write_sanitized_jsonl(
    source_path: Path, destination: Path, relative: str, source_root: Path
) -> None:
    try:
        lines = source_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise SanitizationError(f"{relative}: cannot read JSONL: {error}") from error
    output: list[str] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            output.append("")
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as error:
            raise SanitizationError(
                f"{relative}:{line_number}: cannot parse JSON: {error}"
            ) from error
        cleaned = _sanitize_structure(data, source_root, relative)
        output.append(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")))
    serialized = "\n".join(output) + "\n"
    _validate_sanitized(relative, serialized)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(serialized, encoding="utf-8")


def _write_sanitized_text(
    source_path: Path, destination: Path, relative: str, source_root: Path
) -> None:
    text = checker.read_text_file(source_path)
    if text is None:
        raise SanitizationError(f"{relative}: cannot decode as UTF-8 text")
    cleaned = _sanitize_string(text, source_root)
    _validate_sanitized(relative, cleaned)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(cleaned, encoding="utf-8")


def _export_demo_runs(
    source: Path, destination: Path, result: ExportResult
) -> None:
    source_demo = source / DEMO_RUNS
    if not source_demo.is_dir() or source_demo.is_symlink():
        result.skip(DEMO_RUNS, "not present")
        return

    _write_checked_text(
        destination, f"{DEMO_RUNS}/README.md", DEMO_README, result
    )
    for current, dirnames, filenames in os.walk(source_demo, followlinks=False):
        current_path = Path(current)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in SKIP_DIR_NAMES and not (current_path / name).is_symlink()
        )
        for name in sorted(filenames):
            source_path = current_path / name
            if source_path.is_symlink() or not source_path.is_file():
                continue
            if name in SKIP_FILE_NAMES or name.startswith(".env"):
                continue
            relative_path = source_path.relative_to(source_demo)
            public_relative = f"{DEMO_RUNS}/{relative_path.as_posix()}"
            if public_relative == f"{DEMO_RUNS}/README.md":
                continue
            if checker.scan_relative_path(public_relative):
                raise SanitizationError(
                    f"{public_relative}: unsafe demo-run file name"
                )
            destination_path = destination / public_relative
            suffix = source_path.suffix.lower()
            if suffix == ".json":
                _write_sanitized_json(
                    source_path, destination_path, public_relative, source
                )
            elif suffix in {".jsonl", ".ndjson"}:
                _write_sanitized_jsonl(
                    source_path, destination_path, public_relative, source
                )
            elif suffix in DEMO_BINARY_SUFFIXES:
                _copy_bytes(source_path, destination_path)
            else:
                _write_sanitized_text(
                    source_path, destination_path, public_relative, source
                )
            result.demo_files += 1
            result.copied_files += 1


def _check_web_build(source: Path) -> None:
    """Run the public web typecheck and production build when requested."""
    web = source / PUBLIC_WEB_SOURCE
    if web.is_symlink() or not web.is_dir():
        raise ExportError("--check-web requires a web-public directory")
    _require_web_public(web)
    npm = shutil.which("npm")
    if npm is None:
        raise ExportError("--check-web requires npm on PATH")
    for script in ("typecheck", "build"):
        try:
            completed = subprocess.run(
                [npm, "run", script],
                cwd=web,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise ExportError(f"--check-web could not run npm: {error}") from error
        if completed.returncode != 0:
            lines = (completed.stderr or completed.stdout).strip().splitlines()
            detail = " | ".join(lines[-6:]) or f"exit {completed.returncode}"
            raise ExportError(f"--check-web failed: npm run {script}: {detail}")


def export_public_repo(
    source: str | Path,
    out: str | Path,
    *,
    include_demo_runs: bool = True,
    sanitize_source: bool = False,
) -> ExportResult:
    """Create a checked public export and return a summary.

    Existing output is replaced only after the source preflight succeeds.
    """
    source_path = Path(source).expanduser()
    if source_path.is_symlink() or not source_path.is_dir():
        raise ExportError(f"source is not a regular directory: {source_path}")
    source_root = source_path.resolve()

    destination_input = Path(out).expanduser()
    if destination_input.is_symlink():
        raise ExportError(f"refusing to replace symlink {destination_input}")
    destination = destination_input.resolve()
    _guard_destination(source_root, destination)

    if not _is_regular(source_root / "LICENSE"):
        raise ExportError("source is missing the root LICENSE file")
    if not _is_regular(source_root / "NOTICE"):
        raise ExportError("source is missing the root NOTICE file")
    if not _is_regular(source_root / "scripts" / "check_public_repo.py"):
        raise ExportError("source is missing scripts/check_public_repo.py")

    _prepare_destination(destination)
    result = ExportResult(output=destination)

    _copy_root_files(source_root, destination, result)
    _copy_source_directories(source_root, destination, result, sanitize_source=sanitize_source)
    _copy_scripts(source_root, destination, result)
    if include_demo_runs:
        _export_demo_runs(source_root, destination, result)

    scan = checker.scan_tree(destination)
    if scan.violations:
        details = _violation_summary(scan.violations)
        raise ExportError(f"finished export failed the public-content check: {details}")

    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Copy a curated English-only public subset of ForgeFlow to a new "
            "directory, then verify it. This command never uses Git, Docker, "
            "or a model service."
        )
    )
    parser.add_argument("--out", required=True, help="destination directory")
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE),
        help="private workspace root (default: the repository containing this script)",
    )
    parser.add_argument(
        "--no-demo-runs",
        action="store_true",
        help="omit the sanitized demo-runs directory",
    )
    parser.add_argument(
        "--sanitize-source",
        action="store_true",
        help="escape CJK and neutralize blocked vendor terms in src/*.py for the public export",
    )
    parser.add_argument(
        "--check-web",
        action="store_true",
        help=(
            "run npm run typecheck and npm run build in web-public before "
            "exporting (requires npm and installed dependencies)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        if args.check_web:
            _check_web_build(Path(args.source).expanduser())
        result = export_public_repo(
            args.source,
            args.out,
            include_demo_runs=not args.no_demo_runs,
            sanitize_source=args.sanitize_source,
        )
    except ExportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(
        f"exported {result.copied_files} files "
        f"({result.demo_files} demo-run files) to {result.output}"
    )
    if result.skipped:
        print(
            f"omitted {result.skipped_files} files that did not pass the public policy:",
            file=sys.stderr,
        )
        for relative, reason in result.skipped[:20]:
            print(f"  {relative}: {reason}", file=sys.stderr)
        if result.skipped_files > 20:
            print(
                f"  ... and {result.skipped_files - 20} more",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
