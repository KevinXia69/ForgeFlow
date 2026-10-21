# DSH Python SDK runtime

ForgeFlow prefers the official `deepseek-harness-sdk` when it is installed and
`FORGEFLOW_DSH_SDK=1`. The SDK owns one persistent JSON-RPC runtime, so a
conversation can keep an explicit `session-<hash>` id and survive an API process
restart. When the SDK/runtime is absent, the API falls back to the one-shot
headless CLI adapter.

## Install (local only)

```bash
uv pip install --python .venv/bin/python 'deepseek-harness-sdk==0.2.1rc2'
uv run python scripts/setup_dsh_harness.py
```

The setup script records `FORGEFLOW_DSH_SDK=1` only when the import is
available. It always writes the SDK session map below the ignored `.runtime/`
directory; the SDK runtime wheel is intentionally not part of the default
deployment dependency set.

## Behavior

- `FORGEFLOW_DSH_SDK=1`: SDK adapter (`src/forgeflow/api/dsh_sdk.py`)
- absent/false: legacy `DshHarness` CLI adapter
- session id: `session-<sha256(owner:conversation)[:32]>`
- first turn: `start_session(id)`; later process: `resume_session(id)`
- activity: SDK `assistant/message`, `tool/call`, `tool/result` events mapped to
  the same bounded ForgeFlow `DshReply` / SSE envelope
- timeout: SDK request timeout, then `dsh_timeout`
- workspace: `.runtime/assistant-workspace`, read-only DSH permission mode

The SDK launch uses a small wrapper script (`dsh-sdk-wrapper.sh`) because the
installed runtime is `node …/lib/bin.js`, not a bare `dsh` executable. The
wrapper is local runtime state and is never exported.
