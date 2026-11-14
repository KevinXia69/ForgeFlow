"""A small paid provider probe, with no ERP data and no credential echoes."""
from __future__ import annotations

import asyncio

from forgeflow.model_transport import ModelTransport, TransportError

_ERRORS = {
    'model_runner_unauthorized': 'The provider rejected this API key or model permission.',
    'model_runner_rate_limited': 'The provider rate limit or account quota was reached.',
    'model_timeout': 'The provider did not finish the connection check in time.',
    'model_runner_unavailable': 'The model endpoint could not be reached.',
    'model_runner_error': 'The provider rejected this model or protocol request.',
    'model_runner_invalid_response': 'The endpoint returned an incompatible response.',
    'model_tools_unsupported': 'This model did not return the required tool call.',
    'model_tool_replay_failed': 'The model did not complete the tool result round trip.',
}


async def test_connection(connection: dict, api_key: str) -> dict:
    try:
        transport = ModelTransport(base_url=connection['base_url'], api_key=api_key,
            model=connection['model'], provider=connection['provider'], api=connection['api'], timeout=45)
        await asyncio.to_thread(transport.probe_tools)
    except TransportError as error:
        code = str(error)
        return {'ok': False, 'error': code if code in _ERRORS else 'connection_test_failed',
                'detail': _ERRORS.get(code, 'The connection check failed.')}
    except Exception:
        return {'ok': False, 'error': 'connection_test_failed', 'detail': 'The connection check failed.'}
    return {'ok': True, 'model': connection['model'], 'protocol': connection['api'],
            'tools': True, 'detail': 'The provider completed a tool call and returned its result.'}
