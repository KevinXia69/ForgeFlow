"""Real HTTP setup fences and private on-disk configuration, no providers/Docker."""
from __future__ import annotations

import http.client
import json
import stat
import threading
from http.server import ThreadingHTTPServer

import pytest
from cryptography import x509

from forgeflow.local_workbench import config
from forgeflow.local_workbench.setup import PUBLIC_ORIGIN, SetupState, make_handler


@pytest.fixture
def files(tmp_path):
    path = tmp_path / 'config' / 'runtime.json'
    mounts = {name: tmp_path / name for name in ('control', 'odoo', 'odoo_config', 'qdrant')}
    return path, mounts


@pytest.fixture
def payload():
    return {'profile': 'deepseek_flash', 'model_api_key': 'chat-secret-123456',
            'dashscope_api_key': 'sk-parser-secret-1234', 'siliconflow_api_key': 'embed-secret-1234',
            'display_name': 'Local operator'}


def test_config_secrets_permissions_tls_and_service_boundaries(files, payload):
    path, mounts = files
    value = config.save_configuration(payload, path, mounts=mounts)
    assert config.load_config(path) == value
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert value['env']['FORGEFLOW_ODOO20_PASSWORD'] == value['operator']['password']
    assert value['env']['FORGEFLOW_KNOWLEDGE_ROOT'] == '/data/knowledge'
    assert value['env']['FORGEFLOW_DSH_ENABLED'] == 'true'
    assert value['env']['FORGEFLOW_DSH_SDK'] == 'true'
    assert value['env']['FORGEFLOW_MODEL_API_KEY'] == payload['model_api_key']
    assert value['env']['FORGEFLOW_CONTROL_DATABASE_URL'].endswith('/forgeflow_control')
    assert value['env']['FORGEFLOW_ODOO20_DB'] == 'forgeflow_local'
    assert value['env']['FORGEFLOW_SEMANTIC_CA_FILE'] == '/config/ca-bundle.pem'
    for key in ('control', 'odoo'):
        assert stat.S_IMODE((mounts[key] / 'password').stat().st_mode) == 0o444
        assert len((mounts[key] / 'password').read_text()) >= 32
    odoo_bootstrap = json.loads((mounts['odoo_config'] / 'bootstrap.json').read_text())
    assert set(odoo_bootstrap) == {'schema_version', 'installation_id', 'database', 'operator', 'admin_password'}
    assert 'chat-secret' not in json.dumps(odoo_bootstrap)
    assert 'embed-secret' not in (mounts['qdrant'] / 'config.yaml').read_text()
    cert = x509.load_pem_x509_certificate((mounts['qdrant'] / 'cert.pem').read_bytes())
    assert cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) == ['qdrant']
    assert (mounts['qdrant'] / 'cert.pem').read_bytes() in (path.parent / 'ca-bundle.pem').read_bytes()


def test_update_keys_preserves_installation_database_identity_and_tls(files, payload):
    path, mounts = files
    before = config.save_configuration(payload, path, mounts=mounts)
    tls = (mounts['qdrant'] / 'key.pem').read_bytes()
    after = config.save_configuration({**payload, 'model_api_key': 'new-chat-secret-1234',
                                      'dashscope_api_key': '', 'siliconflow_api_key': ''}, path, mounts=mounts)
    assert before['installation_id'] == after['installation_id']
    assert before['operator'] == after['operator']
    assert (mounts['qdrant'] / 'key.pem').read_bytes() == tls
    differences = {key for key in before['env'] if before['env'][key] != after['env'][key]}
    assert differences == {'DEEPSEEK_API_KEY', 'FORGEFLOW_MODEL_API_KEY', 'FORGEFLOW_CHAT_API_KEY'}


