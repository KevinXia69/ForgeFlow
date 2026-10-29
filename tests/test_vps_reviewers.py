"""Reviewer installation boundaries; no host users, services or ERP are changed."""
import copy
import hashlib
import importlib.util
import json
import stat
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
               reviewers.REVIEWER_NAMES]
    for key in ("api_user", "dsh_user", "sandbox_root", "case_root", "data_root", "socket",
                "container", "odoo_port", "api_port", "dsh_root", "dsh_config", "dsh_launcher",
                "service_unit"):
        assert len({config[key] for config in configs}) == 8, key
    assert all(config["api_port"] != 8000 and config["odoo_port"] != 18069 for config in configs)
    assert all(not config["socket"].startswith("/run/forgeflow/") for config in configs)


@pytest.mark.parametrize("name", ["../../root", "default", "legacy", "judge6", "qa4", "qa10", "judge1;id"])
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


def test_qa3_uses_next_fixed_ports_and_rejects_a_legacy_or_registered_collision():
    config = reviewers.definition("qa3", base())
    assert (config["api_port"], config["odoo_port"]) == (8108, 18108)
    legacy = {"api_port": 8000, "odoo_port": 18069}
    index = {"instances": {name: {"port": 8100 + i, "odoo_port": 18100 + i}
                          for i, name in enumerate(reviewers.REVIEWER_NAMES[:-1], 1)}}
    reviewers.check_ports({"qa3": config}, legacy, index)
    with pytest.raises(ValueError, match="reviewer_port_conflict"):
        reviewers.check_ports({"qa3": config}, {**legacy, "odoo_port": 8108}, index)
    index["instances"]["qa2"]["port"] = 8108
    with pytest.raises(ValueError, match="reviewer_port_conflict"):
        reviewers.check_ports({"qa3": config}, legacy, index)


@pytest.fixture
def routed(monkeypatch, tmp_path):
    index = {"trusted_hosts": ["example.test"],
        "legacy": {"id": "legacy", "port": 8000, "invite_code": "legacy-private"},
        "instances": {name: {"id": name, "port": 8100 + i, "invite_code": name + "-private"}
                      for i, name in enumerate(reviewers.REVIEWER_NAMES, 1)}}
    original = {"routing_secret": "preserved-secret-" * 4, "trusted_hosts": ["example.test"],
        "public_tenant": "judge2", "tenants": [reviewers.tenant(index["legacy"]),
            *(reviewers.tenant(index["instances"][name]) for name in reviewers.REVIEWER_NAMES[:-1])],
        "operator_note": "retain future root configuration fields"}
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps(original))
    writes = []

    def save(destination, value, **options):
        writes.append((destination, copy.deepcopy(value), options))
        destination.write_text(json.dumps(value))

    monkeypatch.setattr(reviewers, "CONFIG_ROOT", tmp_path)
    monkeypatch.setattr(reviewers, "load_index", lambda: copy.deepcopy(index))
    monkeypatch.setattr(reviewers, "trusted_json", lambda path: json.loads(path.read_text()))
    monkeypatch.setattr(reviewers, "system_user", lambda _: SimpleNamespace(pw_gid=12345))
    monkeypatch.setattr(reviewers, "write_json", save)
    return index, original, path, writes


def test_gateway_append_preserves_all_routes_secrets_and_is_write_free_on_replay(routed):
    from forgeflow.judge_gateway import issue_route, read_route

    index, original, path, writes = routed
    cookie = issue_route(original, "qa2", "prior-session", "example.test", now=100)
    result = reviewers.gateway_append(["qa3"])
    actual = json.loads(path.read_text())
    assert actual == {**original, "tenants": [*original["tenants"], reviewers.tenant(index["instances"]["qa3"])]}
    assert result["added"] == ["qa3"] and result["public_tenant"] == "judge2"
    assert actual["tenants"][-1]["invite_sha256"] == [hashlib.sha256(b"qa3-private").hexdigest()]
    assert "-private" not in json.dumps(actual) and original["routing_secret"] not in json.dumps(result)
    assert writes[0][2] == {"mode": 0o640, "gid": 12345}
    assert read_route(actual, cookie, "prior-session", "example.test", now=101) == "qa2"
    before = path.read_bytes()
    replay = reviewers.gateway_append(["qa3"])
    assert replay["changed"] is False and replay["added"] == []
    assert len(writes) == 1 and path.read_bytes() == before


@pytest.mark.parametrize("fault", ["port", "credential", "hosts", "unregistered", "duplicate", "limit"])
def test_gateway_append_conflicts_cannot_change_the_live_configuration(routed, fault):
    index, original, path, writes = routed
    names = ["qa3"]
    if fault == "port":
        index["instances"]["qa3"]["port"] = 8101
    elif fault == "credential":
        names = ["qa2"]
        index["instances"]["qa2"]["invite_code"] = "changed-private"
    elif fault == "hosts":
        index["trusted_hosts"] = ["other.example.test"]
    elif fault == "unregistered":
        del index["instances"]["qa3"]
    elif fault == "duplicate":
        names = ["qa3", "qa3"]
    else:
        while len(original["tenants"]) < 32:
            i = len(original["tenants"])
            original["tenants"].append({"id": f"retained-{i}", "port": 9000 + i,
                "invite_sha256": [hashlib.sha256(f"other-{i}".encode()).hexdigest()]})
        path.write_text(json.dumps(original))
    before = path.read_bytes()
    with pytest.raises((ValueError, KeyError)):
        reviewers.gateway_append(names)
    assert not writes and path.read_bytes() == before


