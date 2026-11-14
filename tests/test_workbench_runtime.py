from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.local_workbench import runtime
from forgeflow.superagent.store import ControlStore

ROOT = Path(__file__).resolve().parents[1]


def seed_module():
    spec = importlib.util.spec_from_file_location(
        "local_odoo_seed", ROOT / "deploy/workbench/odoo_bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_sql_is_an_error_before_any_database_statement(tmp_path):
    connection = SimpleNamespace(execute=lambda *_: pytest.fail("database changed"))
    store = ControlStore("unused", migrations_dir=tmp_path, connection=connection)
    with pytest.raises(RuntimeError, match="^control_migrations_missing$"):
        store.migrate()


def test_explicit_migration_directory_is_honored(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGEFLOW_CONTROL_MIGRATIONS", str(tmp_path))
    assert ControlStore("unused", connection=object()).migrations_dir == tmp_path


def test_assistant_patch_uses_shared_max_profile_without_keys(monkeypatch, tmp_path):
    target = tmp_path / "dsh" / "base.json"
    monkeypatch.setattr(runtime, "application_root", lambda: ROOT)
    monkeypatch.setenv("FORGEFLOW_DSH_PATCH", str(target))
    monkeypatch.setenv("FORGEFLOW_ASSISTANT_PROFILE", "deepseek_flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "do-not-copy-provider-secret")
    monkeypatch.setenv("FORGEFLOW_DSH_REASONING_EFFORT", "off")
    monkeypatch.delenv("FORGEFLOW_BUILTIN_GUIDE_ROOT", raising=False)
    runtime.prepare_assistant()
    value = json.loads(target.read_text())
    provider = next(x for x in value if x["id"] == "llm-pi-ai")["config"]["providers"]["deepseek"]
    assert provider["reasoning"] == "max"
    assert os.environ["FORGEFLOW_DSH_REASONING_EFFORT"] == "off"
    assert os.environ["FORGEFLOW_BUILTIN_GUIDE_ROOT"] == str(ROOT)
    assert "do-not-copy-provider-secret" not in target.read_text()
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(target.parent.glob(".dsh-patch-*"))


def test_assistant_forwards_backend_contract_and_keeps_guides_out_of_uploads(monkeypatch, tmp_path):
    from forgeflow import vps_runtime
    from forgeflow.api import model_settings

    uploaded = tmp_path / "uploaded"
    uploaded.mkdir()
    document = uploaded / "user.md"
    document.write_text("user-owned content")
    monkeypatch.setenv("FORGEFLOW_KNOWLEDGE_ROOT", str(uploaded))
    monkeypatch.setenv("FORGEFLOW_DSH_PATCH", str(tmp_path / "base.json"))
    monkeypatch.delenv("FORGEFLOW_BUILTIN_GUIDE_ROOT", raising=False)
    monkeypatch.setattr(runtime, "application_root", lambda: ROOT)
    monkeypatch.setattr(model_settings, "assistant_model", lambda: {
        "provider": "reviewed-provider", "model": "reviewed-model",
        "base_url": "https://example.invalid", "api_key_env": "REVIEWED_KEY",
        "api": "openai-responses", "input": ["text"], "catalog_native": True})
    observed = {}

    def patch(_config, _model, _base_url, **kwargs):
        observed.update(kwargs)
        return []

    monkeypatch.setattr(vps_runtime, "dsh_patch", patch)
    runtime.prepare_assistant()
    assert observed == {"provider": "reviewed-provider", "api_key_env": "REVIEWED_KEY",
                        "api": "openai-responses", "input_types": ["text"], "catalog_native": True}
    assert os.environ["FORGEFLOW_BUILTIN_GUIDE_ROOT"] == str(ROOT)
    assert os.environ["FORGEFLOW_KNOWLEDGE_ROOT"] == str(uploaded)
    assert list(uploaded.iterdir()) == [document]
    assert document.read_text() == "user-owned content"


def test_assistant_patch_refuses_symlink(monkeypatch, tmp_path):
    original = tmp_path / "original"
    original.write_text("preserve")
    target = tmp_path / "base.json"
    target.symlink_to(original)
    monkeypatch.setattr(runtime, "application_root", lambda: ROOT)
    monkeypatch.setenv("FORGEFLOW_DSH_PATCH", str(target))
    with pytest.raises(RuntimeError, match="workbench_assistant_patch_symlink"):
        runtime.prepare_assistant()
    assert original.read_text() == "preserve"


def test_repeated_odoo_seed_does_not_touch_business_or_users():
    module = seed_module()
    config = {"database": "forgeflow", "installation_id": "installed", "operator": {"login": "local"}}
    saved = {"schema_version": 1, "installation_id": "installed", "operator_login": "local",
             "company_id": 8, "operator_id": 9}

    class Environment:
        cr = SimpleNamespace(dbname="forgeflow", execute=lambda *_: None)

        def __getitem__(self, model):
            assert model == "ir.config_parameter", "restart attempted a business/user mutation"
            return SimpleNamespace(sudo=lambda: SimpleNamespace(get_param=lambda _: json.dumps(saved)))

    assert module.seed(Environment(), config) == {
        "ok": True, "created": False, "synthetic": True, "company_id": 8, "operator_id": 9}
    config["installation_id"] = "another-installation"
    with pytest.raises(module.SeedError, match="odoo_installation_mismatch"):
        module.seed(Environment(), config)


def test_odoo_seed_rejects_database_mismatch_before_queries():
    module = seed_module()
    env = SimpleNamespace(cr=SimpleNamespace(dbname="unrelated",
                          execute=lambda *_: pytest.fail("query against unrelated database")))
    with pytest.raises(module.SeedError, match="odoo_bootstrap_database_mismatch"):
        module.seed(env, {"database": "forgeflow"})


def test_odoo_configuration_symlink_and_public_file_rejected(tmp_path):
    module = seed_module()
    path = tmp_path / "config.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(module.SeedError, match="unsafe_odoo_bootstrap_configuration"):
        module.read_configuration(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(module.SeedError, match="invalid_odoo_bootstrap_configuration"):
        module.read_configuration(link)


def test_odoo_configuration_allows_only_bound_group_read(tmp_path):
    import uuid

    module = seed_module()
    value = {"schema_version": 1, "installation_id": str(uuid.uuid4()), "database": "synthetic",
             "operator": {"login": "local", "password": "synthetic", "display_name": "Local"},
             "admin_password": "synthetic-admin-password-long-enough"}
    path = tmp_path / "bootstrap.json"
    path.write_text(json.dumps(value))
    path.chmod(0o640)
    assert module.read_configuration(path, expected_uid=os.getuid(), expected_gid=os.getgid()) == value
    with pytest.raises(module.SeedError, match="unsafe_odoo_bootstrap_configuration"):
        module.read_configuration(path, expected_uid=os.getuid(), expected_gid=os.getgid() + 1)
    path.chmod(0o660)
    with pytest.raises(module.SeedError, match="unsafe_odoo_bootstrap_configuration"):
        module.read_configuration(path, expected_uid=os.getuid(), expected_gid=os.getgid())


def test_doctor_failure_logs_no_config_values(monkeypatch, capsys):
    from forgeflow.local_workbench import bootstrap, config

    monkeypatch.setattr(config, "load_config", lambda: {"env": {}})
    monkeypatch.setattr(runtime, "configure_environment", lambda _: None)
    monkeypatch.setattr(bootstrap, "doctor", lambda _: (_ for _ in ()).throw(
        RuntimeError("postgresql://user:do-not-print@db/provider-secret")))
    assert runtime.main(["doctor"]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["error"] == "RuntimeError"
    assert "do-not-print" not in output and "provider-secret" not in output