def test_switch_profile_requires_new_key_and_removes_old_provider(files, payload):
    path, mounts = files
    config.save_configuration(payload, path, mounts=mounts)
    with pytest.raises(config.ConfigError, match='valid_provider_keys_required'):
        config.save_configuration({**payload, 'profile': 'nemotron', 'model_api_key': ''}, path, mounts=mounts)
    result = config.save_configuration({**payload, 'profile': 'nemotron', 'model_api_key': 'new-nebius-key'}, path, mounts=mounts)
    assert result['env']['DEEPSEEK_API_KEY'] == payload['model_api_key']
    assert result['env']['NEBIUS_API_KEY'] == 'new-nebius-key'


@pytest.mark.parametrize('change', [
    {'env': {'ATTACKER': 'value'}}, {'profile': 'custom'}, {'model_api_key': 'secret\ninjection'},
    {'model_api_key': ''}, {'dashscope_api_key': ''}, {'siliconflow_api_key': ''},
    {'display_name': 'bad\x00name'}, {'display_name': ''},
    {'dashscope_api_key': 'wrong-service-format'},
])
def test_invalid_setup_has_no_committed_configuration(files, payload, change):
    path, mounts = files
    with pytest.raises(config.ConfigError):
        config.save_configuration({**payload, **change}, path, mounts=mounts)
    assert not path.exists()


def test_invalid_existing_configuration_never_rotates_identity(files, payload):
    path, mounts = files
    path.parent.mkdir()
    path.write_text('{bad json')
    path.chmod(0o600)
    with pytest.raises(config.ConfigError, match='configuration_invalid'):
        config.save_configuration(payload, path, mounts=mounts)
    assert path.read_text() == '{bad json'


def test_private_configuration_rejects_symlink_and_world_readable(files, payload, tmp_path):
    path, mounts = files
    config.save_configuration(payload, path, mounts=mounts)
    path.chmod(0o644)
    with pytest.raises(config.ConfigError, match='unsafe_configuration_permissions'):
        config.load_config(path)
    link = tmp_path / 'link'
    link.symlink_to(path)
    with pytest.raises(config.ConfigError):
        config.load_config(link)
    with pytest.raises(config.ConfigError):
        config.save_configuration(payload, link, mounts=mounts)


@pytest.fixture
def server(files):
    async def tester(connection, key):
        return {"ok": bool(key), "model": connection["model"], "protocol": connection["api"]}
    state = SetupState(files[0], mounts=files[1], connection_tester=tester)
    token = state.issue()
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(state))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None, **overrides):
        conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
        headers = {'Host': '127.0.0.1:17801', 'X-ForgeFlow-Setup': token,
                   'Origin': PUBLIC_ORIGIN, 'Content-Type': 'application/json'}
        headers.update(overrides)
        headers = {key: val for key, val in headers.items() if val is not None}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result
    yield state, token, request
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def test_actual_http_setup_one_time_token_no_secret_readback(server, payload, capsys):
    state, token, request = server
    assert token in state.url().split('#')[1]
    status, headers, body = request('GET', '/')
    assert status == 200 and token.encode() not in body
    assert "default-src 'none'" in headers['Content-Security-Policy']
    assert headers['Cache-Control'] == 'no-store'
    assert json.loads(request('GET', '/state')[2])['configured'] is False
    tested = json.loads(request('POST', '/test', json.dumps(payload))[2])
    payload = {**payload, 'test_receipt': tested['test_receipt']}
    status, _, response = request('POST', '/configure', json.dumps(payload))
    assert status == 200
    assert json.loads(response)['invite_code'] == config.load_config(state.path)['env']['FORGEFLOW_INVITE_CODES']
    assert request('POST', '/configure', json.dumps(payload))[0] == 403
    assert request('GET', '/state')[0] == 403
    assert 'chat-secret' not in capsys.readouterr().out
    assert payload['model_api_key'].encode() not in response


@pytest.mark.parametrize('headers', [
    {'Host': 'evil.example:17801'}, {'Host': 'localhost:17801'},
    {'Origin': 'https://evil.example'}, {'Origin': 'null'}, {'Origin': None},
    {'Sec-Fetch-Site': 'cross-site'}, {'X-ForgeFlow-Setup': 'wrong'},
    {'X-ForgeFlow-Setup': None},
])
def test_cross_site_rebinding_and_missing_capability_cannot_save(server, payload, headers):
    state, _, request = server
    assert request('POST', '/configure', json.dumps(payload), **headers)[0] == 403
    assert not state.path.exists()


