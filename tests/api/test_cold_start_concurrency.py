"""First concurrent API reads must not initialize the control package in workers."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_cold_app_initializes_control_imports_before_concurrent_first_reads(tmp_path):
    # A child process matters: ordinary pytest collection imports the package in
    # unrelated tests, concealing the production cold-start import-lock race.
    code = r'''
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

assert 'forgeflow.superagent' not in sys.modules
from fastapi.testclient import TestClient
from forgeflow.api.app import create_app
from forgeflow.api.auth import issue_cookie
from forgeflow.api.settings import create_settings
root = Path(sys.argv[1])
settings = create_settings({
    'FORGEFLOW_DATA_ROOT': str(root / 'runs'),
    'FORGEFLOW_APPROVALS_ROOT': str(root / 'approvals'),
    'FORGEFLOW_SESSION_SECRET': 'synthetic-cold-start-secret-0123456789',
    'FORGEFLOW_INVITE_CODES': 'synthetic-cold-start-invite',
    'FORGEFLOW_SECURE_COOKIE': 'false',
})
app = create_app(settings)
# The real factory, not a test-only import/warmup, owns initialization. This
# assertion deterministically detects a regression even if scheduling happens
# to avoid the deadlock during the following concurrent HTTP requests.
for name in ('forgeflow.superagent', 'forgeflow.superagent.bindings',
             'forgeflow.superagent.store', 'forgeflow.superagent.subscriptions',
             'forgeflow.superagent.source_runner', 'forgeflow.superagent.tasks'):
    module = sys.modules.get(name)
    assert module is not None and not module.__spec__._initializing, name
paths = ['/api/sources/health', '/api/sources/subscriptions',
         '/api/runtime/health', '/api/odoo20/tasks'] * 4
barrier = Barrier(len(paths))
token = issue_cookie(settings, settings.invite_codes[0], audience='testserver')
with TestClient(app, base_url='http://testserver') as client:
    def read(path):
        barrier.wait(timeout=10)
        response = client.get(path, headers={'Cookie': settings.cookie_name + '=' + token})
        assert response.status_code == 200, (path, response.status_code)
        value = response.json()
        assert value == ({'healthy': False, 'subscriptions': 0, 'last_scan_at': None}
                         if path == '/api/sources/health' else []), path
    with ThreadPoolExecutor(max_workers=len(paths)) as pool:
        futures = [pool.submit(read, path) for path in paths]
        for future in futures:
            future.result(timeout=10)
'''
    repo = Path(__file__).resolve().parents[2]
    # No live database, ERP, account, model or deployment environment enters the
    # cold process. Only synthetic stores under pytest's temporary root exist.
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(repo / "src")}
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=repo,
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
