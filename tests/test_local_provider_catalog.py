"""Explicit pinned protocols and authority-safe draft model configuration."""
from __future__ import annotations

import pytest

from forgeflow.local_workbench.providers import (
    CHAT_KEY_ENV,
    EXPLICIT_APIS,
    PROVIDER_PRESETS,
    ProviderConfigError,
    connection_environment,
    normalize_base_url,
    normalize_connection,
)


@pytest.mark.parametrize('preset', [name for name in PROVIDER_PRESETS if name != 'custom'])
def test_presets_keep_their_real_provider_and_protocol(preset):
    value = normalize_connection({'preset': preset})
    assert value['provider'] == PROVIDER_PRESETS[preset]['provider']
    assert value['api'] == PROVIDER_PRESETS[preset]['api']
    assert value['catalog_native'] == (preset == 'google')
    env = connection_environment(value, 'synthetic-provider-credential')
    assert env[CHAT_KEY_ENV] == 'synthetic-provider-credential'
    assert env['FORGEFLOW_MODEL_API'] == value['api']
    assert env['FORGEFLOW_ASSISTANT_PROVIDER'] == value['provider']
    assert 'DEEPSEEK_API_KEY' not in env


@pytest.mark.parametrize('api', EXPLICIT_APIS)
def test_custom_routes_preserve_selected_wire_protocol(api):
    value = normalize_connection({'preset': 'custom', 'api': api, 'base_url': 'https://gateway.example/v1',
                                  'model': 'example-model', 'custom_endpoint': True})
    assert value['api'] == api and value['input'] == ['text']
    assert value['provider'] == 'forgeflow-custom'


@pytest.mark.parametrize('api', ['google-generative-ai', 'bedrock-converse-stream', 'google-vertex',
                                 'azure-openai-responses', 'invented-protocol'])
def test_unimplemented_custom_auth_and_protocols_are_not_offered(api):
    with pytest.raises(ProviderConfigError, match='unsupported_model_protocol'):
        normalize_connection({'preset': 'custom', 'api': api, 'base_url': 'https://gateway.example/v1',
                              'model': 'example-model', 'custom_endpoint': True})


@pytest.mark.parametrize('url', ['https://user:secret@example.com/v1', 'https://example.com/v1#key',
    'https://example.com/v1?key=secret', 'http://example.com/v1', 'http://192.168.1.7/v1',
    'https://169.254.169.254/v1', 'https://0.0.0.0/v1', 'file:///tmp/model',
    'https://example.com\\@elsewhere/v1', 'https://example.com/\nheader'])
def test_unsafe_destinations_are_rejected(url):
    with pytest.raises(ProviderConfigError):
        normalize_base_url(url)


@pytest.mark.parametrize('url', ['http://127.0.0.1:11434/v1', 'http://[::1]:11434/v1',
                                 'http://host.docker.internal:11434/v1'])
def test_http_is_limited_to_explicit_local_addresses(url):
    assert normalize_base_url(url) == url


def test_changed_authority_requires_explicit_consent_and_has_no_implicit_vision():
    draft = {'preset': 'openai', 'base_url': 'https://api.openai.com.untrusted.example/v1'}
    with pytest.raises(ProviderConfigError, match='custom_endpoint_confirmation_required'):
        normalize_connection(draft)
    value = normalize_connection({**draft, 'custom_endpoint': True})
    assert value['input'] == ['text']
    assert normalize_connection({'preset': 'openai', 'model': 'unknown-model'})['input'] == ['text']


def test_google_native_and_compatible_are_explicit_distinct_routes():
    native = normalize_connection({'preset': 'google'})
    compatible = normalize_connection({'preset': 'google', 'api': 'openai-completions'})
    assert native['catalog_native'] and not compatible['catalog_native']
    assert native['base_url'].endswith('/v1beta')
    assert compatible['base_url'].endswith('/v1beta/openai')


@pytest.mark.parametrize('api', EXPLICIT_APIS)
def test_deepseek_preserves_all_three_verified_protocols(api):
    value = normalize_connection({'preset': 'deepseek_flash', 'api': api})
    assert value['api'] == api and value['provider'] == 'deepseek'
    assert value['model'] == 'deepseek-flash'
    assert value['base_url'] == ('https://api.deepseek.com/anthropic' if api == 'anthropic-messages'
                                 else 'https://api.deepseek.com/v1')
    assert normalize_connection({'preset': 'deepseek_flash'})['api'] == 'openai-completions'


def test_ollama_cannot_reuse_public_placeholder_for_remote_service():
    with pytest.raises(ProviderConfigError, match='ollama_local_endpoint_required'):
        normalize_connection({'preset': 'ollama', 'base_url': 'https://remote.example/v1',
                              'custom_endpoint': True})


@pytest.mark.parametrize("preset,path", [
    ("deepseek_flash", "/anthropic/v1/messages"),
    ("anthropic", "/v1/messages"),
    ("openrouter", "/api/v1/messages"),
])
def test_anthropic_base_is_the_sdk_root_not_the_messages_version_prefix(preset, path):
    from urllib.parse import urlsplit

    value = normalize_connection({"preset": preset, "api": "anthropic-messages"})
    # Pinned Anthropic SDK appends /v1/messages; an extra /v1 breaks Harness
    # even though the direct HTTP connection tester tolerates that suffix.
    assert urlsplit(value["base_url"] + "/v1/messages").path == path