def test_get_never_configures_and_post_requires_bounded_json(server, payload):
    state, _, request = server
    assert request('GET', '/configure')[0] == 404
    assert request('POST', '/configure', 'x' * 8193)[0] == 413
    assert request('POST', '/configure', json.dumps(payload), **{'Content-Type': 'text/plain'})[0] == 415
    assert not state.path.exists()


def test_token_expiry_and_restart_invalidate_old_capability(server):
    state, token, request = server
    config.atomic_write(state.token_path, json.dumps({'token': token, 'expires': 0, 'used': False}).encode())
    assert request('GET', '/state')[0] == 403
    fresh = state.issue()
    assert fresh != token
    assert state.authorized(fresh) and not state.authorized(token)


def test_state_reveals_no_saved_credentials(server, payload):
    state, _, request = server
    config.save_configuration(payload, state.path, mounts=state.mounts)
    status, _, raw = request('GET', '/state')
    assert status == 200
    assert set(json.loads(raw)) == {'configured', 'model_connection', 'presets', 'display_name', 'has_deepseek_search_key'}
    for value in payload.values():
        if 'secret' in value:
            assert value.encode() not in raw


def test_nebius_first_setup_requires_own_deepseek_search_key(files, payload):
    path, mounts = files
    with pytest.raises(config.ConfigError, match='valid_provider_keys_required'):
        config.save_configuration({**payload, 'profile': 'nemotron'}, path, mounts=mounts)
    assert not path.exists()
    value = config.save_configuration({**payload, 'profile': 'nemotron',
                                       'deepseek_search_api_key': 'separate-search-secret'}, path, mounts=mounts)
    assert value['env']['NEBIUS_API_KEY'] == payload['model_api_key']
    assert value['env']['DEEPSEEK_API_KEY'] == 'separate-search-secret'
    assert value['env']['FORGEFLOW_MODEL_API_KEY'] == payload['model_api_key']


def test_nebius_search_key_rotation_preserves_chat_and_internal_credentials(files, payload):
    path, mounts = files
    first = config.save_configuration({**payload, 'profile': 'nemotron',
                                       'deepseek_search_api_key': 'first-search-secret'}, path, mounts=mounts)
    updated = config.save_configuration({'profile': 'nemotron',
                                         'deepseek_search_api_key': 'second-search-secret'}, path, mounts=mounts)
    assert first['installation_id'] == updated['installation_id']
    assert {key for key in first['env'] if first['env'][key] != updated['env'][key]} == {'DEEPSEEK_API_KEY'}
    preserved = config.save_configuration({'profile': 'nemotron', 'deepseek_search_api_key': ''}, path, mounts=mounts)
    assert preserved == updated


def test_deepseek_profile_reuses_chat_key_and_scoped_child_has_no_business_secrets(files, payload, tmp_path):
    from forgeflow.api.dsh_environment import assistant_environment

    path, mounts = files
    value = config.save_configuration(payload, path, mounts=mounts)
    assert value['env']['DEEPSEEK_API_KEY'] == value['env']['FORGEFLOW_MODEL_API_KEY']
    nebius = config.save_configuration({**payload, 'profile': 'nemotron',
                                        'model_api_key': 'nebius-chat-secret'}, path, mounts=mounts)
    child = assistant_environment(nebius['env'], home=tmp_path / 'harness',
                                  model_api_key=nebius['env']['FORGEFLOW_CHAT_API_KEY'])
    assert child['DEEPSEEK_API_KEY'] == payload['model_api_key']
    assert child['FORGEFLOW_CHAT_API_KEY'] == 'nebius-chat-secret'
    assert 'NEBIUS_API_KEY' not in child
    for key in ('FORGEFLOW_CONTROL_DATABASE_URL', 'FORGEFLOW_ODOO20_PASSWORD',
                'FORGEFLOW_LOCAL_ODOO_DB_PASSWORD', 'QDRANT_API_KEY', 'DASHSCOPE_API_KEY', 'SEMANTIC_API_KEY'):
        assert key not in child


