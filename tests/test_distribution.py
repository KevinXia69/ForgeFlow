"""Release-tree checks run without provider, ERP, Docker or private state."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "forgeflow_distribution_checker", ROOT / "scripts/check_distribution.py")
checker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checker
SPEC.loader.exec_module(checker)


def write(root, relative, text="safe content\n"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def codes(root):
    return {item.code for item in checker.check_distribution(root, dependencies=False)[0]}


@pytest.mark.parametrize("name", [
    ".env", "deploy/.env.local", ".runtime/config.json", "secrets/password",
    "data/accounts.sqlite3", "control.dump", "backups/state.json",
    "attachments/upload.pdf", "docs/evidence/raw.json", "runs/result.json",
])
def test_private_state_is_rejected_even_with_innocuous_contents(tmp_path, name):
    write(tmp_path, name)
    assert "private-state" in codes(tmp_path)


def test_runtime_language_brand_and_external_identifiers_are_preserved(tmp_path):
    source = 'API_KEY_ENV = "DEEPSEEK_API_KEY"\nMODEL = "deepseek-flash"\nLABEL = "线圈参数"\n'
    path = write(tmp_path, "src/example.py", source)
    findings, hashes = checker.check_distribution(tmp_path, dependencies=False)
    assert not findings
    assert hashes.keys() == {"src/example.py"}
    assert path.read_text() == source


def test_secret_findings_do_not_disclose_the_matched_value(tmp_path):
    secret = "gh" + "p_" + "X7a4Z0b9N2c8R6v5T1m3L0q9"
    write(tmp_path, "settings.json", json.dumps({"provider": secret}))
    findings, _ = checker.check_distribution(tmp_path, dependencies=False)
    assert {x.code for x in findings} == {"credential"}
    assert secret not in repr(findings)


def test_test_directory_does_not_exempt_new_credentials():
    secret = "sk-" + "1AbCdEfGhIjKlMnOpQrStUvWxYz9"
    assert checker.scan_text(f'API_KEY = "{secret}"', "tests/test_example.py")
    assert not checker.scan_text('API_KEY = "synthetic-test-key"', "tests/test_example.py")
    assert checker.scan_text('API_KEY = "synthetic-test-key"', "src/config.py")


def test_container_acceptance_canaries_are_exact_and_path_bound():
    for value in ("ci-model-fixture-key", "sk-ci-parser-fixture-key", "ci-embedding-fixture-key"):
        source = f'API_KEY = "{value}"'
        assert not checker.scan_text(source, "scripts/accept_local_stack.py")
        assert checker.scan_text(source, "src/config.py")
    assert checker.scan_text('API_KEY = "' + 'unreviewed-ci-' + 'fixture-value"',
                             "scripts/accept_local_stack.py")


@pytest.mark.parametrize("value", [
    'PASSWORD = "' + 'unreviewed-' + 'password-value"',
    "SERVICE_TOKEN=abC1dE2fG3hI4jK5lM6n7",
    "postgresql://" + "alice:private_password@db/database",
])
def test_other_secret_forms_fail_closed(value):
    # SERVICE_TOKEN is tested below after constructing its exact auth name.
    value = value.replace("SERVICE_TOKEN", "SERVICE_AUTH_TOKEN")
    assert checker.scan_text(value, ".env.example")


def test_env_example_may_have_placeholders_but_not_real_tokens(tmp_path):
    path = write(tmp_path, ".env.example", "API_KEY=\n")
    assert not codes(tmp_path)
    path.write_text("API_KEY=" + "A9z" * 10 + "\n")
    assert codes(tmp_path) == {"credential"}


def test_symlink_cannot_escape_or_bypass_private_path_checks(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("not read")
    (tmp_path / "source.py").symlink_to(outside)
    (tmp_path / "web").symlink_to(tmp_path.parent, target_is_directory=True)
    assert codes(tmp_path) == {"non-regular-file", "symlink"}


def test_relative_and_dynamic_runtime_imports_require_actual_modules(tmp_path):
    write(tmp_path, "src/forgeflow/__init__.py", "")
    write(tmp_path, "src/forgeflow/api/__init__.py", "")
    write(tmp_path, "src/forgeflow/api/app.py",
          'from . import missing\nimportlib.import_module("forgeflow.worker")\n')
    findings = checker.check_imports(tmp_path)
    assert len(findings) == 2
    assert all(x.code == "missing-python-dependency" for x in findings)
    write(tmp_path, "src/forgeflow/api/missing.py", "")
    write(tmp_path, "src/forgeflow/worker.py", "")
    assert not checker.check_imports(tmp_path)


def test_exported_python_values_are_not_mistaken_for_missing_modules(tmp_path):
    write(tmp_path, "src/forgeflow/__init__.py", '__version__ = "1"\n')
    write(tmp_path, "src/forgeflow/main.py", "from forgeflow import __version__\n")
    assert not checker.check_imports(tmp_path)


def test_reference_sources_and_review_pin_are_checked(tmp_path):
    shutil.copytree(ROOT / checker.PACK, tmp_path / checker.PACK)
    write(tmp_path, "src/forgeflow/knowledge/reference_facts.py",
          (ROOT / "src/forgeflow/knowledge/reference_facts.py").read_text())
    assert not checker.check_reference_pack(tmp_path)
    path = tmp_path / checker.PACK / "K04.md"
    path.write_text(path.read_text().replace("636", "637"))
    assert "reference-hash-mismatch" in {x.code for x in checker.check_reference_pack(tmp_path)}
    sidecar = tmp_path / checker.PACK / "comparisons.json"
    sidecar.write_text(sidecar.read_text() + " ")
    assert "reference-review-pin-mismatch" in {x.code for x in checker.check_reference_pack(tmp_path)}
    path.unlink()
    assert "reference-pack-incomplete" in {x.code for x in checker.check_reference_pack(tmp_path)}


def test_docx_template_metadata_and_external_relationships_are_scanned(tmp_path):
    path = tmp_path / "reference.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("docProps/core.xml", '<root><creator>A person</creator></root>')
        archive.writestr("word/_rels/document.xml.rels",
                         '<root><Relationship TargetMode="External" Target="file:///etc/passwd"/></root>')
    assert {x.code for x in checker._docx(path, "reference.docx")} == {"template-private-metadata"}


def test_reviewed_template_has_no_hidden_content_or_private_metadata():
    assert not checker._docx(ROOT / "deploy/pandoc-reference.docx", "deploy/pandoc-reference.docx")


def test_docker_copy_inputs_and_package_locks_are_part_of_dependency_closure(tmp_path):
    for rel in ("web", "deploy/vps/dsh"):
        write(tmp_path, f"{rel}/package.json", '{"dependencies":{"example":"1.0.0"}}')
        write(tmp_path, f"{rel}/package-lock.json",
              '{"packages":{"":{"dependencies":{"example":"1.0.0"}}}}')
    write(tmp_path, "deploy/workbench/Dockerfile.app",
          "FROM scratch\nCOPY scripts/worker.py /app/worker.py\nCOPY --from=build /output /app/web\n")
    assert {x.code for x in checker.check_build_inputs(tmp_path)} == {"missing-build-input"}
    write(tmp_path, "scripts/worker.py", "")
    assert not checker.check_build_inputs(tmp_path)
    write(tmp_path, "web/package.json", '{"dependencies":{"example":"2.0.0"}}')
    assert {x.code for x in checker.check_build_inputs(tmp_path)} == {"npm-lock-mismatch"}


def test_required_runtime_file_cannot_be_silently_omitted(tmp_path):
    findings, _ = checker.check_distribution(tmp_path)
    required = {x.path for x in findings if x.code == "missing-required-file"}
    assert "scripts/assistant_mcp.py" in required
    assert "migrations/control/024_trigger_admission_cardinality.sql" in required
    assert "web/src/historicalDemo.json" in required


def test_frontend_imports_and_odoo_registry_data_cannot_be_silently_dropped(tmp_path):
    write(tmp_path, "web/src/main.tsx", 'import View from "./View";\n')
    write(tmp_path, "odoo-addons/forgeflow_general/__init__.py", "from . import models\n")
    write(tmp_path, "odoo-addons/forgeflow_general/__manifest__.py", "{'data':['security/access.csv']}")
    assert {x.code for x in checker.check_web_and_addon(tmp_path)} == {
        "missing-web-dependency", "missing-addon-dependency", "missing-addon-data"}
    write(tmp_path, "web/src/View.tsx", "export default 1;\n")
    write(tmp_path, "odoo-addons/forgeflow_general/models/__init__.py", "")
    write(tmp_path, "odoo-addons/forgeflow_general/security/access.csv", "id,name\n")
    assert not checker.check_web_and_addon(tmp_path)
