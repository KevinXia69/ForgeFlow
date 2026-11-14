"""One-time, loopback-only setup capability; never log requests or form values."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import (
    ConfigError,
    atomic_write,
    config_path,
    configuration_lock,
    load_config,
    prepare_model,
    previous_connection,
    save_configuration,
)
from .providers import CHAT_KEY_ENV, PROVIDER_PRESETS

PUBLIC_ORIGIN = "http://127.0.0.1:17801"
MAX_BODY = 8192
TOKEN_LIFETIME = 30 * 60
TEST_ERRORS = {
    "model_runner_unauthorized": "The provider rejected authentication. Check the key and model access.",
    "model_runner_rate_limited": "The provider rate limit or quota was reached.",
    "model_runner_unavailable": "The selected model endpoint is unavailable.",
    "model_runner_error": "The provider rejected this model request.",
    "model_runner_invalid_response": "The endpoint returned an unsupported response format.",
    "model_timeout": "The connection test timed out.",
    "model_tools_unsupported": "This model did not complete the required tool call.",
    "model_tool_replay_failed": "This model did not accept the tool result round-trip.",
}
PAGE = r'''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Set up ForgeFlow locally</title><style>
*{box-sizing:border-box}body{font:16px/1.55 system-ui,sans-serif;background:#f5f7fa;color:#15243b;margin:0;padding:32px 18px}main{max-width:720px;margin:auto;background:white;border:1px solid #d6deea;border-radius:16px;padding:28px}h1{font-size:28px;line-height:1.2}label{display:block;margin-top:18px;font-weight:600}input,select,button{font:inherit;width:100%;padding:10px;border:1px solid #8292aa;border-radius:7px;margin-top:5px}input[type=checkbox]{width:auto;margin-right:8px}button{background:#183f7a;color:#fff;cursor:pointer;margin-top:20px}button:disabled{opacity:.6;cursor:not-allowed}.hint{color:#45566f;font-size:14px;margin:5px 0}#result,#test-result{white-space:pre-wrap;overflow-wrap:anywhere}code{overflow-wrap:anywhere}.error{color:#a51d26}a{color:#174a90}fieldset{border:0;padding:0}small{font-size:14px}h2{font-size:20px;margin-top:30px}
</style><main><h1>Set up your local ForgeFlow</h1>
<p>运行在你的电脑上 / Runs on this computer. Prompts and documents may be sent to your selected providers; their usage charges apply.</p>
<form id="form"><fieldset id="fields" disabled><h2>Chat and investigation / 聊天与后台调查</h2>
<label for="profile">Provider preset / 供应商</label><select id="profile" name="profile"></select>
<label for="protocol">API protocol / 接口协议</label><select id="protocol"></select>
<label for="base_url">API base URL / 接口地址</label><input id="base_url" type="url" maxlength="2048" spellcheck="false" autocomplete="off" required>
<label><input id="custom_endpoint" type="checkbox">I intentionally use a custom endpoint / 我确认使用自定义接口地址</label>
<p id="destination" class="hint"></p>
<p class="hint">HTTPS is required except for local endpoints. In Docker, use host.docker.internal to reach a service on your computer. Google native uses the installed catalog adapter; it is not converted to an OpenAI protocol.</p>
<label for="model">Model ID / 模型</label><input id="model" maxlength="160" spellcheck="false" autocomplete="off" required>
<label><input id="image_input" type="checkbox">This exact model supports image input / 此型号支持图片输入</label><p class="hint">Check your provider's model capabilities. Unknown models default to text only. Reasoning uses the highest supported configured level; unsupported features are not invented.</p>
<label for="model_api_key">Chat API key</label><input id="model_api_key" name="model_api_key" type="password" autocomplete="off" maxlength="512">
<p id="local-key-note" class="hint" hidden>Standard local Ollama needs no API key. Its compatibility adapter uses the public placeholder “ollama”.</p>
<button id="test" type="button">Test connection and tools / 测试连接与工具调用</button><p class="hint">Testing sends a small real model request and may incur token charges. Saving requires a successful test of these exact settings and key.</p><p id="test-result" role="status" aria-live="polite"></p>
<h2>Web search and knowledge / 联网搜索与知识库</h2>
<div id="search-key-group" hidden><label for="deepseek_search_api_key">DeepSeek API key · web search / 联网搜索</label><input id="deepseek_search_api_key" name="deepseek_search_api_key" type="password" autocomplete="off" maxlength="512" disabled><p class="hint">The existing web search always calls DeepSeek with your own key, independently of the chat provider. Each search is a separate paid model call.</p></div>
<p id="shared-search-note" class="hint">Official DeepSeek chat and web search reuse the chat key. Each search is a separate paid model call.</p>
<label for="dashscope_api_key">DashScope API key · document parsing / 文档解析</label><input id="dashscope_api_key" name="dashscope_api_key" type="password" autocomplete="off" maxlength="512">
<label for="siliconflow_api_key">SiliconFlow API key · embeddings + reranking / 语义检索</label><input id="siliconflow_api_key" name="siliconflow_api_key" type="password" autocomplete="off" maxlength="512"><p class="hint">The current knowledge index uses Qwen3 Embedding 8B (4096 dimensions) and Qwen3 Reranker 8B. Chat-provider changes do not change this index or its dedicated credentials.</p>
<label for="display_name">Local operator name / 本地操作员名称</label><input id="display_name" name="display_name" value="Local operator" maxlength="100" autocomplete="off">
<p class="hint">Keys are stored in the private Docker configuration volume with restricted permissions, without at-rest encryption. They are not stored in browser storage or application logs. Keep backups private.</p><p id="update" class="hint" hidden>Leave an existing key blank to keep it. A different provider or server requires its own key. Your identity, data and internal credentials are preserved.</p>
<button id="save" type="submit" disabled>Save and start / 保存并启动</button></fieldset></form>
<p id="result" role="status" aria-live="polite">Authorizing this setup window…</p><div id="done" hidden><p>Save your private invite code, then use it to sign in or register in ForgeFlow. / 请保存邀请码，用它在本地 ForgeFlow 登录或注册。</p><code id="invite"></code><p>The launcher is starting services. This can take several minutes.</p><a href="http://127.0.0.1:17800/app/" target="_blank" rel="noreferrer">Open ForgeFlow / 打开 ForgeFlow</a></div></main>
<script>
'use strict';
const token=location.hash.slice(1);history.replaceState(null,'',location.pathname);
const $=selector=>document.querySelector(selector),fields=$('#fields'),result=$('#result');
let presets={},configured=false,hasSavedSearchKey=false,receipt='';
async function call(path,body){const response=await fetch(path,{method:body?'POST':'GET',cache:'no-store',credentials:'omit',headers:{'X-ForgeFlow-Setup':token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});const data=await response.json();if(!response.ok)throw Error(data.error||'setup_failed');return data;}
function invalidate(){receipt='';$('#save').disabled=true;$('#test-result').textContent='';}
function connection(){return {preset:$('#profile').value,api:$('#protocol').value,base_url:$('#base_url').value,model:$('#model').value,custom_endpoint:$('#custom_endpoint').checked,image_input:$('#image_input').checked};}
function updateSearch(){const preset=presets[$('#profile').value],official=$('#profile').value==='deepseek_flash'&&!$('#custom_endpoint').checked&&$('#base_url').value.replace(/\/+$/,'')===(preset.endpoints[$('#protocol').value]||'').replace(/\/+$/,'');$('#search-key-group').hidden=official;$('#shared-search-note').hidden=!official;$('#deepseek_search_api_key').disabled=official;$('#deepseek_search_api_key').required=!official&&!hasSavedSearchKey;const local=$('#profile').value==='ollama';$('#local-key-note').hidden=!local;$('#model_api_key').required=!configured&&!local;try{$('#destination').textContent='The chat key will be sent to / 聊天凭据将发送至: '+new URL($('#base_url').value).origin;}catch{$('#destination').textContent='Enter an API base URL.';}}
function setPreset(id,saved){const preset=presets[id];$('#model_api_key').value='';$('#protocol').replaceChildren(...Object.keys(preset.endpoints).map(api=>{const option=document.createElement('option');option.value=api;option.textContent=api;return option;}));$('#protocol').value=saved?.api||preset.api;$('#base_url').value=saved?.base_url??preset.endpoints[$('#protocol').value];$('#model').value=saved?.model??preset.model;$('#custom_endpoint').checked=!!saved?.custom_endpoint;$('#image_input').checked=(saved?.input||preset.input).includes('image');invalidate();updateSearch();}
$('#profile').addEventListener('change',()=>setPreset($('#profile').value));
$('#protocol').addEventListener('change',()=>{$('#model_api_key').value='';$('#base_url').value=presets[$('#profile').value].endpoints[$('#protocol').value];$('#custom_endpoint').checked=false;invalidate();updateSearch();});
for(const selector of ['#base_url','#model','#model_api_key','#custom_endpoint','#image_input'])$(selector).addEventListener('input',()=>{if(selector==='#base_url')$('#model_api_key').value='';if((selector==='#model'&&$('#model').value!==presets[$('#profile').value].model)||selector==='#base_url'||selector==='#custom_endpoint')$('#image_input').checked=false;invalidate();updateSearch();});
$('#test').addEventListener('click',async()=>{invalidate();fields.disabled=true;$('#test-result').textContent='Testing the selected model and tools…';try{const data=await call('/test',{model_connection:connection(),model_api_key:$('#model_api_key').value});if(!data.ok)throw Error(data.detail||data.error||'connection_test_failed');receipt=data.test_receipt;$('#test-result').textContent='Connection and tools passed / 连接与工具调用通过';}catch(error){$('#test-result').textContent='Test failed: '+error.message;}finally{fields.disabled=false;$('#save').disabled=!receipt;updateSearch();}});
call('/state').then(data=>{presets=data.presets;configured=data.configured;hasSavedSearchKey=!!data.has_deepseek_search_key;$('#profile').replaceChildren(...Object.entries(presets).map(([id,preset])=>{const option=document.createElement('option');option.value=id;option.textContent=preset.label;return option;}));$('#profile').value=data.model_connection?.preset||'deepseek_flash';setPreset($('#profile').value,data.model_connection);if(configured){$('#display_name').value=data.display_name;$('#update').hidden=false;}else{$('#dashscope_api_key').required=true;$('#siliconflow_api_key').required=true;}fields.disabled=false;result.textContent='';updateSearch();}).catch(()=>{result.className='error';result.textContent='This setup link is missing, expired, or already used. Run the launcher again. / 配置链接已失效，请重新运行启动器。';});
$('#form').addEventListener('submit',async event=>{event.preventDefault();if(!receipt)return;const payload=Object.fromEntries(new FormData(event.target));delete payload.profile;payload.model_connection=connection();payload.test_receipt=receipt;fields.disabled=true;result.className='';result.textContent='Saving private configuration…';try{const data=await call('/configure',payload);for(const input of document.querySelectorAll('input[type=password]'))input.value='';$('#invite').textContent=data.invite_code;$('#done').hidden=false;$('#form').hidden=true;result.textContent='Saved. This one-time setup link is now closed. / 已保存，一次性配置链接已关闭。';}catch(error){fields.disabled=false;result.className='error';result.textContent='Could not save: '+error.message;}});
</script></html>'''
# A static hash policy permits only this bundled script; the page has no network assets.
_SCRIPT = PAGE.split('<script>')[1].split('</script>')[0]
SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()


class SetupState:
    def __init__(self, path: Path | None = None, *, mounts: dict[str, Path] | None = None,
                 connection_tester=None):
        self.path = config_path(path)
        self.mounts = mounts
        self.token_path = self.path.parent / ".setup-capability.json"
        self.mutex = threading.Lock()
        self.connection_tester = connection_tester
        self.receipt_key = secrets.token_bytes(32)
        self.test_receipts: dict[str, tuple[str, float]] = {}

    def issue(self) -> str:
        with self.mutex, configuration_lock(self.path):
            record = {"token": secrets.token_urlsafe(32), "expires": time.time() + TOKEN_LIFETIME,
                      "used": False}
            atomic_write(self.token_path, json.dumps(record).encode())
            return record["token"]

    def record(self) -> dict:
        try:
            if self.token_path.is_symlink():
                return {}
            return json.loads(self.token_path.read_bytes())
        except (OSError, ValueError):
            return {}

    def authorized(self, token: str) -> bool:
        record = self.record()
        return (isinstance(token, str) and bool(token) and not record.get("used", True)
                and record.get("expires", 0) > time.time()
                and hmac.compare_digest(record.get("token", ""), token))

    def url(self) -> str:
        record = self.record()
        token = record.get("token", "")
        if not self.authorized(token):
            raise ConfigError("setup_link_expired")
        return PUBLIC_ORIGIN + "/#" + token

    def _draft(self, payload: object) -> tuple[dict, str]:
        previous = load_config(self.path) if self.path.exists() else None
        return prepare_model(payload, previous)

    def _fingerprint(self, token: str, connection: dict, key: str) -> str:
        encoded = json.dumps([token, connection, key], sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self.receipt_key, encoded, hashlib.sha256).hexdigest()

    def test(self, token: str, payload: object) -> dict:
        if not self.authorized(token):
            raise ConfigError("setup_authorization_required")
        connection, key = self._draft(payload)
        tester = self.connection_tester
        if tester is None:
            try:
                from .connection_test import test_connection
                tester = test_connection
            except ImportError:
                raise ConfigError("connection_test_unavailable") from None
        try:
            result = asyncio.run(tester(connection, key))
        except Exception:
            raise ConfigError("connection_test_failed") from None
        if not isinstance(result, dict):
            raise ConfigError("connection_test_failed")
        if result.get("ok") is not True:
            code = result.get("error")
            if code in TEST_ERRORS:
                return {"ok": False, "error": code, "detail": TEST_ERRORS[code]}
            raise ConfigError("connection_test_failed")
        with self.mutex:
            if not self.authorized(token):
                raise ConfigError("setup_authorization_required")
            receipt = secrets.token_urlsafe(24)
            now = time.time()
            self.test_receipts = {name: record for name, record in self.test_receipts.items()
                                  if record[1] > now}
            if len(self.test_receipts) >= 16:
                self.test_receipts.clear()
            self.test_receipts[receipt] = (self._fingerprint(token, connection, key), now + 900)
        return {"ok": True, "test_receipt": receipt, "model": connection["model"], "protocol": connection["api"]}

    def save(self, token: str, payload: object) -> dict:
        with self.mutex:
            if not self.authorized(token):
                raise ConfigError("setup_authorization_required")
            if not isinstance(payload, dict):
                raise ConfigError("invalid_configuration_fields")
            draft = dict(payload)
            receipt = draft.pop("test_receipt", None)
            record = self.test_receipts.get(receipt) if isinstance(receipt, str) else None
            connection, key = self._draft(draft)
            if (record is None or record[1] <= time.time()
                    or not hmac.compare_digest(record[0], self._fingerprint(token, connection, key))):
                raise ConfigError("connection_test_required")
            value = save_configuration(draft, self.path, mounts=self.mounts,
                                       expected_connection=connection, expected_key=key)
            self.test_receipts.clear()
            atomic_write(self.token_path, json.dumps({"used": True}).encode())
            return value


def make_handler(state: SetupState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ForgeFlowSetup"
        sys_version = ""

        def log_message(self, _format, *args):
            pass  # In particular, never log URLs, capabilities, keys or bodies.

        def _reply(self, status: int, value: object, *, html: bool = False):
            body = value.encode() if html else json.dumps(value).encode()
            self.send_response(status)
            for name, val in {
                "Content-Type": "text/html; charset=utf-8" if html else "application/json",
                "Content-Length": str(len(body)), "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY", "Connection": "close",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                    f"script-src 'sha256-{SCRIPT_HASH}'; connect-src 'self'; "
                    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            }.items():
                self.send_header(name, val)
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _fence(self, *, mutation=False) -> bool:
            if self.headers.get_all("Host") != ["127.0.0.1:17801"]:
                self._reply(403, {"error": "setup_origin_denied"})
                return False
            origin = self.headers.get("Origin")
            if origin not in (None, PUBLIC_ORIGIN) or (mutation and origin != PUBLIC_ORIGIN):
                self._reply(403, {"error": "setup_origin_denied"})
                return False
            if self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
                self._reply(403, {"error": "setup_origin_denied"})
                return False
            return True

        def _authorized(self) -> bool:
            tokens = self.headers.get_all("X-ForgeFlow-Setup") or []
            if len(tokens) != 1 or not state.authorized(tokens[0]):
                self._reply(403, {"error": "setup_authorization_required"})
                return False
            return True

        def do_GET(self):
            if not self._fence():
                return
            if self.path == "/":
                return self._reply(200, PAGE, html=True)
            if self.path != "/state":
                return self._reply(404, {"error": "not_found"})
            if not self._authorized():
                return
            try:
                value = load_config(state.path)
            except ConfigError as error:
                if str(error) == "configuration_missing":
                    return self._reply(200, {"configured": False, "presets": PROVIDER_PRESETS})
                return self._reply(409, {"error": str(error)})
            self._reply(200, {"configured": True,
                             "model_connection": previous_connection(value), "presets": PROVIDER_PRESETS,
                             "display_name": value["operator"]["display_name"],
                             "has_deepseek_search_key": bool(value["env"].get("DEEPSEEK_API_KEY"))})

        def do_POST(self):
            if not self._fence(mutation=True) or not self._authorized():
                return
            if self.path not in {"/configure", "/test"}:
                return self._reply(404, {"error": "not_found"})
            if self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding"):
                return self._reply(415, {"error": "json_required"})
            lengths = self.headers.get_all("Content-Length") or []
            try:
                length = int(lengths[0]) if len(lengths) == 1 else -1
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                return self._reply(413, {"error": "invalid_body_size"})
            self.connection.settimeout(10)
            try:
                payload = json.loads(self.rfile.read(length))
                if self.path == "/test":
                    return self._reply(200, state.test(self.headers["X-ForgeFlow-Setup"], payload))
                value = state.save(self.headers["X-ForgeFlow-Setup"], payload)
            except (ValueError, UnicodeError, OSError) as error:
                code = str(error) if isinstance(error, ConfigError) else "configuration_invalid"
                return self._reply(400, {"error": code})
            self._reply(200, {"ok": True, "invite_code": value["env"]["FORGEFLOW_INVITE_CODES"]})
    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private local ForgeFlow setup")
    parser.add_argument("command", choices=("serve", "status", "url"))
    parser.add_argument("--saved", action="store_true", help="Require this setup session to be saved")
    args = parser.parse_args(argv)
    state = SetupState()
    if args.command == "status":
        try:
            configured = load_config(state.path)
        except ConfigError as error:
            print(json.dumps({"configured": False, "error": str(error)}))
            return 1
        env = configured["env"]
        connection = previous_connection(configured)
        chat_key = env.get(CHAT_KEY_ENV) or env.get("NEBIUS_API_KEY" if connection["preset"] == "nemotron" else "DEEPSEEK_API_KEY")
        if not chat_key or any(not env.get(key) for key in (
                "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "SEMANTIC_API_KEY")):
            print('{"configured":false,"error":"provider_credentials_incomplete"}')
            return 1
        if args.saved and not state.record().get('used', False):
            print('{"configured":true,"saved":false}')
            return 1
        print('{"configured":true}')
        return 0
    if args.command == "url":
        try:
            print(state.url())
        except ConfigError as error:
            print(json.dumps({"error": str(error)}))
            return 1
        return 0
    state.issue()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), make_handler(state))
    print('{"setup":"ready","port":8080}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
