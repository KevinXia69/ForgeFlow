"""Reject malformed, text-only, oversized and nonterminating provider responses."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from forgeflow.api.model_settings import ModelSettingsError, build_settings
from forgeflow.local_workbench.connection_test import test_connection as run_connection_test
from forgeflow.model_transport import ModelTransport, TransportError

APIS = ('openai-completions', 'openai-responses', 'anthropic-messages', 'google-generative-ai')


def transport(api='openai-completions', handler=None, **kwargs):
    factory = None if handler is None else lambda: httpx.Client(transport=httpx.MockTransport(handler))
    return ModelTransport(base_url='https://provider.invalid/v1', api_key='synthetic-credential',
                          model='model', api=api, client_factory=factory, **kwargs)


@pytest.mark.parametrize('api,payload', [
    ('openai-completions', {'choices': [{'message': 'unexpected'}]}),
    ('openai-responses', {'output': ['unexpected']}),
    ('anthropic-messages', {'content': ['unexpected']}),
    ('google-generative-ai', {'candidates': [{'content': 'unexpected'}]}),
])
def test_malformed_provider_structures_have_safe_errors(api, payload):
    with pytest.raises(TransportError, match='^model_runner_invalid_response$'):
        transport(api).parse(payload)
    with pytest.raises(TransportError, match='^model_tools_unsupported$'):
        transport(api, lambda _: httpx.Response(200, json=payload)).probe_tools()


@pytest.mark.parametrize('api,payload', [
    ('openai-completions', {'choices': [{'message': {'content': 'I can use tools.'}}]}),
    ('openai-responses', {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'I can use tools.'}]}]}),
    ('anthropic-messages', {'content': [{'type': 'text', 'text': 'I can use tools.'}]}),
    ('google-generative-ai', {'candidates': [{'content': {'parts': [{'text': 'I can use tools.'}]}}]}),
])
def test_plain_text_cannot_pass_tool_probe(api, payload):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)
    with pytest.raises(TransportError, match='^model_tools_unsupported$'):
        transport(api, handler).probe_tools()
    assert len(requests) == 1


@pytest.mark.parametrize('call_id,args,name', [('', '{}', 'forgeflow_connection_check'),
    (None, '{}', 'forgeflow_connection_check'), ('call', '{"unexpected":1}', 'forgeflow_connection_check'),
    ('call', '{}', 'wrong_tool')])
def test_invalid_tool_calls_are_not_replayed(call_id, args, name):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'tool_calls': [
            {'id': call_id, 'function': {'name': name, 'arguments': args}}]}}]})
    with pytest.raises(TransportError, match='^model_tools_unsupported$'):
        transport(handler=handler).probe_tools()
    assert len(requests) == 1


def test_returning_nonce_with_another_tool_call_is_not_completed():
    count = 0
    call = {'id': 'call', 'function': {'name': 'forgeflow_connection_check', 'arguments': '{}'}}
    def handler(request):
        nonlocal count
        count += 1
        body = json.loads(request.content)
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant',
            'tool_calls': [call], 'content': None if count == 1 else body['messages'][-1]['content']}}]})
    with pytest.raises(TransportError, match='^model_tool_replay_failed$'):
        transport(handler=handler).probe_tools()
    assert count == 2


def test_slow_progress_has_a_total_deadline(monkeypatch):
    clock = [0.0]
    closed = []
    monkeypatch.setattr('forgeflow.model_transport.time.monotonic', lambda: clock[0])
    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(1000):
                clock[0] += 0.3
                yield b' '
        def close(self):
            closed.append(True)
    instance = transport(handler=lambda _: httpx.Response(200, stream=Trickle()), timeout=1)
    with pytest.raises(TransportError, match='^model_timeout$'):
        instance.complete([{'role': 'user', 'content': 'hello'}])
    assert clock[0] < 2 and closed == [True]


def test_response_budget_stops_stream_and_closes(monkeypatch):
    monkeypatch.setattr('forgeflow.model_transport.MAX_RESPONSE_BYTES', 16)
    closed = []
    class Oversized(httpx.SyncByteStream):
        def __iter__(self):
            yield b'x' * 17
            pytest.fail('oversized response must not continue reading')
        def close(self):
            closed.append(True)
    with pytest.raises(TransportError, match='^model_runner_invalid_response$'):
        transport(handler=lambda _: httpx.Response(200, stream=Oversized())).complete([])
    assert closed == [True]


def test_read_timeout_is_safe_and_explicit():
    def handler(request):
        assert request.extensions['timeout'] == {'connect': 3, 'read': 3, 'write': 3, 'pool': 3}
        raise httpx.ReadTimeout('provider returned a confidential response')
    with pytest.raises(TransportError, match='^model_timeout$'):
        transport(handler=handler, timeout=3).complete([])


@pytest.mark.parametrize('api', [None, [], {}, 3])
def test_nonstring_api_is_validation_error(api):
    with pytest.raises(ModelSettingsError, match='invalid_model_protocol'):
        build_settings('custom', 'https://provider.invalid', 'm', 'synthetic-credential', api=api)
    with pytest.raises(TransportError, match='unsupported_model_protocol'):
        transport(api)


@pytest.mark.parametrize('error', [TransportError('provider secret body'), RuntimeError('provider secret body')])
def test_connection_failure_never_echoes_unknown_exception(monkeypatch, error):
    def fail(_):
        raise error
    monkeypatch.setattr(ModelTransport, 'probe_tools', fail)
    result = asyncio.run(run_connection_test({'base_url': 'https://provider.invalid', 'model': 'm',
                                       'provider': 'custom', 'api': 'openai-completions'}, 'synthetic-credential'))
    assert result == {'ok': False, 'error': 'connection_test_failed', 'detail': 'The connection check failed.'}


def test_deepseek_anthropic_probe_disables_thinking_but_worker_preserves_max():
    instance = ModelTransport(base_url='https://api.deepseek.com/anthropic/v1',
                              api_key='synthetic-credential', model='deepseek-flash',
                              provider='deepseek', api='anthropic-messages')
    probe = instance.body([{'role': 'user', 'content': 'test'}], probe=True)
    assert probe['thinking'] == {'type': 'disabled'}
    assert probe['tool_choice']['type'] == 'tool'
    normal = instance.body([{'role': 'user', 'content': 'test'}])
    assert normal['thinking'] == {'type': 'enabled'}
    assert normal['output_config'] == {'effort': 'max'}
    assert 'thinking' not in transport('anthropic-messages').body([])


def test_deepseek_responses_probe_and_worker_reasoning_contract():
    instance = ModelTransport(base_url='https://api.deepseek.com/v1',
                              api_key='synthetic-credential', model='deepseek-flash',
                              provider='deepseek', api='openai-responses')
    assert instance.body([], probe=True)['reasoning'] == {'effort': 'none'}
    assert instance.body([])['reasoning'] == {'effort': 'max'}
    assert 'reasoning' not in transport('openai-responses').body([])
