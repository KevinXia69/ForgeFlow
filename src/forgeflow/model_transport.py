"""Bounded JSON transport for the four model protocols exposed by the workbench.

Chat/tool sessions use the pinned Harness adapter. This smaller transport serves
connection probes and the supervisor's single structured completion; errors never
include provider bodies, credentials or endpoint URLs.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import httpx

APIS = frozenset({'openai-completions', 'openai-responses', 'anthropic-messages', 'google-generative-ai'})
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class TransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    text: str
    usage: dict[str, Any]
    raw: dict[str, Any]


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


class ModelTransport:
    def __init__(self, *, base_url: str, api_key: str, model: str,
                 api: str = 'openai-completions', provider: str = 'openai-compatible',
                 timeout: float = 120, client_factory: Callable | None = None):
        if not isinstance(api, str) or api not in APIS:
            raise TransportError('unsupported_model_protocol')
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise TransportError('invalid_model_timeout')
        self.base_url, self.api_key, self.model = base_url.rstrip('/'), api_key, model
        self.api, self.provider, self.timeout = api, provider, timeout
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=timeout, trust_env=False))

    def _request(self, body: dict) -> dict:
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        suffix = {'openai-completions': '/chat/completions', 'openai-responses': '/responses',
                  'anthropic-messages': '/messages' if self.base_url.endswith('/v1') else '/v1/messages',
                  'google-generative-ai': '/models/' + quote(self.model, safe='-._') + ':generateContent'}[self.api]
        if self.api == 'anthropic-messages':
            headers.update({'x-api-key': self.api_key, 'anthropic-version': '2023-06-01'})
        elif self.api == 'google-generative-ai':
            headers['x-goog-api-key'] = self.api_key
        else:
            headers['Authorization'] = 'Bearer ' + self.api_key
        client = self.client_factory()
        deadline = time.monotonic() + self.timeout
        try:
            with client.stream('POST', self.base_url + suffix, headers=headers, json=body,
                               follow_redirects=False, timeout=self.timeout) as response:
                if time.monotonic() >= deadline:
                    raise TransportError('model_timeout')
                if response.status_code in (401, 403):
                    raise TransportError('model_runner_unauthorized')
                if response.status_code == 429:
                    raise TransportError('model_runner_rate_limited')
                if response.status_code != 200:
                    raise TransportError('model_runner_error')
                data = bytearray()
                for chunk in response.iter_bytes():
                    # Read timeouts alone reset for every chunk. Stop a peer
                    # that keeps the connection alive with an endless trickle;
                    # any in-flight read is still bounded by self.timeout.
                    if time.monotonic() >= deadline:
                        raise TransportError('model_timeout')
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise TransportError('model_runner_invalid_response')
                    data.extend(chunk)
            payload = json.loads(data)
            if not isinstance(payload, dict):
                raise TransportError('model_runner_invalid_response')
            return payload
        except httpx.TimeoutException:
            raise TransportError('model_timeout') from None
        except httpx.HTTPError:
            raise TransportError('model_runner_unavailable') from None
        except (ValueError, UnicodeError, RecursionError):
            raise TransportError('model_runner_invalid_response') from None
        finally:
            client.close()

    def body(self, messages: list[dict], *, probe: bool = False) -> dict:
        tool = {'name': 'forgeflow_connection_check', 'description': 'Return a connection check nonce.',
                'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
        if self.api == 'openai-completions':
            body = {'model': self.model, 'messages': messages}
            if self.provider == 'deepseek' and self.model in {'deepseek-flash', 'deepseek-v4-pro'}:
                body.update(thinking={'type': 'disabled'} if probe else {'type': 'enabled'})
                if not probe:
                    body['reasoning_effort'] = 'max'
            if probe:
                body.update(tools=[{'type': 'function', 'function': tool}], tool_choice={
                    'type': 'function', 'function': {'name': tool['name']}})
            return body
        if self.api == 'openai-responses':
            body = {'model': self.model, 'input': messages, 'max_output_tokens': 8192}
            if self.provider == 'deepseek' and self.model in {'deepseek-flash', 'deepseek-v4-pro'}:
                body['reasoning'] = {'effort': 'none' if probe else 'max'}
            if probe:
                body.update(tools=[{'type': 'function', **tool}],
                            tool_choice={'type': 'function', 'name': tool['name']})
            return body
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        normal = [m for m in messages if m['role'] != 'system']
        if self.api == 'anthropic-messages':
            body = {'model': self.model, 'messages': normal, 'max_tokens': 8192}
            if self.provider == 'deepseek' and self.model in {'deepseek-flash', 'deepseek-v4-pro'}:
                body['thinking'] = {'type': 'disabled' if probe else 'enabled'}
                if not probe:
                    body['output_config'] = {'effort': 'max'}
            if system:
                body['system'] = system
            if probe:
                body.update(tools=[{'name': tool['name'], 'description': tool['description'],
                                    'input_schema': tool['parameters']}],
                            tool_choice={'type': 'tool', 'name': tool['name']})
            return body
        body = {'contents': [{'role': 'model' if m['role'] == 'assistant' else 'user',
                              'parts': [{'text': m['content']}]} for m in normal],
                'generationConfig': {'maxOutputTokens': 8192}}
        if system:
            body['systemInstruction'] = {'parts': [{'text': system}]}
        if probe:
            body.update(tools=[{'functionDeclarations': [tool]}], toolConfig={
                'functionCallingConfig': {'mode': 'ANY', 'allowedFunctionNames': [tool['name']]}})
        return body

    def parse(self, payload: dict) -> Completion:
        try:
            if self.api == 'openai-completions':
                text = payload['choices'][0]['message'].get('content') or ''
                raw_usage = payload.get('usage') or {}
                prompt, output, total = (raw_usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
            elif self.api == 'openai-responses':
                text = '\n'.join(block['text'] for item in payload.get('output', [])
                    if item.get('type') == 'message' for block in item.get('content', [])
                    if block.get('type') == 'output_text')
                raw_usage = payload.get('usage') or {}
                prompt, output, total = (raw_usage.get(k) for k in ('input_tokens', 'output_tokens', 'total_tokens'))
            elif self.api == 'anthropic-messages':
                text = '\n'.join(block['text'] for block in payload['content'] if block.get('type') == 'text')
                raw_usage = payload.get('usage') or {}
                values = [_count(raw_usage.get(k)) for k in ('input_tokens', 'output_tokens')]
                prompt, output = values
                cached = sum(_count(raw_usage.get(k)) or 0 for k in ('cache_creation_input_tokens', 'cache_read_input_tokens'))
                prompt = prompt + cached if prompt is not None else None
                total = prompt + output if prompt is not None and output is not None else None
            else:
                text = '\n'.join(part['text'] for part in payload['candidates'][0]['content'].get('parts', [])
                                 if 'text' in part and not part.get('thought'))
                raw_usage = payload.get('usageMetadata') or {}
                prompt, output, total = (raw_usage.get(k) for k in ('promptTokenCount', 'candidatesTokenCount', 'totalTokenCount'))
                if _count(output) is not None:
                    output += _count(raw_usage.get('thoughtsTokenCount')) or 0
            if not isinstance(text, str):
                raise ValueError
            receipt = {'tokens': _count(total) or 0, 'prompt_tokens': _count(prompt) or 0,
                       'completion_tokens': _count(output) or 0, 'complete': _count(total) is not None,
                       'model': str(payload.get('model') or payload.get('modelVersion') or self.model),
                       'provider': self.provider}
            request_id = payload.get('id') or payload.get('responseId')
            if isinstance(request_id, str):
                receipt['request_id'] = request_id
            return Completion(text.strip(), receipt, payload)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            raise TransportError('model_runner_invalid_response') from None

    def complete(self, messages: list[dict]) -> Completion:
        result = self.parse(self._request(self.body(messages)))
        if not result.text:
            raise TransportError('model_runner_invalid_response')
        return result

    def probe_tools(self) -> None:
        """Exercise one harmless tool request and replay, including native metadata."""
        import secrets
        nonce = secrets.token_hex(8)
        messages = [{'role': 'user', 'content': 'Call forgeflow_connection_check once. Then reply with only its returned nonce.'}]
        body = self.body(messages, probe=True)
        payload = self._request(body)
        name = 'forgeflow_connection_check'
        try:
            if self.api == 'openai-completions':
                message = payload['choices'][0]['message']
                call, = message['tool_calls']
                if (call['function']['name'] != name or json.loads(call['function']['arguments']) != {}
                        or not isinstance(call['id'], str) or not call['id']):
                    raise ValueError
                body['messages'] += [message, {'role': 'tool', 'tool_call_id': call['id'], 'content': nonce}]
                body['tool_choice'] = 'none'
            elif self.api == 'openai-responses':
                call, = [item for item in payload['output'] if item.get('type') == 'function_call']
                if (call['name'] != name or json.loads(call['arguments']) != {}
                        or not isinstance(call['call_id'], str) or not call['call_id']):
                    raise ValueError
                body['input'] += payload['output'] + [{'type': 'function_call_output', 'call_id': call['call_id'], 'output': nonce}]
                body['tool_choice'] = 'none'
            elif self.api == 'anthropic-messages':
                call, = [item for item in payload['content'] if item.get('type') == 'tool_use']
                if (call['name'] != name or call['input'] != {}
                        or not isinstance(call['id'], str) or not call['id']):
                    raise ValueError
                body['messages'] += [{'role': 'assistant', 'content': payload['content']},
                    {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': call['id'], 'content': nonce}]}]
                body['tool_choice'] = {'type': 'none'}
            else:
                content = payload['candidates'][0]['content']
                call, = [part['functionCall'] for part in content['parts'] if 'functionCall' in part]
                if call['name'] != name or call.get('args', {}) != {}:
                    raise ValueError
                body['contents'] += [content, {'role': 'user', 'parts': [{'functionResponse': {
                    'name': name, 'response': {'nonce': nonce}, **({'id': call['id']} if call.get('id') else {})}}]}]
                body['toolConfig'] = {'functionCallingConfig': {'mode': 'NONE'}}
        except (AttributeError, KeyError, IndexError, ValueError, TypeError, RecursionError):
            raise TransportError('model_tools_unsupported') from None
        final = self._request(body)
        reply = self.parse(final)
        try:
            if self.api == 'openai-completions':
                pending = bool(final['choices'][0]['message'].get('tool_calls'))
            elif self.api == 'openai-responses':
                pending = any(item.get('type') == 'function_call' for item in final['output'])
            elif self.api == 'anthropic-messages':
                pending = any(item.get('type') == 'tool_use' for item in final['content'])
            else:
                pending = any('functionCall' in part for part in final['candidates'][0]['content']['parts'])
        except (AttributeError, KeyError, IndexError, TypeError):
            raise TransportError('model_tool_replay_failed') from None
        if pending:
            raise TransportError('model_tool_replay_failed')
        if reply.text != nonce:
            raise TransportError('model_tool_replay_failed')
