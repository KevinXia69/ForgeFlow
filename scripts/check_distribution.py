#!/usr/bin/env python3
"""Check a source/release staging tree without rewriting its runtime bytes.

This is a release guard, not proof that arbitrary binaries contain no secrets.
Use a clean staging tree (for example a Git archive), review binary assets and
keep the resulting manifest with the release. Findings never include values.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

REQUIRED = (
    "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "pyproject.toml", "uv.lock",
    "src/forgeflow/cli.py", "src/forgeflow/api/app.py", "src/forgeflow/api/web.py",
    "src/forgeflow/api/dsh_environment.py", "src/forgeflow/api/dsh_sdk.py",
    "src/forgeflow/api/reference_facts.py", "src/forgeflow/knowledge/reference_facts.py",
    "src/forgeflow/knowledge/reference_pack.py", "src/forgeflow/semantic/config.py",
    "src/forgeflow/superagent/store.py", "src/forgeflow/superagent/supervisor.py",
    "src/forgeflow/integrations/odoo20/verifier.py", "src/forgeflow/planning/multi_bom.py",
    "scripts/setup_dsh_harness.py", "scripts/assistant_mcp.py",
    "scripts/install_reference_knowledge.py", "scripts/run_supervisor.py",
    "scripts/run_docmind_worker.py", "deploy/vps/dsh/package.json",
    "scripts/build_installers.py", "scripts/accept_local_stack.py", "scripts/local_stack_acceptance.py",
    "tests/integrations/workbench_seed_probe.py", "tests/integrations/workbench_state_probe.py",
    "tests/integrations/workbench_control_probe.py", "tests/integrations/workbench_http_probe.py",
    "tests/integrations/workbench_recovery_probe.py", "tests/fixtures/workbench_embedding_provider.py",
    "deploy/vps/dsh/package-lock.json", "deploy/pandoc-reference.docx",
    "migrations/control/001_control.sql", "migrations/control/024_trigger_admission_cardinality.sql",
    "odoo-addons/forgeflow_general/__manifest__.py",
    "odoo-addons/forgeflow_general/models/approval_delegation.py",
    "odoo-addons/forgeflow_general/models/authorization.py",
    "odoo-addons/forgeflow_general/security/ir.access.csv",
    "odoo-addons/forgeflow_general/security/forgeflow_security.xml",
    "web/package.json", "web/package-lock.json", "web/vite.config.ts",
    "web/index.html", "web/tsconfig.json", "web/src/main.tsx",
    "web/src/historicalDemo.json", "web/src/PrivateMarkdown.tsx",
    "docs/en/product-guide.md", "docs/en/technical-guide.md",
    "docs/en/workbench-guide.md", "docs/en/current-capabilities.md",
    "src/forgeflow/local_workbench/setup.py", "src/forgeflow/local_workbench/config.py",
    "src/forgeflow/local_workbench/runtime.py", "src/forgeflow/local_workbench/bootstrap.py",
    "deploy/workbench/Dockerfile.app", "deploy/workbench/Dockerfile.odoo",
    "deploy/workbench/odoo_init.py", "deploy/workbench/odoo_bootstrap.py",
    "deploy/workbench/requirements.lock", "deploy/workbench/compose.yaml",
    "deploy/workbench/compose.source.yaml", "installer/start.sh", "installer/stop.sh",
    "installer/Start-ForgeFlow.command", "installer/Start-ForgeFlow.cmd",
    "installer/Start-ForgeFlow.ps1", "installer/Stop-ForgeFlow.cmd", "installer/Stop-ForgeFlow.ps1",
    "README.md", "README.zh-CN.md", "docs/LOCAL_INSTALL.md",
    "docs/images/local-workbench-home.png",
)
PACK = "knowledge/reference/supply-recovery-v1"
IGNORE = {".git", ".venv", "venv", "node_modules", "__pycache__",
          ".pytest_cache", ".ruff_cache", ".mypy_cache"}
PRIVATE_PARTS = {".runtime", "secrets", "backups", "filestore", "pgdata", ".ssh"}
PRIVATE_ROOTS = {"runs", "approvals", "attachments", "logs", "vendor", "demo-runs"}
PRIVATE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".dump", ".backup", ".bak",
                    ".log", ".pem", ".key", ".p12", ".pfx", ".tar", ".gz", ".zip"}
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
                  ".woff", ".woff2", ".ttf", ".otf"}
PROVIDER_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|(?:AKIA|ASIA)[0-9A-Z]{16}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})\b")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
SECRET_NAME = r"(?:[A-Za-z0-9_]*(?:api[_-]?key|secret|password|access[_-]?token|auth[_-]?token|invite(?:[_-]?codes?)?))"
QUOTED_SECRET = re.compile(
    rf"(?i)[\"']?{SECRET_NAME}[\"']?\s*[:=]\s*[\"'](?P<value>[A-Za-z0-9_./+@=:-]{{12,}})[\"']")
ENV_SECRET = re.compile(
    rf"(?im)^\s*{SECRET_NAME}\s*[:=]\s*(?P<value>[A-Za-z0-9_./+@=:-]{{12,}})\s*$")
URL_CREDENTIAL = re.compile(r"(?i)\b(?:postgres(?:ql)?|https?)://[^\s/:]+:[^\s/@]+@")
USER_HOME = re.compile(r"(?:/Users/|[A-Za-z]:[\\/]Users[\\/])([A-Za-z0-9_.-]+)")
# Exact, reviewed synthetic canaries in the existing public tests. This is not
# a blanket test-directory exemption: a different value still fails the gate.
TEST_VALUES = frozenset({
    "do-not-expose-manifest-key", "private-probe-key", "synthetic-do-not-export",
    "synthetic-not-public", "synthetic-test-key", "isolated-unknown-qa-test",
    "local-review-invite", "not-a-configured-code", "private-fixture-invite",
    "qa3-private-invite", "synthetic-b-test-invite", "synthetic-diagnostic-invite",
    "synthetic-turn-owner-a", "synthetic-turn-owner-b", "test-invite-code-0123456789",
    "synthetic-container-startup-invite", "synthetic-facts-a", "synthetic-facts-b",
    "sk-test-key-0123456789", "test-nebius-secret", "sk-test-not-a-real-key",
    "sk-test-platform", "unrelated-provider-secret", "unrelated-secret",
    "must-not-import-this-either", "must-not-import-this-secret", "only-for-model-phase",
    "hosted-test-invite-000001", "hosted-test-invite-000002", "independent-invite-code-0001",
    "investigation-owner-invite", "invite-code-for-tests-0001", "product-approval-invite-0001",
    "restore-test-invite-0001", "account-test-invite-0001", "gateway-test-owner",
    "synthetic-browser-invite", "synthetic-browser-invite-00000001",
    "synthetic-header-invite-00000001", "synthetic-import-invite-0001",
    "synthetic-startup-invite", "local-default-strategy-owner", "synthetic-cold-start-invite",
    "private-nebius-secret", "synthetic-key-canary", "unused-local-test",
    "fixture-secret-value", "private-fixture-secret", "sk-platform-secret-0123456789",
    "PRIVATE_CANARY", "SYNTHETIC-NEVER-LOG-THIS", "Synthetic-pass-1",
    "a-different-session-secret-000000", "account-test-session-secret-0123456789",
    "actual-secret-1234", "another-secret", "attachment-session-secret-0123456789",
    "bootstrap-session-secret-0123456789abcdef", "correct-horse-1", "deepseek-key",
    "fixture-secret-must-never-be-logged", "fixture-secret-never-persist",
    "gateway-only-routing-secret-", "gateway-test-secret-01234567890123456789",
    "hosted-test-secret-0123456789abcdef", "https://user:pass@", "https://user:password-value@",
    "https://user:password@", "https://user:secret@", "image-api-session-secret-0123456789",
    "independent-test-secret-0123456789abcdef", "interaction-session-secret-0123456789",
    "isolated-unknown-qa-test-secret-0001", "legacy-invite", "legacy-private",
    "local-default-strategy-secret-0123456789", "nebius-only-test-credential-123456",
    "new-password-1", "not-a-real-key", "not-configured", "not-exported",
    "odoo20-api-session-secret-0123456789", "other-pass-01", "other-secret",
    "planner-pass-1", "planner-pass-2", "postgresql://user:pw@", "preserved-secret-",
    "private-child-stdout", "private-fixture", "private-secret", "private-session",
    "product-approval-flow-secret-0123456789", "qdrant-secret",
    "restore-test-session-secret-0123456789", "secret-marker", "semantic-secret",
    "structured-session-secret-0123456789", "synthetic-admin-123",
    "synthetic-browser-secret-1234567890", "synthetic-browser-session-secret-00000001",
    "synthetic-canary", "synthetic-cold-start-secret-0123456789",
    "synthetic-container-startup-secret-0123456789", "synthetic-diagnostic-session-secret-01",
    "synthetic-facts-secret-01234567890123456789", "synthetic-header-test-secret-00000001",
    "synthetic-member-123", "synthetic-other-123", "synthetic-replacement-123",
    "synthetic-secret-canary", "synthetic-session-secret-0123456789",
    "synthetic-startup-secret-01234567890123456789", "synthetic-test-value",
    "synthetic-turn-secret-01234567890123456789", "test-b-scenario-secret-at-least-32-characters",
    "test-evidence-secret-01234567890123456789", "test-investigation-session-secret-0123456789",
    "test-product-session-secret-0123456789", "test-session-secret-0123456789abcdef",
    "untouched-qa1", "untouched-qa2", "unused-fixture", "wrong-pass-000",
    "sk-docmind-synthetic-12345", "chat-secret-123456", "sk-parser-secret-1234",
    "embed-secret-1234", "new-chat-secret-1234", "new-nebius-key", "synthetic-password",
    "synthetic-session-secret-32-characters", "synthetic-invite-code-1234",
    "postgresql://user:do-not-print@",
    "wrong-service-format", "test-local-qdrant",
    "ci-model-fixture-key", "sk-ci-parser-fixture-key", "ci-embedding-fixture-key",
    "synthetic-admin-password-long-enough",
    "separate-search-secret", "first-search-secret", "second-search-secret", "nebius-chat-secret",
    "search-key-original", "search-key-fixed", "replacement-chat-secret", "gateway-own-secret",
    "synthetic-provider-credential", "synthetic-credential",
    "user-chat-key", "official-search-key", "unrelated-key", "official-key",
    "synthetic-model-invite-001", "synthetic-session-secret-000000000001",
    "synthetic-installed-chat-key", "unselected-key", "synthetic-platform-key", "synthetic-user-key",
})
RUNTIME_CANARIES = {
    "src/forgeflow/judge_gateway.py": {"unused-trust-validation-"},
    "src/forgeflow/permissions.py": {"read-only-not-for-login"},
    "scripts/accept_local_stack.py": {
        "ci-model-fixture-key", "sk-ci-parser-fixture-key", "ci-embedding-fixture-key",
    },
    "scripts/accept_local_odoo.py": {
        "ci-model-fixture-key", "sk-ci-parser-fixture-key", "ci-embedding-fixture-key",
    },
}


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    code: str


def scan_text(text: str, path: str) -> list[Finding]:
    findings = set()
    test = path.startswith(("tests/", "web/e2e/"))
    reviewed_test_constants = test or path == "scripts/check_distribution.py"
    for pattern in (PROVIDER_TOKEN, PRIVATE_KEY, QUOTED_SECRET, ENV_SECRET, URL_CREDENTIAL):
        if pattern is ENV_SECRET and path.endswith((".py", ".ts", ".tsx", ".js", ".mjs")):
            continue  # Unquoted source expressions are variables, not credential literals.
        for match in pattern.finditer(text):
            value = match.groupdict().get("value") or match.group()
            if reviewed_test_constants and value in TEST_VALUES:
                continue
            if value in RUNTIME_CANARIES.get(path, set()):
                continue
            if pattern is QUOTED_SECRET and re.fullmatch(r"(?:FORGEFLOW|NEBIUS|DEEPSEEK)_[A-Z0-9_]+", value):
                continue  # Environment-variable metadata, never an actual key.
            if pattern is URL_CREDENTIAL and any(char in value for char in "{}$"):
                continue
            findings.add(Finding(path, text.count("\n", 0, match.start()) + 1, "credential"))
    for match in USER_HOME.finditer(text):
        if test and match.group(1) in {"example", "private"}:
            continue
        findings.add(Finding(path, text.count("\n", 0, match.start()) + 1, "personal-path"))
    return sorted(findings)


def _private_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    return (bool(PRIVATE_PARTS.intersection(path.parts)) or path.parts[0] in PRIVATE_ROOTS
            or relative.startswith("docs/evidence/")
            or (path.name.startswith(".env") and path.name != ".env.example")
            or path.suffix.lower() in PRIVATE_SUFFIXES
            or path.name in {".DS_Store", "Thumbs.db"})


def _docx(path: Path, relative: str) -> list[Finding]:
    findings = []
    try:
        with zipfile.ZipFile(path) as archive:
            if sum(item.file_size for item in archive.infolist()) > 5_000_000:
                return [Finding(relative, 1, "template-too-large")]
            for name in archive.namelist():
                if not name.endswith((".xml", ".rels")):
                    findings.append(Finding(relative, 1, "template-unreviewed-attachment"))
                    continue
                text = archive.read(name).decode("utf-8")
                findings.extend(scan_text(text, relative))
                tree = ElementTree.fromstring(text)
                for element in tree.iter():
                    local = element.tag.rsplit("}", 1)[-1]
                    if (element.attrib.get("TargetMode") == "External"
                            or local in {"ins", "del", "object", "comment"}
                            or (local in {"creator", "lastModifiedBy", "Company", "Manager"}
                                and (element.text or "").strip())):
                        findings.append(Finding(relative, 1, "template-private-metadata"))
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, ElementTree.ParseError):
        findings.append(Finding(relative, 1, "template-invalid"))
    return findings


def _module(root: Path, name: str) -> Path | None:
    stem = root / "src" / Path(*name.split("."))
    for path in (stem.with_suffix(".py"), stem / "__init__.py"):
        if path.is_file() and not path.is_symlink():
            return path
    return None


def check_imports(root: Path) -> list[Finding]:
    findings = []
    files = sorted((root / "src/forgeflow").rglob("*.py"))
    files += [root / name for name in REQUIRED if name.startswith("scripts/")]
    for path in files:
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (ValueError, SyntaxError):
            findings.append(Finding(rel, 1, "python-invalid"))
            continue
        parts = path.relative_to(root / "src").with_suffix("").parts if rel.startswith("src/") else ()
        package = parts[:-1]
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package[:len(package) - node.level + 1]
                    name = ".".join((*base, *(() if not node.module else node.module.split("."))))
                else:
                    name = node.module or ""
                names = [name]
                if name.startswith("forgeflow") and (parent := _module(root, name)):
                    # `from package import module` also needs its child file,
                    # unless the parent declares that exported name itself.
                    parent_tree = ast.parse(parent.read_text(encoding="utf-8"))
                    declared = {n.id for n in ast.walk(parent_tree) if isinstance(n, ast.Name)
                                and isinstance(n.ctx, ast.Store)}
                    declared.update(n.name for n in ast.walk(parent_tree)
                                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
                    declared.update(a.asname or a.name.split(".")[0] for n in ast.walk(parent_tree)
                                    if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names)
                    names += [f"{name}.{a.name}" for a in node.names
                              if a.name != "*" and a.name not in declared]
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "import_module" and node.args
                  and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                names = [node.args[0].value]
            for name in names:
                if (name == "forgeflow" or name.startswith("forgeflow.")) and _module(root, name) is None:
                    findings.append(Finding(rel, node.lineno, "missing-python-dependency"))
    return findings


def check_reference_pack(root: Path) -> list[Finding]:
    findings = []
    try:
        folder = root / PACK
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        entries = manifest["documents"]
        if {entry["source_id"] for entry in entries} != {f"K{i:02}" for i in range(1, 13)}:
            raise ValueError("incomplete pack")
        for entry in entries:
            filename = entry["filename"]
            if filename != entry["source_id"] + ".md":
                raise ValueError("invalid path")
            path = folder / filename
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                findings.append(Finding(f"{PACK}/{filename}", 1, "reference-hash-mismatch"))
        raw = (folder / "comparisons.json").read_bytes()
        tree = ast.parse((root / "src/forgeflow/knowledge/reference_facts.py").read_text(encoding="utf-8"))
        pins = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "REVIEWED_COMPARISONS_SHA256"
                        for t in node.targets) and isinstance(node.value, ast.Constant)]
        if pins != [hashlib.sha256(raw).hexdigest()]:
            findings.append(Finding(f"{PACK}/comparisons.json", 1, "reference-review-pin-mismatch"))
    except (OSError, ValueError, KeyError, TypeError, SyntaxError):
        findings.append(Finding(PACK, 1, "reference-pack-incomplete"))
    return findings


def check_build_inputs(root: Path) -> list[Finding]:
    findings = []
    for rel in ("web", "deploy/vps/dsh"):
        try:
            package = json.loads((root / rel / "package.json").read_text(encoding="utf-8"))
            lock = json.loads((root / rel / "package-lock.json").read_text(encoding="utf-8"))
            for key in ("dependencies", "devDependencies"):
                if package.get(key, {}) != lock["packages"][""].get(key, {}):
                    findings.append(Finding(f"{rel}/package-lock.json", 1, "npm-lock-mismatch"))
        except (OSError, ValueError, KeyError, TypeError):
            findings.append(Finding(rel, 1, "npm-manifest-incomplete"))
    for path in sorted((root / "deploy").rglob("Dockerfile*")):
        text = re.sub(r"\\\n", " ", path.read_text(encoding="utf-8"))
        for number, line in enumerate(text.splitlines(), 1):
            if not re.match(r"\s*(COPY|ADD)\s", line, re.I) or "--from=" in line:
                continue
            try:
                words = shlex.split(line)[1:]
                words = [word for word in words if not word.startswith("--")]
                if words and words[0].startswith("["):
                    words = json.loads(line.split(None, 1)[1])
                for source in words[:-1]:
                    if "$" in source or source.startswith(("https://", "http://")):
                        findings.append(Finding(path.relative_to(root).as_posix(), number, "unresolved-build-input"))
                    elif source.startswith("/") or ".." in PurePosixPath(source).parts or not list(root.glob(source)):
                        findings.append(Finding(path.relative_to(root).as_posix(), number, "missing-build-input"))
            except (ValueError, TypeError):
                findings.append(Finding(path.relative_to(root).as_posix(), number, "invalid-build-input"))
    return findings


def check_web_and_addon(root: Path) -> list[Finding]:
    findings = []
    imports = re.compile(
        r"\b(?:from\s*|import\s*\(?\s*)[\"'](?P<path>\.[^\"']+)[\"']")
    for path in sorted((root / "web/src").rglob("*")):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".css"}:
            continue
        text = path.read_text(encoding="utf-8")
        for match in imports.finditer(text):
            target = path.parent / match["path"].split("?", 1)[0]
            candidates = [target] + [Path(str(target) + ext) for ext in (".ts", ".tsx", ".js", ".json")]
            candidates += [target / ("index" + ext) for ext in (".ts", ".tsx", ".js")]
            if not any(candidate.is_file() and not candidate.is_symlink() for candidate in candidates):
                findings.append(Finding(path.relative_to(root).as_posix(),
                                        text.count("\n", 0, match.start()) + 1,
                                        "missing-web-dependency"))
    addon = root / "odoo-addons/forgeflow_general"
    for path in sorted(addon.rglob("__init__.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            names = [node.module] if node.module else [alias.name for alias in node.names]
            for name in names:
                target = path.parent / Path(*name.split("."))
                if not (target.with_suffix(".py").is_file() or (target / "__init__.py").is_file()):
                    findings.append(Finding(path.relative_to(root).as_posix(), node.lineno,
                                            "missing-addon-dependency"))
    try:
        manifest = ast.literal_eval((addon / "__manifest__.py").read_text(encoding="utf-8"))
        for relative in manifest.get("data", []):
            if (PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
                    or not (addon / relative).is_file()):
                findings.append(Finding("odoo-addons/forgeflow_general/__manifest__.py", 1,
                                        "missing-addon-data"))
    except (OSError, ValueError, SyntaxError, TypeError):
        findings.append(Finding("odoo-addons/forgeflow_general/__manifest__.py", 1, "invalid-addon-manifest"))
    return findings


def check_distribution(root: Path, *, dependencies: bool = True) -> tuple[list[Finding], dict[str, str]]:
    findings, hashes = [], {}
    for current, folders, filenames in os.walk(root, followlinks=False):
        folder = Path(current)
        for name in list(folders):
            path = folder / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                findings.append(Finding(rel, 1, "symlink"))
                folders.remove(name)
            elif name in IGNORE:
                folders.remove(name)
            elif _private_path(rel):
                findings.append(Finding(rel, 1, "private-state"))
                folders.remove(name)
        for name in sorted(filenames):
            path = folder / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                findings.append(Finding(rel, 1, "non-regular-file"))
                continue
            if _private_path(rel):
                findings.append(Finding(rel, 1, "private-state"))
                continue
            raw = path.read_bytes()
            hashes[rel] = hashlib.sha256(raw).hexdigest()
            if rel == "deploy/pandoc-reference.docx":
                findings.extend(_docx(path, rel))
                continue
            try:
                if b"\0" in raw:
                    raise UnicodeError()
                text = raw.decode("utf-8-sig")
            except UnicodeError:
                if path.suffix.lower() not in ASSET_SUFFIXES:
                    findings.append(Finding(rel, 1, "unreviewed-binary"))
                continue
            findings.extend(scan_text(text, rel))
    if dependencies:
        for rel in (*REQUIRED, f"{PACK}/README.md", f"{PACK}/ODOO_LICENSE.txt"):
            if not (root / rel).is_file() or (root / rel).is_symlink():
                findings.append(Finding(rel, 1, "missing-required-file"))
        findings.extend(check_imports(root))
        findings.extend(check_reference_pack(root))
        findings.extend(check_build_inputs(root))
        findings.extend(check_web_and_addon(root))
    return sorted(set(findings)), dict(sorted(hashes.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--manifest", type=Path, help="Write file hashes outside the inspected tree")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir() or args.root.is_symlink():
        parser.error("root must be a regular directory")
    if args.manifest and args.manifest.resolve().is_relative_to(root):
        parser.error("manifest must be outside the inspected tree")
    findings, hashes = check_distribution(root)
    for item in findings:
        print(f"{item.path}:{item.line}: {item.code}")
    result = {"ok": not findings, "files": len(hashes), "findings": len(findings)}
    if args.manifest:
        args.manifest.write_text(json.dumps({**result, "sha256": hashes}, indent=2) + "\n")
    print(json.dumps(result))
    return bool(findings)


if __name__ == "__main__":
    sys.exit(main())
