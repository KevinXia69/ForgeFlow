#!/usr/bin/env python3
"""Check a directory for content that must never reach the public export.

The checker is standard-library only and intentionally conservative. It walks
regular text files, skips binary files and dependency/VCS directories, and
reports every finding as ``path:line: kind: message``. A clean directory exits
0; any finding exits 1; an unusable path exits 2.

Sensitive words and path prefixes are assembled from fragments below so this
checker can itself be scanned without matching its own policy definitions.
"""

from __future__ import annotations

import argparse
import ast
import io
import os
import re
import sys
import tokenize
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

# Han, kana, bopomofo, CJK punctuation, radicals, and compatibility blocks.
_CJK_RANGES = (
    (0x2E80, 0x2EFF),
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x1100, 0x11FF),
    (0x3100, 0x312F),
    (0x3130, 0x318F),
    (0x3190, 0x319F),
    (0x31A0, 0x31BF),
    (0x31C0, 0x31EF),
    (0x31F0, 0x31FF),
    (0x3200, 0x32FF),
    (0x3300, 0x33FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xA960, 0xA97F),
    (0xAC00, 0xD7AF),
    (0xD7B0, 0xD7FF),
    (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F),
    (0xFF00, 0xFFEF),
    (0x20000, 0x2FA1F),
    (0x30000, 0x3134F),
)


def _regex_codepoint(value: int) -> str:
    if value <= 0xFFFF:
        return f"\\u{value:04x}"
    return f"\\U{value:08x}"


CJK_PATTERN = re.compile(
    "["
    + "".join(
        f"{_regex_codepoint(start)}-{_regex_codepoint(end)}"
        for start, end in _CJK_RANGES
    )
    + "]"
)

# The public policy blocks a small set of vendor/product names. They are built
# from fragments so this source file does not contain the literal terms.
_FORBIDDEN_PARTS = (
    ("Chat", "GPT"),
    ("Co", "dex"),
    ("Deep", "Seek", " Harness"),
    ("Deep", "Seek"),
)
FORBIDDEN_TERMS = tuple("".join(parts) for parts in _FORBIDDEN_PARTS)
FORBIDDEN_PATTERN = re.compile(
    "|".join(re.escape(term) for term in FORBIDDEN_TERMS), re.IGNORECASE
)

# Absolute user-home paths, including Windows drive-letter forms.
USER_PATH_PATTERN = re.compile(
    "/Us" + r"ers/|[A-Za-z]:[\\/]+Us" + r"ers[\\/]", re.IGNORECASE
)

# PEM private-key blocks only. Public keys and certificates are not secrets.
PEM_PRIVATE_PATTERN = re.compile(
    "-----BEGIN " + r"(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?" + "-----"
)

# Common provider tokens, assignment forms, and bearer headers. Values are
# never printed by the checker, only the location and category.
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b"),
    re.compile(
        r"(?i)[\"']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
        r"secret[_-]?key|invite(?:[_-]?codes?)?)[\"']?"
        r"\s*[:=]\s*[\"']?(?!(?:FORGEFLOW|NEBIUS)_)"
        r"[A-Za-z0-9_\-]{16,}(?![\w(])"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{24,}"),
)

_SKIP_DIR_NAMES = frozenset(
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
        "node_modules",
        "venv",
    }
)
_BINARY_SUFFIXES = frozenset(
    {
        ".bin",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".mov",
        ".mp4",
        ".otf",
        ".pdf",
        ".png",
        ".pyc",
        ".sig",
        ".so",
        ".tar",
        ".ttf",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
        ".zip",
    }
)


@dataclass(frozen=True)
class Violation:
    """One policy finding at a stable file:line location."""

    path: str
    line: int
    kind: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.kind}: {self.message}"


@dataclass(frozen=True)
class ScanResult:
    """Counts and findings from one directory scan."""

    text_files: int
    binary_files: int
    violations: tuple[Violation, ...]


def _is_third_party_license(path: str) -> bool:
    """Third-party MIT/Apache notices must keep their copyright wording.

    Vendored source licenses are the one deliberate exception to the
    vendor-term policy: removing the attribution would make the redistribution
    unlawful. Restrict the exception to the vendored DSH license path; ordinary
    source, docs and translations still fail on the same terms.
    """
    normalized = path.replace("\\", "/").lstrip("./")
    return normalized.endswith("/LICENSE") and "/dsh/" in f"/{normalized}"


def _is_dependency_manifest(path: str) -> bool:
    """Dependency names are factual package metadata, not product prose."""
    normalized = path.replace("\\", "/").lstrip("./")
    return normalized in {
        "pyproject.toml", "uv.lock", "web/package.json", "web/package-lock.json"
    }


