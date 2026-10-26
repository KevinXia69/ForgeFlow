"""Reviewer installation boundaries; no host users, services or ERP are changed."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pwd")

SPEC = importlib.util.spec_from_file_location(
    "vps_reviewers_test", Path(__file__).resolve().parents[1] / "scripts/vps_reviewers.py")
reviewers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reviewers)


def base():
    return {"repo_root": "/opt/forgeflow", "python": "/opt/forgeflow/.venv/bin/python",
            "odoo_image": "sha256:" + "a" * 64, "dsh_cli": "/opt/forgeflow/dsh/bin.js",
            "dsh_node": "/usr/local/bin/node"}


def test_five_judges_and_qa_have_disjoint_sensitive_resources():
    configs = [reviewers.definition(name, base()) for name in
               [*("judge" + str(i) for i in range(1, 6)), "qa1", "qa2"]]
    for key in ("api_user", "dsh_user", "sandbox_root", "case_root", "data_root", "socket",
                "container", "odoo_port", "api_port", "dsh_root", "dsh_config", "dsh_launcher",
                "service_unit"):
        assert len({config[key] for config in configs}) == 7, key
    assert all(config["api_port"] != 8000 and config["odoo_port"] != 18069 for config in configs)
    assert all(not config["socket"].startswith("/run/forgeflow/") for config in configs)


@pytest.mark.parametrize("name", ["../../root", "default", "legacy", "judge6", "qa3", "judge1;id"])
def test_installer_has_no_user_selected_paths_or_arbitrary_instance_names(name):
    with pytest.raises(ValueError, match="unknown_reviewer_instance"):
        reviewers.definition(name, base())


def test_gateway_configuration_gets_only_invite_hashes_and_its_own_secret(monkeypatch, tmp_path):
    index = {"trusted_hosts": ["example.test"],
             "legacy": {"id": "legacy", "port": 8000, "invite_code": "legacy-private"},
             "instances": {name: {"id": name, "port": port, "invite_code": name + "-private"}
                           for name, port in (("judge1", 8101), ("qa1", 8106))}}
    captured = []
    monkeypatch.setattr(reviewers, "CONFIG_ROOT", tmp_path)
    monkeypatch.setattr(reviewers, "load_index", lambda: index)
    monkeypatch.setattr(reviewers, "system_user", lambda _name: SimpleNamespace(pw_gid=12345))
    monkeypatch.setattr(reviewers, "write_json", lambda path, value, **kwargs: captured.append((value, kwargs)))
    result = reviewers.gateway("judge1")
    config, options = captured[-1]
    serialized = json.dumps(config)
    assert "-private" not in serialized
    assert [item["id"] for item in config["tenants"]] == ["legacy", "judge1"]
    assert len(config["routing_secret"]) >= 32
    assert options == {"mode": 0o640, "gid": 12345}
    assert result["public_proxy_unchanged"] is True
    with pytest.raises(ValueError, match="public_reviewer_instance_missing"):
        reviewers.gateway("qa1")


def test_instance_backup_bindings_do_not_point_at_another_reviewer():
    config = reviewers.definition("judge2", base())
    backup = reviewers.backup_definition(config, {"encryption_key_file": "/etc/forgeflow/backup.key",
                                                 "output_root": "/var/backups/forgeflow"})
    assert backup["maintenance_units"] == ["forgeflow-runtime@judge2.service"]
    assert backup["runtime_root"] == config["sandbox_root"]
    assert backup["secret_files"]["runtime.json"] == "/etc/forgeflow/instances/judge2.json"
    assert backup["output_root"] == "/var/backups/forgeflow/judge2"
    assert all("judge1" not in str(value) for value in backup.values())
