"""Opt-in startup checks against a built API image, without model or ERP calls.

FORGEFLOW_TEST_API_IMAGE=forgeflow-api:local python -m pytest -q \
    tests/test_deploy_container_startup.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

IMAGE = os.environ.get("FORGEFLOW_TEST_API_IMAGE", "")
pytestmark = pytest.mark.skipif(not IMAGE, reason="set FORGEFLOW_TEST_API_IMAGE to a built API image")
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SYNTHETIC_ENV = {
    "FORGEFLOW_SESSION_SECRET": "synthetic-container-startup-secret-0123456789",
    "FORGEFLOW_INVITE_CODES": "synthetic-container-startup-invite",
    "FORGEFLOW_SECURE_COOKIE": "false",
}


def _docker(*args: str, check: bool = True):
    command = ["docker"]
    if context := os.environ.get("FORGEFLOW_TEST_DOCKER_CONTEXT"):
        command += ["--context", context]
    return subprocess.run(command + list(args), capture_output=True, text=True,
                          check=check, timeout=90)


def _assert_started(container: str, *, read_only: bool):
    deadline = time.monotonic() + 45
    health = "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status == 200"
    while time.monotonic() < deadline:
        result = _docker("exec", container, "python", "-c", health, check=False)
        if result.returncode == 0:
            break
        time.sleep(0.25)
    else:
        pytest.fail(_docker("logs", container, check=False).stdout + result.stderr)
    inspection = json.loads(_docker("inspect", container).stdout)[0]
    assert inspection["HostConfig"]["ReadonlyRootfs"] is read_only
    # Defaults must be writable for the image's non-root identity, without
    # weakening the recorded-runs mount or disabling the assistant runtime.
    code = """
import os
from pathlib import Path
import forgeflow.api.dsh_harness
from forgeflow.api.dsh_harness import DshHarness
assert os.getuid() == 10001
assert os.environ['FORGEFLOW_DSH_ENABLED'] == '1'
harness = DshHarness.from_env()
assert type(harness).__name__ == 'SdkDshHarness'
assert harness.configured()
assert os.access(harness._wrapper(), os.X_OK)
assert Path(forgeflow.api.dsh_harness.__file__).with_name('dsh_image_guard.mjs').is_file()
for key in ('FORGEFLOW_DSH_HOME', 'FORGEFLOW_DSH_WORKSPACE', 'FORGEFLOW_ATTACHMENTS_ROOT'):
    path = Path(os.environ[key])
    assert path.is_dir(), key
    assert path.stat().st_uid == 10001, key
    (path / 'startup-probe').write_text('writable', encoding='utf-8')
"""
    code += r"""
import http.cookiejar
import json
import urllib.request
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
origin = 'http://127.0.0.1:8000'
login = urllib.request.Request(origin + '/api/auth/session',
    data=json.dumps({'invite_code': 'synthetic-container-startup-invite'}).encode(),
    headers={'Content-Type': 'application/json'})
assert opener.open(login, timeout=5).status == 201
payload = (b'--startup-boundary\r\nContent-Disposition: form-data; name="file"; filename="probe.txt"\r\n'
           b'Content-Type: text/plain\r\n\r\ncontainer-only-attachment-probe\r\n--startup-boundary--\r\n')
upload = urllib.request.Request(origin + '/api/attachments', data=payload,
    headers={'Content-Type': 'multipart/form-data; boundary=startup-boundary'})
with opener.open(upload, timeout=5) as response:
    assert response.status == 200
    assert json.load(response)['size'] == len(b'container-only-attachment-probe')
"""
    if read_only:
        code += """
try:
    Path('/data/runs/startup-probe').write_text('must not succeed', encoding='utf-8')
except OSError:
    pass
else:
    raise AssertionError('recorded runs unexpectedly writable')
"""
    _docker("exec", container, "python", "-c", code)
    _assert_mcp_tool_roundtrip(container)
    _assert_text_document_conversion(container)


def _assert_text_document_conversion(container: str):
    # Check the actual immutable runtime as UID 10001, including the read-only
    # Compose variant: converters, Chinese fonts and writable temporary output.
    code = r"""
import re
import tempfile
from pathlib import Path

from docx import Document
from pypdf import PdfReader
from forgeflow.knowledge.docmind_convert import prepare, _soffice_pdf

sources = {
    'txt': '采购审批：预算 120000 元\n# 原文标记\n继电器 DC24，数量 6\n',
    'md': '# 采购审批\n\n预算 120000 元\n\n| 物料 | 数量 |\n| --- | --- |\n| 继电器 | 6 |\n',
    'html': '<h1>采购审批</h1><p>预算 120000 元</p><table><tr><th>物料</th><th>数量</th></tr>'
            '<tr><td>继电器</td><td>6</td></tr></table>',
}
with tempfile.TemporaryDirectory(prefix='conversion-startup-') as directory:
    root = Path(directory)
    for extension, text in sources.items():
        source = root / ('input.' + extension)
        source.write_text(text, encoding='utf-8')
        prepared = prepare(source, '采购资料.' + extension, root / extension, target='bailian')
        assert prepared.converted and prepared.extension == 'docx'
        document = Document(prepared.upload_path)
        if extension == 'txt':
            assert '\n'.join(p.text for p in document.paragraphs) == text
        else:
            assert document.tables[0].cell(1, 0).text == '继电器'
        pdf = PdfReader(_soffice_pdf(prepared.upload_path, prepared.upload_path.parent))
        extracted = re.sub(r'\s+', '', '\n'.join(page.extract_text() or '' for page in pdf.pages))
        assert all(value in extracted for value in ('采购审批', '预算120000元', '继电器', '6')), extracted
        fonts = [str(font.get_object().get('/BaseFont', '')) for page in pdf.pages
                 for font in page['/Resources']['/Font'].values()]
        assert any('Noto' in font and 'CJK' in font for font in fonts), fonts
