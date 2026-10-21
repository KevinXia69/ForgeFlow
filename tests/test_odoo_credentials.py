from __future__ import annotations

import pytest

from forgeflow import odoo_credentials


def test_default_path_is_the_admin_key(monkeypatch):
    monkeypatch.delenv(odoo_credentials.ENV_NAME, raising=False)
    assert odoo_credentials.key_path() == odoo_credentials.Path("/etc/odoo/api_key")


def test_environment_overrides_the_path(monkeypatch, tmp_path):
    path = tmp_path / "api_key.read"
    path.write_text("secret-key\n")
    monkeypatch.setenv(odoo_credentials.ENV_NAME, str(path))
    assert odoo_credentials.key_path() == path
    assert odoo_credentials.read_key() == "secret-key"


def test_missing_key_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(odoo_credentials.ENV_NAME, str(tmp_path / "absent"))
    with pytest.raises(OSError):
        odoo_credentials.read_key()
