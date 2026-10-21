# Model Settings

The default model is `nvidia/Nemotron-3.5-Super-VL`, served through Nebius Token
Factory at `https://api.tokenfactory.nebius.com/v1`. The product workbench offers
the platform default plus an optional browser-owned OpenAI-compatible key.

## Platform configuration

The platform configuration comes only from server-side environment variables:

| Variable | Purpose |
| --- | --- |
| `FORGEFLOW_MODEL_PROFILE` | Named profile, default `nemotron` |
| `FORGEFLOW_MODEL_BASE_URL` | OpenAI-compatible base URL |
| `FORGEFLOW_MODEL_NAME` | Model identifier, default `nvidia/Nemotron-3.5-Super-VL` |
| `FORGEFLOW_MODEL_API_KEY` | Platform key; never returned to a browser |

The review instance holds the key in platform configuration. A reviewer sees only
that a key exists and is hidden. `GET /api/settings/model` returns a masked view:

```json
{
  "configured": true,
  "source": "platform",
  "profile": "nemotron",
  "base_url": "https://api.tokenfactory.nebius.com/v1",
  "model": "nvidia/Nemotron-3.5-Super-VL",
  "has_key": true,
  "key_hidden": true,
  "can_override": true,
  "updated_at": "..."
}
```

The actual key, its prefix, and its length are not part of the response. The key is
not written to logs, error messages, or run manifests.

## Local workspace configuration

On a local workbench, `PUT /api/settings/model` can save a model configuration for
the server process. `DELETE` clears it. The review deployment rejects both with
`403 settings_read_only` so that a reviewer cannot change the shared instance.

`POST /api/settings/model/test` accepts an optional model override. Without an
override it uses the platform key on the review instance or the locally saved
configuration on a workstation. It returns `{ok, latency_ms}` on success or
`{ok: false, error}` on failure.

## Browser-owned key

A reviewer may switch to "use my own key". The browser then shows base URL,
model, and API key fields. The key is stored only in the browser's `localStorage`
under `forgeflow.model` and is sent temporarily with the request. The server does
not persist it.

The own-key path is OpenAI-compatible. A reviewer can use a different provider,
for example DeepSeek V4.1 Flash 1017, provided the base URL and model name match
that provider. The platform quota is not used for a browser-owned key, and the
key is never shared with other reviewers.

Turning the switch off clears the local configuration and returns to the platform
default. The UI does not echo the key back into the page after saving.

## Assistant answer selection

`POST /api/assistant/answer` first performs evidence retrieval. It then selects a
model in this order:

1. A `model` object sent in the request with a browser-owned key.
2. The locally saved workspace configuration.
3. The platform environment configuration.

The model receives the question and at most five retrieved passages, with each
passage capped at 1500 characters. The system prompt requires it to rely only on
those passages, cite `path:start-end`, and state when the evidence is
insufficient.

If the model call fails, the endpoint returns `mode: retrieval` with the original
passages and citations plus a `warning`. It does not replace the evidence with a
generated answer and does not return a 5xx solely because generation failed.

## Sampling and context

The recorded development configuration uses:

- Temperature 1.0.
- Top-p 0.95.
- Context budget 131,072 tokens.
- Output limit omitted by default; the provider decides, and the run records
  `all-requests-v2` as the policy version.
- Thinking mode `provider-default` by default. `--thinking-mode off` is an
  explicit experiment through `chat_template_kwargs.enable_thinking=false`; it is
  not the default.
- Explicit `--max-output-tokens` is a separate experimental factor recorded as
  `explicit`.

Historical 16k and 32k output-limit failures are retained. A response-length stop
is recorded as an observed stop reason, not silently treated as a hard model
limit. The project also records context-window and single-output limits
separately, because they fail in different ways.

## Model probe

`uv run forgeflow probe-model` performs a bounded interface check: at most two
completion requests and one no-side-effect test tool. It omits the output-limit
field by default; an explicit `--max-tokens` between 1 and 8192 uses that
experimental budget and is recorded as an explicit choice.

The probe proves that the endpoint is callable. It does not prove ERP task
success, tool reliability, or a stable model capability. The same applies to a
successful assistant answer: it is a product interaction, not a benchmark result.

## What is not protected

- There is no per-user quota or rate limit on the platform key. Anyone with an
  invite code can consume platform model capacity.
- The browser-owned key is protected by browser storage and transport, not by a
  server-side secret store. A compromised browser can read it.
- Model reasoning text, full system prompts, and raw tool responses are not shown
  in the UI or stored in public evidence.
- A successful model call is not a task success. The original verifier remains
  the only source of the business score.