"""
    result = _docker("exec", container, "python", "-c", code, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def _assert_mcp_tool_roundtrip(container: str):
    # Exercise the command recorded in the image's generated composition. A
    # healthy API and configured harness do not prove its MCP subprocess starts.
    code = r"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

patch = json.loads(Path(os.environ['FORGEFLOW_DSH_PATCH']).read_text())
server, = [module['config'] for operation in patch
           for module in operation.get('insert', [])
           if module.get('id') == 'mcp-forgeflow']
assert server['transport'] == 'stdio'
assert server['failOnStartupError'] is True
requests = [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2024-11-05', 'capabilities': {},
        'clientInfo': {'name': 'container-startup-probe', 'version': '1'},
    }},
    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
        'name': 'workspace_summary', 'arguments': {'limit': 0},
    }},
]
with tempfile.TemporaryDirectory(prefix='mcp-startup-') as directory:
    evidence = Path(directory) / 'evidence.json'
    evidence.write_text(json.dumps({
        'schema_version': 1, 'scope_id': 'synthetic-container-scope',
        'cases': [
            {'case_id': 'synthetic-decision', 'state': 'awaiting_approval'},
            {'case_id': 'synthetic-executing', 'state': 'executing'},
        ],
        'runs': [{'run_id': 'synthetic-run', 'strict_success': True,
                  'evaluation_valid': True}],
    }))
    result = subprocess.run(
        [server['command'], *server['args']], cwd=server['cwd'],
        env={**os.environ, **server['env'], 'FORGEFLOW_ASSISTANT_EVIDENCE': str(evidence)},
        input=''.join(json.dumps(request) + '\n' for request in requests),
        text=True, capture_output=True, timeout=15,
    )
assert result.returncode == 0, result.stderr
responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
assert [response['id'] for response in responses] == [1, 2, 3], responses
assert responses[0]['result']['serverInfo']['name'] == 'forgeflow-evidence'
assert responses[0]['result']['capabilities'] == {'tools': {}}
tools = {tool['name'] for tool in responses[1]['result']['tools']}
assert tools == {'workspace_summary', 'list_cases', 'case_brief', 'get_case',
                 'list_runs', 'get_run', 'search_evidence', 'project_knowledge'}, tools
assert responses[2]['result']['isError'] is False, responses[2]
summary = json.loads(responses[2]['result']['content'][0]['text'])
assert summary['total_cases'] == 2, summary
assert summary['counts'] == {'decision': 1, 'executing': 1, 'verified': 0, 'evidence': 1}, summary
assert summary['verified_historical_runs'] == 1, summary
assert summary['cases'] == [], summary
"""
    result = _docker("exec", container, "python", "-c", code, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_default_image_starts_as_non_root_with_writable_assistant_state():
    name = f"forgeflow-startup-default-{uuid.uuid4().hex[:12]}"
    args = ["run", "-d", "--name", name]
    for key, value in SYNTHETIC_ENV.items():
        args += ["-e", f"{key}={value}"]
    try:
        _docker(*args, IMAGE)
        _assert_started(name, read_only=False)
    finally:
        _docker("rm", "-f", name, check=False)


def test_read_only_compose_starts_with_fresh_owned_state_volumes(tmp_path):
    project = f"forgeflow-startup-compose-{uuid.uuid4().hex[:12]}"
    config = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    service = config["services"]["api"]
    service.pop("build")
    service.pop("env_file")
    service.pop("ports")
    service["image"] = IMAGE
    service["restart"] = "no"
    service["environment"] = {
        **SYNTHETIC_ENV, "FORGEFLOW_DATA_ROOT": "/data/runs",
        "FORGEFLOW_APPROVALS_ROOT": "/data/approvals",
    }
    runs = tmp_path / "runs"
    runs.mkdir(mode=0o755)
    # Only the test inputs, published ports and resource names change. Keep the
    # actual Compose filesystem restrictions and runtime volume destinations.
    service["volumes"] = [
        f"{runs}:/data/runs:ro" if value == "../runs:/data/runs:ro" else value
        for value in service["volumes"]
    ]
    for key, volume in config["volumes"].items():
        volume["name"] = f"{project}-{key}"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    env = dict(os.environ)
    if context := os.environ.get("FORGEFLOW_TEST_DOCKER_CONTEXT"):
        env["DOCKER_CONTEXT"] = context
    command = ["docker-compose"] if shutil.which("docker-compose") else ["docker", "compose"]
    command += ["-p", project, "-f", str(compose_file)]

    def compose(*args, check=True):
        return subprocess.run(command + list(args), env=env, capture_output=True,
                              text=True, check=check, timeout=90)

    try:
        compose("up", "-d", "--no-build")
        container = compose("ps", "-q", "api").stdout.strip()
        assert container
        _assert_started(container, read_only=True)
        assert list(runs.iterdir()) == []
    finally:
        compose("down", "--volumes", "--remove-orphans", check=False)