def runtime_identifier_spans(text: str, *, path: str) -> tuple[tuple[int, int], ...]:
    """Keep exact external API identifiers in the two runtime integration files.

    This exception applies only to whole Python tokens, never prose, arbitrary
    string contents, or another file. All other content checks still apply.
    """
    normalized = path.replace("\\", "/").lstrip("./")
    vendor = "Deep" + "Seek"
    names = {vendor.lower() + "_harness", vendor + "Harness"}
    packages = {"@" + vendor.lower() + "-ai/dsh-mcp-client",
                "session-log-" + vendor.lower()}
    allowed_names = normalized == "src/forgeflow/api/dsh_sdk.py"
    allowed_package = normalized == "src/forgeflow/api/dsh_permissions.py"
    if not (allowed_names or allowed_package):
        return ()
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    spans = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            allowed = allowed_names and token.type == tokenize.NAME and token.string in names
            if allowed_package and token.type == tokenize.STRING:
                try:
                    value = ast.literal_eval(token.string)
                    allowed = isinstance(value, str) and value in packages
                except (SyntaxError, ValueError):
                    allowed = False
            if allowed:
                spans.append((
                    offsets[token.start[0] - 1] + token.start[1],
                    offsets[token.end[0] - 1] + token.end[1],
                ))
    except (tokenize.TokenError, IndentationError):
        return ()
    return tuple(spans)


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan_text(text: str, *, path: str = "<text>") -> list[Violation]:
    """Return the policy findings in ``text``, one per kind per line."""
    violations: list[Violation] = []
    seen: set[tuple[str, int]] = set()

    def add(kind: str, line: int, message: str) -> None:
        key = (kind, line)
        if key not in seen:
            seen.add(key)
            violations.append(Violation(path=path, line=line, kind=kind, message=message))

    for match in CJK_PATTERN.finditer(text):
        add("cjk", _line_number(text, match.start()), "contains a CJK character")
    if not _is_third_party_license(path) and not _is_dependency_manifest(path):
        identifiers = runtime_identifier_spans(text, path=path)
        for match in FORBIDDEN_PATTERN.finditer(text):
            if any(start <= match.start() and match.end() <= end for start, end in identifiers):
                continue
            add(
                "forbidden-term",
                _line_number(text, match.start()),
                "contains a blocked model-vendor term",
            )
    for match in USER_PATH_PATTERN.finditer(text):
        add(
            "user-path",
            _line_number(text, match.start()),
            "contains an absolute user-home path",
        )
    for match in PEM_PRIVATE_PATTERN.finditer(text):
        add(
            "private-key",
            _line_number(text, match.start()),
            "contains a PEM private-key block",
        )
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            add(
                "credential",
                _line_number(text, match.start()),
                "matches a common API-key or invite-code pattern",
            )

    violations.sort(key=lambda item: (item.line, item.kind, item.message))
    return violations


def scan_relative_path(relative: str) -> list[Violation]:
    """Flag CJK or blocked terms in a file or directory name."""
    violations: list[Violation] = []
    if CJK_PATTERN.search(relative):
        violations.append(
            Violation(relative, 1, "cjk", "file name contains a CJK character")
        )
    if FORBIDDEN_PATTERN.search(relative):
        violations.append(
            Violation(
                relative,
                1,
                "forbidden-term",
                "file name contains a blocked model-vendor term",
            )
        )
    return violations


def read_text_file(path: Path) -> str | None:
    """Read UTF-8 text, or return None when the file is binary."""
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return None
    try:
        data = path.read_bytes()
    except OSError as error:
        raise OSError(f"cannot read {path}: {error}") from error
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _iter_entries(root: Path) -> Iterator[Path]:
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _SKIP_DIR_NAMES and not (current_path / name).is_symlink()
        )
        for name in dirnames:
            yield current_path / name
        for name in sorted(filenames):
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def _license_violation(root: Path) -> Violation | None:
    path = root / "LICENSE"
    if path.is_symlink() or not path.is_file():
        return Violation("LICENSE", 1, "license", "missing root LICENSE file")
    try:
        empty = path.stat().st_size == 0
    except OSError:
        return Violation("LICENSE", 1, "license", "cannot read root LICENSE file")
    if empty:
        return Violation("LICENSE", 1, "license", "root LICENSE file is empty")
    return None


def scan_tree(root: Path) -> ScanResult:
    """Scan every regular text file below ``root`` and require a LICENSE."""
    if root.is_symlink() or not root.is_dir():
        raise OSError(f"not a directory: {root}")
    violations: list[Violation] = []
    license_violation = _license_violation(root)
    if license_violation is not None:
        violations.append(license_violation)

    text_files = 0
    binary_files = 0
    for path in _iter_entries(root):
        relative = path.relative_to(root).as_posix()
        violations.extend(scan_relative_path(relative))
        if path.is_dir():
            continue
        text = read_text_file(path)
        if text is None:
            binary_files += 1
            continue
        text_files += 1
        violations.extend(scan_text(text, path=relative))

    violations.sort(key=lambda item: (item.path, item.line, item.kind, item.message))
    return ScanResult(
        text_files=text_files,
        binary_files=binary_files,
        violations=tuple(violations),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check a directory for content that must not appear in the "
            "ForgeFlow public repository export."
        )
    )
    parser.add_argument("directory", help="directory (or single file) to check")
    args = parser.parse_args(argv)

    root = Path(args.directory).expanduser()
    if root.is_symlink() or not root.exists():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    try:
        if root.is_file():
            text = read_text_file(root)
            if text is None:
                print(f"error: {root} is not a UTF-8 text file", file=sys.stderr)
                return 2
            result = ScanResult(
                text_files=1,
                binary_files=0,
                violations=tuple(scan_text(text, path=root.name)),
            )
        elif root.is_dir():
            result = scan_tree(root)
        else:
            print(f"error: not a directory: {root}", file=sys.stderr)
            return 2
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    for violation in result.violations:
        print(violation.render())
    print(
        f"checked {result.text_files} text files "
        f"({result.binary_files} binary files skipped)",
        file=sys.stderr,
    )
    return 1 if result.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