def test_legacy_nebius_missing_search_key_reopens_setup_without_reset(files, payload, monkeypatch, capsys):
    from forgeflow.local_workbench.setup import main

    path, mounts = files
    original = config.save_configuration({**payload, 'profile': 'nemotron',
                                          'deepseek_search_api_key': 'search-key-original'}, path, mounts=mounts)
    original['env'].pop('DEEPSEEK_API_KEY')
    config.atomic_write(path, json.dumps(original).encode())
    monkeypatch.setenv('FORGEFLOW_LOCAL_CONFIG', str(path))
    assert main(['status']) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'provider_credentials_incomplete'
    fixed = config.save_configuration({'profile': 'nemotron',
                                       'deepseek_search_api_key': 'search-key-fixed'}, path, mounts=mounts)
    assert fixed['installation_id'] == original['installation_id']
    assert fixed['operator'] == original['operator']
    assert fixed['env']['NEBIUS_API_KEY'] == original['env']['NEBIUS_API_KEY']
    assert main(['status']) == 0


def test_connection_receipt_binds_model_key_and_capability(server, payload):
    state, token, request = server
    tested = json.loads(request('POST', '/test', json.dumps(payload))[2])
    assert tested['ok']
    for change in ({'model_api_key': 'replacement-chat-secret'}, {'profile': 'nemotron'}):
        status, _, raw = request('POST', '/configure', json.dumps({**payload, **change,
                                             'test_receipt': tested['test_receipt']}))
        assert status == 400
        assert json.loads(raw)['error'] == 'connection_test_required'
    assert not state.path.exists()
    record = state.test_receipts[tested['test_receipt']]
    import hashlib
    assert record[0] != hashlib.sha256(payload['model_api_key'].encode()).hexdigest()
    assert payload['model_api_key'] not in str(record)
    assert request('POST', '/configure', json.dumps({**payload, 'test_receipt': tested['test_receipt']}))[0] == 200


def test_no_receipt_or_expired_receipt_cannot_save(server, payload):
    state, _, request = server
    assert request('POST', '/configure', json.dumps(payload))[0] == 400
    tested = json.loads(request('POST', '/test', json.dumps(payload))[2])
    fingerprint, _ = state.test_receipts[tested['test_receipt']]
    state.test_receipts[tested['test_receipt']] = (fingerprint, 0)
    assert request('POST', '/configure', json.dumps({**payload, 'test_receipt': tested['test_receipt']}))[0] == 400
    assert not state.path.exists()


def test_new_endpoint_never_receives_previously_saved_chat_key(files, payload):
    path, mounts = files
    before = config.save_configuration(payload, path, mounts=mounts)
    with pytest.raises(config.ConfigError, match='valid_provider_keys_required'):
        config.prepare_model({'model_connection': {'preset': 'deepseek_flash',
                              'base_url': 'https://gateway.example/v1', 'custom_endpoint': True}}, before)
    normalized, key = config.prepare_model({'model_connection': {'preset': 'deepseek_flash',
                                           'base_url': 'https://gateway.example/v1', 'custom_endpoint': True},
                                           'model_api_key': 'gateway-own-secret'}, before)
    assert key == 'gateway-own-secret'
    assert not config.shared_deepseek_search(normalized)


def test_failed_connection_test_exposes_only_fixed_safe_error(server, payload):
    state, _, request = server
    async def failing(connection, key):
        return {'ok': False, 'error': 'model_tools_unsupported',
                'detail': 'raw provider body with a secret that must never be returned'}
    state.connection_tester = failing
    status, _, body = request('POST', '/test', json.dumps(payload))
    assert status == 200
    result = json.loads(body)
    assert result['ok'] is False and result['error'] == 'model_tools_unsupported'
    assert 'raw provider body' not in body.decode()
    assert 'test_receipt' not in result and not state.test_receipts
