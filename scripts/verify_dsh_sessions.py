#!/usr/bin/env python3
"""Opt-in real Nebius/DSH session and HTTP stream acceptance, with fake cases.

Uses the existing launcher for credentials and fresh temporary runtime state.
Only tool names, counters and boolean assertions are recorded; model text,
prompts, request headers, credentials and private session files are excluded.
No ERP, benchmark container or executor daemon is used.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import secrets
import shutil
import signal
import socket
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
import yaml

from forgeflow.api.app import create_app
from forgeflow.api.dsh_harness import DshHarness
from forgeflow.api.dsh_sdk import SdkDshHarness
from forgeflow.api.settings import Settings
from forgeflow.product import FakeProductExecutor

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {"mcp__forgeflow__" + name for name in (
    "list_cases", "get_case", "case_brief", "list_runs", "get_run", "search_evidence",
)}


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('/tmp/forgeflow-dsh-sessions.json'))
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Report already exists; choose a new output path to preserve evidence.')
    work = Path(tempfile.mkdtemp(prefix='forgeflow-v9-dsh-sessions-'))
    report = {'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
              'category': 'real_dsh_nebius_http_with_synthetic_owner_cases',
              'stages': [], 'requests': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    proxy = server = listener = None
    runtimes = []
    clients = []
    counters = {'ask_calls': 0, 'stream_calls': 0, 'runtime_closes': 0}

    def save():
        args.output.write_text(json.dumps(report, indent=2) + '\n')

    def stage(name, **values):
        report['stages'].append({'name': name, **values})
        save()
        print(json.dumps({'stage': name, **values}), flush=True)

    class ObservedHarness(SdkDshHarness):
        def with_evidence(self, evidence):
            scoped = super().with_evidence(evidence)
            runtimes.append(scoped)
            return scoped

        def ask(self, question, conversation_id=None):
            counters['ask_calls'] += 1
            return super().ask(question, conversation_id)

        def stream(self, question, conversation_id=None, **options):
            counters['stream_calls'] += 1
            yield from super().stream(question, conversation_id, **options)

        def close(self):
            if self._harness is not None:
                counters['runtime_closes'] += 1
            super().close()

    def read_frames(response):
        seq = None
        for line in response.iter_lines():
            if line.startswith('id: '):
                seq = int(line[4:])
            elif line.startswith('data: '):
                yield seq, json.loads(line[6:])

    def assert_ok(response):
        if response.status_code != 200:
            raise RuntimeError('unexpected_api_status')
        return response.json()

    def deadline(*_):
        raise TimeoutError('acceptance_deadline')

    try:
        launcher = module(ROOT / 'scripts/local_workbench.py', 'session_probe_launcher')
        env = dict(os.environ)
        with contextlib.redirect_stdout(io.StringIO()):
            launcher._prepare_dsh(env)
        if not env.get('NEBIUS_API_KEY'):
            raise RuntimeError('nebius_credential_unavailable')
        os.environ.update(env)
        setup = module(ROOT / 'scripts/setup_dsh_harness.py', 'session_probe_setup')
        setup.PATCH = work / 'base.json'
        setup._write_patch()
        rows = yaml.safe_load(setup.PATCH.read_text())
        provider = next(row for row in rows if row.get('id') == 'llm-pi-ai')['config']['providers']['nebius']
        upstream = urllib.parse.urlsplit(provider['baseURL'])
        if upstream.scheme != 'https' or upstream.hostname not in {
            'api.tokenfactory.nebius.com', 'api.studio.nebius.ai',
        }:
            raise RuntimeError('unreviewed_nebius_upstream')
        provider['models'][0]['defaultMaxTokens'] = 2048

        class Proxy(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                return

            def do_POST(self):
                try:
                    if self.path != '/v1/chat/completions':
                        self.send_error(404)
                        return
                    body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    request = json.loads(body)
                    tools = sorted(tool.get('function', {}).get('name') for tool in request.get('tools', []))
                    report['requests'].append({'tool_names': tools, 'model': request.get('model')})
                    save()
                    headers = {key: value for key, value in self.headers.items()
                               if key.lower() in {'authorization', 'content-type', 'accept'}}
                    target = urllib.parse.urlunsplit((upstream.scheme, upstream.netloc,
                        upstream.path.rstrip('/') + '/chat/completions', '', ''))
                    forwarded = urllib.request.Request(target, data=body, headers=headers, method='POST')
                    with urllib.request.urlopen(forwarded, timeout=180) as reply:
                        self.send_response(reply.status)
                        self.send_header('Content-Type', reply.headers.get('Content-Type', 'application/json'))
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        while chunk := reply.read1(65536):
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        self.close_connection = True
                except Exception as error:
                    report.setdefault('transport_error_types', []).append(type(error).__name__)
                    save()
                    self.close_connection = True

        proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        provider['baseURL'] = f'http://127.0.0.1:{proxy.server_port}/v1'
        setup.PATCH.write_text(json.dumps(rows))
        home = work / 'home'
        home.mkdir()
        (home / 'AGENTS.md').write_text(setup.HOME_AGENTS)
        base = ObservedHarness(cli=Path(env['FORGEFLOW_DSH_CLI']), patch=setup.PATCH,
            home=home, workspace=Path(env['FORGEFLOW_DSH_WORKSPACE']),
            node=env.get('FORGEFLOW_DSH_NODE', 'node'), timeout=180,
            sessions_file=work / 'unused-base-session-map.json')
        codes = (secrets.token_hex(16), secrets.token_hex(16))
        settings = Settings(data_root=work / 'records', approvals_root=work / 'state',
            session_secret=secrets.token_hex(32), invite_codes=codes, secure_cookie=False)
        fake = FakeProductExecutor()
        with patch.object(DshHarness, 'from_env', return_value=base):
            app = create_app(settings, product_executor=fake)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
            log_level='critical', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError('api_start_failed')
        client = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=180, trust_env=False)
        clients.append(client)
        auth = client.post('/api/auth/session', json={'invite_code': codes[0]})
        if auth.status_code != 201:
            raise RuntimeError('session_creation_failed')
        uid = auth.json()['uid']
        created = client.post('/api/product/cases', json={'scenario': 'A'})
        if created.status_code != 201:
            raise RuntimeError('fixture_create_failed')
        case = created.json()
        stage('configured', provider='nebius', model=provider['models'][0]['id'],
              owner_scoped_fixture=True, real_http=True, erp_used=False)
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(300)
        marker = 'session-marker-' + secrets.token_hex(6)
        first = assert_ok(client.post('/api/assistant/answer', json={
            'question': f'Remember the test marker {marker}. Use list_cases once, then give a one-sentence summary of visible cases.',
            'conversation_id': 'resume-probe',
        }))
        second = assert_ok(client.post('/api/assistant/answer', json={
            'question': 'What test marker did I provide in my previous message? Reply with that marker only.',
            'conversation_id': 'resume-probe',
        }))
        assert first['mode'] == second['mode'] == 'harness'
        assert marker in second['answer']
        current = runtimes[-1]
        current.close()
        third = assert_ok(client.post('/api/assistant/answer', json={
            'question': 'What test marker did I provide earlier? Reply with that marker only.',
            'conversation_id': 'resume-probe',
        }))
        assert third['mode'] == 'harness' and marker in third['answer']
        stage('session_resume', consecutive_turns=2, remembered_after_runtime_restart=True,
              same_session=first.get('session_id') == second.get('session_id') == third.get('session_id'))

        question = 'Use list_cases once to inspect the current case, then summarize its state in one sentence.'
        body = {'question': question, 'conversation_id': 'stream-probe', 'turn_id': 'original-turn'}
        stream_before = counters['stream_calls']
        request_before = len(report['requests'])
        disconnected = None
        with client.stream('POST', '/api/assistant/stream', json=body) as response:
            assert response.status_code == 200
            for seq, event in read_frames(response):
                if event.get('type') == 'trace':
                    disconnected = seq
                    break
        assert disconnected is not None
        with client.stream('POST', '/api/assistant/stream', json={**body, 'resume': True},
                           headers={'Last-Event-ID': str(disconnected)}) as response:
            reconnected = list(read_frames(response))
        final = next(event for _, event in reconnected if event.get('type') == 'final')
        assert all(seq > disconnected for seq, _ in reconnected)
        request_complete = len(report['requests'])
        with client.stream('POST', '/api/assistant/stream', json={**body, 'resume': True},
                           headers={'Last-Event-ID': str(disconnected)}) as response:
            replayed = list(read_frames(response))
        replay_final = next(event for _, event in replayed if event.get('type') == 'final')
        assert replay_final['answer'] == final['answer']
        assert len(report['requests']) == request_complete
        assert counters['stream_calls'] - stream_before == 1
        stage('http_sse_resume', disconnected_after_trace=True,
              one_producer=True, replay_no_provider_request=True,
              reconnect_event_ids_advance=True, upstream_requests=request_complete - request_before)

        case_stream = f'/api/product/cases/{case["case_id"]}/ask/stream'
        case_cancel = f'/api/product/cases/{case["case_id"]}/ask/cancel'
        case_body = {'question': 'Use get_case to inspect this case, then briefly explain its current state.',
                     'conversation_id': 'case-stream-probe', 'turn_id': 'case-original'}
        before_case = counters['stream_calls']
        with client.stream('POST', case_stream, json=case_body) as response:
            assert response.status_code == 200
            for case_seq, event in read_frames(response):
                if event.get('type') == 'trace':
                    break
        with client.stream('POST', case_stream, json={**case_body, 'resume': True},
                           headers={'Last-Event-ID': str(case_seq)}) as response:
            case_events = list(read_frames(response))
        case_final = next(event for _, event in case_events if event.get('type') == 'final')
        assert case_final['mode'] == 'harness'
        assert case_final['evidence_refs'][0]['id'] == case['case_id']
        complete_count = len(report['requests'])
        with client.stream('POST', case_stream, json={**case_body, 'resume': True}) as response:
            assert any(event.get('type') == 'final' for _, event in read_frames(response))
        assert counters['stream_calls'] - before_case == 1
        assert len(report['requests']) == complete_count
        assert client.post(case_stream, json={**case_body, 'turn_id': 'missing', 'resume': True}).status_code == 410
        assert len(report['requests']) == complete_count
        stage('case_http_sse_resume', one_producer=True, replay_no_provider_request=True,
              citation_bound_to_owned_case=True, missing_turn_no_restart=True)

        stop_body = {**case_body, 'turn_id': 'case-stop',
                     'question': 'Use list_cases and get_case to check this case, then describe its current state.'}
        with client.stream('POST', case_stream, json=stop_body) as response:
            assert response.status_code == 200
            for _, event in read_frames(response):
                if event.get('type') == 'trace':
                    break
        stopped = client.post(case_cancel, json={
            'conversation_id': case_body['conversation_id'], 'turn_id': stop_body['turn_id']})
        assert stopped.status_code == 200 and stopped.json()['cancelled'] is True
        assert stopped.json()['provider_interrupt_confirmed'] is False
        assert client.post(case_stream, json={**stop_body, 'resume': True}).status_code == 409
        replacement = {**case_body, 'turn_id': 'case-after-stop',
                       'question': 'Reply with exactly the word READY, without using tools.'}
        with client.stream('POST', case_stream, json=replacement) as response:
            after_stop = list(read_frames(response))
        replacement_final = next(event for _, event in after_stop if event.get('type') == 'final')
        assert 'READY' in replacement_final['answer']
        stage('case_stop_then_new_turn', stopped_turn_not_replayed=True,
              new_question_answered=True, provider_interrupt_claimed=False)

        old_runtime = runtimes[-1]
        old_scope = old_runtime.evidence_scope_path
        record_path = app.state.product_cases.path_for(case['case_id'])
        record = json.loads(record_path.read_text())
        assert record['uid'] == uid
        record['uid'] = 'synthetic-revoked-owner'
        record_path.write_text(json.dumps(record))
        assert client.post('/api/assistant/stream', json={**body, 'resume': True}).status_code == 410
        revoked_body = {'conversation_id': 'stream-probe', 'turn_id': 'after-revoke',
            'question': f'Call get_case with case_id {case["case_id"]} and report whether it is accessible. Do not rely on previous conversation evidence.'}
        with client.stream('POST', '/api/assistant/stream', json=revoked_body,
                           headers={'Last-Event-ID': str(disconnected)}) as response:
            after_revoke = list(read_frames(response))
        revoked_final = next(event for _, event in after_revoke if event.get('type') == 'final')
        new_runtime = runtimes[-1]
        denied = any(step.get('status') == 'failed' for _, event in after_revoke
                     for step in event.get('activity', []))
        assert old_runtime is not new_runtime and old_runtime._harness is None
        assert not old_scope.exists() and new_runtime.evidence_scope_path != old_scope
        assert revoked_final.get('session_id') != final.get('session_id')
        assert revoked_final['answer'] != final['answer']
        assert assert_ok(client.get('/api/product/cases')) == []
        stage('scope_revocation', old_snapshot_removed=True, old_runtime_closed=True,
              new_session=True, old_final_not_replayed=True, cross_owner_tool_denied=denied)
        assert denied
        all_scoped = bool(report['requests']) and all(set(row['tool_names']) == EXPECTED_TOOLS for row in report['requests'])
        assert all_scoped and fake.apply_calls == 0
        stage('final_assertions', every_request_exact_six_mcp=all_scoped,
              fake_apply_calls=fake.apply_calls, **counters)
        report['status'] = 'passed'
    except BaseException as error:
        report['status'] = 'failed'
        report['error_type'] = type(error).__name__
        print(json.dumps({'status': 'failed', 'error_type': type(error).__name__}), flush=True)
    finally:
        signal.alarm(0)
        for client in clients:
            client.close()
        if server is not None:
            server.should_exit = True
            thread.join(timeout=10)
        for runtime in runtimes:
            with contextlib.suppress(Exception):
                runtime.close()
        if listener is not None:
            listener.close()
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        report['request_count'] = len(report['requests'])
        report['runtime_closed'] = all(runtime._harness is None for runtime in runtimes)
        report['api_closed'] = server is None or not thread.is_alive()
        if report['runtime_closed'] and report['api_closed']:
            shutil.rmtree(work)
        report['temporary_data_removed'] = not work.exists()
        if not report['temporary_data_removed']:
            report['status'] = 'failed'
        save()
        print(json.dumps({'status': report.get('status'), 'report': str(args.output),
                          'request_count': report['request_count'],
                          'temporary_data_removed': report['temporary_data_removed']}), flush=True)
    return 0 if report.get('status') == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
