"""Tests for the public-repository export and content-check tools.

Both scripts are loaded by path so these tests exercise the same files a
maintainer runs. Synthetic trees keep every artifact inside ``tmp_path`` and
build policy-sensitive strings from fragments so this test file is itself safe
to publish.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts" / "check_public_repo.py"
EXPORTER_PATH = ROOT / "scripts" / "export_public_repo.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_module(CHECKER_PATH, "public_repo_checker_under_test")
exporter = _load_module(EXPORTER_PATH, "public_repo_exporter_under_test")

CJK = "\u4e2d"
CHAT_TERM = "Chat" + "GPT"
VENDOR_TERM = "Co" + "dex"
DEEP_TERM = "Deep" + "Seek"
USER_HOME = "/Us" + "ers/alice/private"
WINDOWS_HOME = "C:" + "\\Us" + "ers\\alice\\private"
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"
FAKE_API_KEY = "sk-" + "a" * 32
FAKE_INVITE = "invite" + "-code-" + "0123456789abcdef"
SUPPLEMENTAL = "\u8865" + "\u5145" + "\u6750" + "\u6599"
LICENSE_TEXT = "Apache License\nVersion 2.0, January 2004\n"
API_LINE = "api_key = " + repr(FAKE_API_KEY)
INVITE_LINE = "invite_code = " + repr(FAKE_INVITE)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, data: object) -> None:
    _write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _clean_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _write(root / "LICENSE", LICENSE_TEXT)
    _write(root / "README.md", "# Clean\n\nEnglish only.\n")
    _write(root / "src" / "app.py", "VALUE = 1\n")
    return root


def _source_tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _write(source / "README.md", "# Public ForgeFlow\n\nEnglish only.\n")
    _write(source / "LICENSE", LICENSE_TEXT)
    _write(source / "NOTICE", "ForgeFlow\n\nThird-party notices.\n")
    _write(source / "pyproject.toml", "[project]\nname = 'forgeflow'\n")
    _write(source / "uv.lock", "version = 1\n")
    _write(source / "src" / "pkg" / "__init__.py", "VALUE = 1\n")
    _write(source / "src" / "pkg" / "keep.py", "def keep():\n    return 1\n")
    _write(source / "tests" / "test_ok.py", "def test_ok():\n    assert True\n")
    _write(source / "web" / "package.json", '{"name": "forgeflow-web"}\n')
    _write(source / "web" / "package-lock.json", '{"lockfileVersion": 3}\n')
    _write(source / "web" / "tsconfig.json", "{}\n")
    _write(source / "web" / "vite.config.ts", "export default {};\n")
    _write(source / "web" / "index.html", "<!doctype html><title>ForgeFlow</title>\n")
    _write(source / "web" / "src" / "app.ts", "export const value = 1;\n")
    _write(source / "web" / "src" / "private.ts", "export const privateOnly = 1;\n")
    _write(source / "web" / "e2e" / "smoke.mjs", "console.log('ok');\n")
    _write(source / "web" / "README.md", "private notes " + CJK + "\n")
    _write(source / "web-public" / "README.md", "# Public web\n\nEnglish only.\n")
    _write(source / "web-public" / "index.html", "<!doctype html><title>ForgeFlow</title>\n")
    _write(source / "web-public" / "package.json", '{"name": "forgeflow-web"}\n')
    _write(source / "web-public" / "package-lock.json", '{"lockfileVersion": 3}\n')
    _write(source / "web-public" / "tsconfig.json", "{}\n")
    _write(source / "web-public" / "vite.config.ts", "export default {};\n")
    _write(source / "web-public" / ".gitignore", "node_modules/\ndist/\n")
    _write(source / "web-public" / "src" / "main.tsx", "export const value = 1;\n")
    _write(source / "web-public" / "e2e" / "smoke.mjs", "console.log('ok');\n")
    _write(source / "contracts" / "demo.json", '{"ok": true}\n')
    _write(source / "contracts" / "README.md", "private notes " + CJK + "\n")
    _write(source / "scripts" / "export_public_repo.py", "#!/usr/bin/env python3\n")
    _write(source / "scripts" / "check_public_repo.py", "#!/usr/bin/env python3\n")

    private = {
        ".git/config": "repositoryformatversion = 0\n",
        ".env": "LOCAL_ONLY=1\n",
        "runs/run-1/summary.json": "{}\n",
        "vendor/upstream.c": "int value;\n",
        "build-inputs/input.bin": "binary input\n",
        ".runtime/state.json": "{}\n",
        "node_modules/pkg/index.js": "module.exports = 1;\n",
        "src/forgeflow/api/static/app/index.html": CHAT_TERM + CJK,
        "src/forgeflow/api/static/app/assets/clean.css": "body { color: red; }\n",
        "src/forgeflow/api/static/app.js": "console.log('built');\n",
        "web/node_modules/pkg/index.js": "module.exports = 1;\n",
        "web/dist/app.js": "console.log(1);\n",
        "docs/private.md": CHAT_TERM + CJK,
        f"{SUPPLEMENTAL}/notes.md": CHAT_TERM + CJK,
    }
    for relative, text in private.items():
        _write(source / relative, text)
    return source


def test_checker_accepts_clean_tree_and_ignores_binary(tmp_path, capsys):
    root = _clean_tree(tmp_path)
    (root / "assets").mkdir()
    (root / "assets" / "blob.bin").write_bytes(
        b"\x00" + CJK.encode("utf-8") + CHAT_TERM.encode("utf-8")
    )

    assert checker.main([str(root)]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "binary files skipped" in captured.err


def test_checker_reports_cjk_terms_paths_keys_and_credentials(tmp_path, capsys):
    root = _clean_tree(tmp_path)
    _write(
        root / "README.md",
        "\n".join(
            [
                "# Clean",
                CJK,
                CHAT_TERM,
                USER_HOME,
                WINDOWS_HOME,
                PRIVATE_KEY,
                API_LINE,
                INVITE_LINE,
            ]
        )
        + "\n",
    )
    _write(root / f"{CJK}.md", "clean\n")
    (root / f"{CJK}_dir").mkdir()

    assert checker.main([str(root)]) == 1
    captured = capsys.readouterr().out
    for line in (2, 3, 4, 5, 6, 7, 8):
        assert f"README.md:{line}:" in captured
    assert f"{CJK}.md:1:" in captured
    assert f"{CJK}_dir:1:" in captured


def test_checker_requires_a_root_license(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root / "README.md", "# Clean\n")
    assert checker.main([str(root)]) == 1
    assert "LICENSE:1:" in capsys.readouterr().out


def test_tooling_sources_pass_their_own_policy():
    for path in (CHECKER_PATH, EXPORTER_PATH, Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert checker.scan_text(text, path=path.name) == []


def test_exporter_copies_public_subset_and_excludes_private_paths(tmp_path):
    source = _source_tree(tmp_path)
    out = tmp_path / "out"

    result = exporter.export_public_repo(source, out)

    expected = (
        "README.md",
        "LICENSE",
        "NOTICE",
        "pyproject.toml",
        "uv.lock",
        "src/pkg/__init__.py",
        "src/pkg/keep.py",
        "tests/test_ok.py",
        "contracts/demo.json",
        "scripts/export_public_repo.py",
        "scripts/check_public_repo.py",
        "web/README.md",
        "web/index.html",
        "web/package.json",
        "web/package-lock.json",
        "web/tsconfig.json",
        "web/vite.config.ts",
        "web/.gitignore",
        "web/src/main.tsx",
        "web/e2e/smoke.mjs",
    )
    for relative in expected:
        assert (out / relative).is_file(), relative
    assert (out / "README.md").read_text(encoding="utf-8").startswith("# Public ForgeFlow")
    assert (out / "web" / "README.md").read_text(encoding="utf-8").startswith("# Public web")
    assert not (out / "web" / "src" / "private.ts").exists()
    assert sorted(path.name for path in (out / "scripts").iterdir()) == [
        "check_public_repo.py",
        "export_public_repo.py",
    ]

    private = (
        ".git",
        ".env",
        "runs",
        "vendor",
        "build-inputs",
        ".runtime",
        "node_modules",
        "web/node_modules",
        "web/dist",
        "web/src/private.ts",
        "docs",
        SUPPLEMENTAL,
        "src/forgeflow/api/static/app",
        "src/forgeflow/api/static/app.js",
    )
    for relative in private:
        assert not (out / relative).exists(), relative

    assert result.copied_files > 0
    assert checker.main([str(out)]) == 0


def test_exporter_rejects_unsafe_web_public_source(tmp_path):
    source = _source_tree(tmp_path)
    _write(source / "web-public" / "src" / "leak.ts", "// " + CJK + "\n")

    with pytest.raises(exporter.ExportError, match="web/src/leak.ts"):
        exporter.export_public_repo(source, tmp_path / "out")


def test_exporter_rejects_blocked_terms_in_web_public(tmp_path):
    source = _source_tree(tmp_path)
    _write(source / "web-public" / "src" / "leak.ts", "// " + VENDOR_TERM + "\n")

    with pytest.raises(exporter.ExportError, match="forbidden-term"):
        exporter.export_public_repo(source, tmp_path / "out")


def test_exporter_requires_core_web_public_files(tmp_path):
    source = _source_tree(tmp_path)
    (source / "web-public" / "package-lock.json").unlink()

    with pytest.raises(
        exporter.ExportError,
        match="web-public is missing required file: package-lock.json",
    ):
        exporter.export_public_repo(source, tmp_path / "out")


def test_exporter_does_not_fall_back_to_private_web(tmp_path):
    source = _source_tree(tmp_path)
    shutil.rmtree(source / "web-public")
    out = tmp_path / "out"

    result = exporter.export_public_repo(source, out)

    assert not (out / "web").exists()
    assert ("web-public", "missing directory") in result.skipped
    assert checker.main([str(out)]) == 0


def test_cli_check_web_runs_the_local_build_check(tmp_path, monkeypatch):
    source = _source_tree(tmp_path)
    out = tmp_path / "out"
    calls: list[Path] = []
    monkeypatch.setattr(exporter, "_check_web_build", calls.append)

    assert (
        exporter.main(
            [
                "--source",
                str(source),
                "--out",
                str(out),
                "--no-demo-runs",
                "--check-web",
            ]
        )
        == 0
    )
    assert calls == [source]
    assert (out / "web" / "index.html").is_file()


def test_exporter_sanitizes_demo_run_locations_and_keeps_verifier_results(tmp_path):
    source = _source_tree(tmp_path)
    run = source / "demo-runs" / "run-1"
    trial = run / "harbor" / "job" / "trial"
    _write_json(
        run / "manifest.json",
        {
            "run_id": "run-1",
            "workspace_root": str(source),
            "trials_dir": str(source / "runs" / "run-1" / "harbor" / "job"),
            "trial_uri": trial.as_uri(),
            "command": [
                str(source / ".venv" / "bin" / "harbor"),
                "-p",
                str(source / "runs" / "run-1" / "task" / "task-1"),
            ],
            "nested": {"workspace_root": str(source), "value": "kept"},
            "bare_home": "/Us" + "ers/",
            "bare_windows": "C:" + "\\Us" + "ers\\",
            "forward_windows": "c:/Us" + "ers/alice/private",
        },
    )
    _write_json(
        run / "summary.json",
        {
            "run_id": "run-1",
            "score": 100.0,
            "strict_success": True,
            "verifier": {"all_applicable_rules_pass": True},
        },
    )
    _write_json(
        trial / "result.json",
        {
            "trial_uri": trial.as_uri(),
            "workspace_root": str(source),
            "task": {"path": str(source / "runs" / "run-1" / "task" / "task-1")},
            "score": 100.0,
        },
    )
    _write_json(trial / "verifier" / "reward.json", {"overall_score": 100.0})
    _write(trial / "verifier" / "reward.txt", "100.0\n")
    _write_json(trial / "agent" / "recovery" / "active-plan.json", {"plan_id": "p1"})

    out = tmp_path / "out"
    result = exporter.export_public_repo(source, out)

    manifest = json.loads((out / "demo-runs" / "run-1" / "manifest.json").read_text())
    assert "workspace_root" not in manifest
    assert "trials_dir" not in manifest
    assert "trial_uri" not in manifest
    assert manifest["command"][0] == ".venv/bin/harbor"
    assert manifest["command"][2] == "runs/run-1/task/task-1"
    assert manifest["nested"] == {"value": "kept"}
    assert manifest["bare_home"] == "<home>/"
    assert manifest["bare_windows"] == "<home>\\"
    assert manifest["forward_windows"] == "<home>/private"

    result_document = json.loads(
        (out / "demo-runs" / "run-1" / "harbor" / "job" / "trial" / "result.json").read_text()
    )
    assert "workspace_root" not in result_document
    assert "trial_uri" not in result_document
    assert result_document["task"]["path"] == "runs/run-1/task/task-1"
    assert result_document["score"] == 100.0
    summary = json.loads((out / "demo-runs" / "run-1" / "summary.json").read_text())
    assert summary["verifier"] == {"all_applicable_rules_pass": True}

    published = "\n".join(
        path.read_text(encoding="utf-8")
        for path in out.rglob("*")
        if path.suffix in {".json", ".jsonl", ".md", ".txt"}
    )
    assert str(source) not in published
    assert USER_HOME not in published
    assert WINDOWS_HOME not in published
    assert CHAT_TERM not in published
    assert CJK not in published
    assert (out / "demo-runs" / "README.md").is_file()
    assert result.demo_files == 6
    assert checker.main([str(out)]) == 0


def test_exporter_rejects_unsanitizable_demo_json(tmp_path):
    source = _source_tree(tmp_path)
    _write_json(source / "demo-runs" / "run-1" / "summary.json", {"note": CHAT_TERM})
    with pytest.raises(exporter.SanitizationError, match="summary.json"):
        exporter.export_public_repo(source, tmp_path / "out")


def test_cli_can_omit_demo_runs(tmp_path, capsys):
    source = _source_tree(tmp_path)
    _write_json(source / "demo-runs" / "run-1" / "summary.json", {"score": 100.0})
    out = tmp_path / "out"

    assert (
        exporter.main(
            ["--source", str(source), "--out", str(out), "--no-demo-runs"]
        )
        == 0
    )
    assert not (out / "demo-runs").exists()
    assert "exported" in capsys.readouterr().out
    assert checker.main([str(out)]) == 0


def test_forbidden_terms_are_case_insensitive(tmp_path):
    root = _clean_tree(tmp_path)
    _write(root / "notes.txt", VENDOR_TERM.lower() + "\n" + DEEP_TERM.lower() + "\n")

    assert checker.main([str(root)]) == 1


def test_third_party_license_keeps_attribution_but_other_files_do_not():
    assert checker.scan_text("Copyright (c) " + VENDOR_TERM, path="web/src/dsh/LICENSE") == []
    assert checker.scan_text("Copyright (c) " + VENDOR_TERM, path="web/src/other/LICENSE")


def test_dependency_manifests_may_name_packages():
    text = '"@' + ''.join(["deep", "seek-ai"]) + '"'
    for path in ("web/package.json", "web/package-lock.json", "pyproject.toml", "uv.lock"):
        assert checker.scan_text(text, path=path) == []
    assert checker.scan_text(text, path="web/src/leak.ts")
    assert checker.scan_text(text, path="docs/pyproject.toml")


@pytest.mark.parametrize(
    "path",
    ("pyproject.toml", "uv.lock", "src/forgeflow/api/dsh_sdk.py",
     "src/forgeflow/api/dsh_permissions.py"),
)
def test_dependency_exceptions_never_allow_sensitive_content(path):
    text = "\n".join((CJK, USER_HOME, PRIVATE_KEY, API_LINE, INVITE_LINE))
    kinds = {item.kind for item in checker.scan_text(text, path=path)}
    assert kinds == {"cjk", "user-path", "private-key", "credential"}


def test_runtime_identifiers_are_exact_tokens_in_exact_files():
    module = DEEP_TERM.lower() + "_harness"
    symbol = DEEP_TERM + "Harness"
    package = "@" + DEEP_TERM.lower() + "-ai/dsh-mcp-client"
    sdk_path = "src/forgeflow/api/dsh_sdk.py"
    permissions_path = "src/forgeflow/api/dsh_permissions.py"
    sdk = f"import {module}\nfrom {module} import {symbol}\nclient = {symbol}()\n"
    permissions = f"PACKAGE = {package!r}\n"
    assert checker.scan_text(sdk, path=sdk_path) == []
    assert checker.scan_text(permissions, path=permissions_path) == []
    for text, path in (
        (sdk, "src/other.py"), (permissions, "src/other.py"),
        (f"# {module}\n", sdk_path), (f"NAME = {module!r}\n", sdk_path),
        (f"import {module}_other\n", sdk_path),
        (f"PACKAGE = {package + '-other'!r}\n", permissions_path),
        (f"PACKAGE = {package + ' ' + FAKE_API_KEY!r}\n", permissions_path),
    ):
        assert "forbidden-term" in {item.kind for item in checker.scan_text(text, path=path)}
    findings = checker.scan_text(permissions + API_LINE, path=permissions_path)
    assert {item.kind for item in findings} == {"credential"}


def test_upload_module_identifier_preserved_without_widening_secret_checks():
    module_id = "session-log-" + DEEP_TERM.lower()
    path = "src/forgeflow/api/dsh_permissions.py"
    text = f"DISABLED_UPLOAD_MODULES = ({module_id!r},)\n"
    assert checker.scan_text(text, path=path) == []
    assert exporter._sanitize_python_source(text, path=path) == text
    for candidate, location in (
        (text, "src/other.py"),
        (f"VALUE = {module_id + '-other'!r}\n", path),
        (f"VALUE = {module_id + ' ' + FAKE_API_KEY!r}\n", path),
    ):
        assert "forbidden-term" in {v.kind for v in checker.scan_text(candidate, path=location)}
    assert "credential" in {v.kind for v in checker.scan_text(text + API_LINE, path=path)}


def test_exporter_preserves_dependency_metadata_and_runtime_identifiers(tmp_path):
    source = _source_tree(tmp_path)
    package_name = DEEP_TERM.lower() + "-harness-sdk"
    metadata = f'[project]\nname = "forgeflow"\ndependencies = ["{package_name}==1"]\n'
    lock = f'version = 1\n[[package]]\nname = "{package_name}"\nversion = "1"\n'
    _write(source / "pyproject.toml", metadata)
    _write(source / "uv.lock", lock)
    module = DEEP_TERM.lower() + "_harness"
    symbol = DEEP_TERM + "Harness"
    package = "@" + DEEP_TERM.lower() + "-ai/dsh-mcp-client"
    sdk = (
        f'"""{DEEP_TERM} Harness integration. {CJK}"""\n'
        f"from {module} import {symbol}\n"
        f"def create():\n    return {symbol}()\n"
    )
    permissions = f'"""{DEEP_TERM} integration. {CJK}"""\nPACKAGE = {package!r}\n'
    sdk_path = "src/forgeflow/api/dsh_sdk.py"
    permissions_path = "src/forgeflow/api/dsh_permissions.py"
    _write(source / sdk_path, sdk)
    _write(source / permissions_path, permissions)
    out = tmp_path / "out"
    exporter.export_public_repo(source, out, sanitize_source=True)
    assert (out / "pyproject.toml").read_text() == metadata
    assert (out / "uv.lock").read_text() == lock
    exported_sdk = (out / sdk_path).read_text()
    assert f"from {module} import {symbol}" in exported_sdk
    assert f"return {symbol}()" in exported_sdk
    namespace = {}
    exec((out / permissions_path).read_text(), namespace)
    assert namespace["PACKAGE"] == package
    assert checker.scan_tree(out).violations == ()


@pytest.mark.parametrize("relative", ("pyproject.toml", "uv.lock"))
def test_exporter_requires_dependency_metadata(tmp_path, relative):
    source = _source_tree(tmp_path)
    (source / relative).unlink()
    with pytest.raises(exporter.ExportError, match="required file is missing"):
        exporter.export_public_repo(source, tmp_path / "out")


@pytest.mark.parametrize("relative", ("pyproject.toml", "uv.lock"))
def test_exporter_rejects_sensitive_dependency_metadata(tmp_path, relative):
    source = _source_tree(tmp_path)
    _write(source / relative, API_LINE)
    with pytest.raises(exporter.ExportError, match="credential"):
        exporter.export_public_repo(source, tmp_path / "out")