def test_old_gateway_command_cannot_silently_drop_retained_qa_routes(routed):
    _, _, path, writes = routed
    before = path.read_bytes()
    with pytest.raises(ValueError, match="existing_gateway_routes_would_change_use_append"):
        reviewers.gateway("judge2")
    assert not writes and path.read_bytes() == before


def test_preparing_qa3_twice_preserves_existing_reviewers_and_issued_credentials(monkeypatch, tmp_path):
    config_root, state_root, repo = tmp_path / "etc", tmp_path / "state", tmp_path / "repo"
    unit = repo / "deploy/vps/forgeflow-runtime@.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\nDescription=Review instance\n")
    monkeypatch.setattr(reviewers, "CONFIG_ROOT", config_root)
    monkeypatch.setattr(reviewers, "STATE_ROOT", state_root)
    monkeypatch.setattr(reviewers, "INDEX_PATH", config_root / "reviewers.json")
    runtime = reviewers.configuration({**base(), "repo_root": str(repo),
        "sandbox_root": "/var/lib/legacy", "case_root": "/var/lib/legacy/cases",
        "data_root": "/var/lib/legacy/runs", "socket": "/run/legacy.sock",
        "container": "legacy-odoo", "scenario": "A", "api_port": 8000, "odoo_port": 18069})
    codes = [str(i) * 32 for i in range(6)]
    index = {"schema": 1, "trusted_hosts": ["example.test"],
        "legacy": {"id": "legacy", "port": 8000, "invite_code": codes[0]},
        "instances": {name: {"id": name, "port": 8100 + i, "odoo_port": 18100 + i,
                             "invite_code": name + "-retained-credential", "history": ["must-remain"]}
                      for i, name in enumerate(reviewers.REVIEWER_NAMES[:-1], 1)}}
    initial = copy.deepcopy(index)
    key = tmp_path / "backup.key"
    documents = {config_root / "runtime.json": runtime,
        config_root / "backup.json": {"encryption_key_file": str(key), "output_root": "/var/backups/forgeflow"},
        reviewers.INDEX_PATH: index}
    envs = {config_root / "runtime.env": {"FORGEFLOW_INVITE_CODES": ",".join(codes),
                                          "FORGEFLOW_TRUSTED_HOSTS": "example.test"}}
    writes, commands, identities, directories = [], [], [], []

    def command(argv, **kwargs):
        commands.append(argv)
        return runtime["odoo_image"] if argv[:3] == ["docker", "image", "inspect"] else ""

    def save_json(path, value, **kwargs):
        documents[path] = copy.deepcopy(value)
        writes.append(path)

    def save(path, text, **kwargs):
        writes.append(path)
        if path.suffix == ".env":
            envs[path] = {k: json.loads(v) for k, v in (line.split("=", 1) for line in text.splitlines())}

    def user(name):
        identities.append(name)
        return SimpleNamespace(pw_uid=1001 if "-api-" in name else 1002,
                               pw_gid=1001 if "-api-" in name else 1002)

    real_lstat = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda p: SimpleNamespace(st_mode=stat.S_IFREG | 0o600,
        st_uid=0, st_size=32) if p == key else real_lstat(p))
    monkeypatch.setattr(reviewers, "trusted_json", lambda path: copy.deepcopy(documents[path]))
    monkeypatch.setattr(reviewers, "load_index", lambda: copy.deepcopy(documents[reviewers.INDEX_PATH]))
    monkeypatch.setattr(reviewers, "private_env", lambda path: copy.deepcopy(envs[path]))
    monkeypatch.setattr(reviewers, "directory", lambda path: directories.append(path))
    monkeypatch.setattr(reviewers, "system_user", user)
    monkeypatch.setattr(reviewers, "command", command)
    monkeypatch.setattr(reviewers, "write_json", save_json)
    monkeypatch.setattr(reviewers, "write", save)
    monkeypatch.setattr(reviewers.os, "replace", lambda *_: None)
    reviewers.prepare(["qa3"])
    credentials = copy.deepcopy(envs[config_root / "instances/qa3.env"])
    config = copy.deepcopy(documents[config_root / "instances/qa3.json"])
    reviewers.prepare(["qa3"])
    assert envs[config_root / "instances/qa3.env"] == credentials
    assert documents[config_root / "instances/qa3.json"] == config
    assert all(documents[reviewers.INDEX_PATH]["instances"][name] == row
               for name, row in initial["instances"].items())
    assert set(identities) == {"forgeflow-api-qa3", "forgeflow-dsh-qa3"}
    assert writes.count(config_root / "instances/qa3.env") == 1
    assert writes.count(config_root / "instances/qa3.json") == 1
    assert not any("qa1" in str(path) or "qa2" in str(path) for path in writes + directories)
    assert not any(argv[:2] in (["systemctl", "start"], ["docker", "rm"], ["docker", "run"])
                   for argv in commands)
