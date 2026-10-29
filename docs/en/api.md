# Product API

`forgeflow api` exposes recorded-run review and capability-gated product case preparation, candidate selection, approval, execution status and evidence. ERP writes belong to the separate controlled executor; an assistant response is never approval. Deployment-specific access is described in the [judge guide](judge-guide.md).

## Authentication

`POST /api/auth/session` accepts:

```json
{"invite_code": "example-invite-code"}
```

A successful request returns `201`, a body with `uid` and `expires_at`, and a
`ff_session` cookie. The cookie is HttpOnly, SameSite=Lax, and Secure when the
deployment enables it. Sessions are stateless HMAC cookies; they are not stored
in a database. Every other `/api/*` endpoint requires the cookie and returns 401
when it is missing or invalid.

Invite codes are compared by hash. An incorrect length or content does not reveal
configuration details.

## Runs

| Endpoint | Behavior |
| --- | --- |
| `GET /api/scenarios` | Returns the two supported scenarios, A and B; arbitrary task names are not accepted |
| `GET /api/runs` | Lists `run_id`, task, score, strict success, `evaluation_valid`, and security profile |
| `GET /api/runs/{run_id}` | Full RunView projection with consistency, attestation status, and evidence paths |
| `GET /api/runs/{run_id}/events` | SSE; sends `snapshot` on state changes and `end` at a terminal state or after 30 seconds |
| `POST /api/runs` | Creates a run, returns `202` with `{run_id, state, reused}` |

Create request:

```json
{"scenario": "A", "idempotency_key": "an-example-key-0001"}
```

`idempotency_key` is 8 to 128 characters. The same `(uid, idempotency_key)` always
returns the same `run_id`; it never starts a duplicate. Only one run executes at a
time; the rest are queued. A process restart marks a leftover `running` entry as
`interrupted`. Creating runs requires `FORGEFLOW_RUNNER_ENABLED=true` and a
server-side spawner; otherwise the endpoint returns `503 runner_disabled`.

API-created runs have an owner. A different reviewer receives `404` for that run,
so the API does not reveal that it exists. CLI-created recorded runs have no owner
and are visible to every signed-in reviewer; that is the demo semantic.

Lifecycle states are `queued -> running -> finished`. Once the run directory
exists, `GET /api/runs/{run_id}` returns the recorded result.

## Plans

| Endpoint | Behavior |
| --- | --- |
| `GET /api/runs/{run_id}/plans` | Lists exported plans for the run |
| `GET /api/runs/{run_id}/plans/{plan_id}` | Plan status, operation counts, steps, planned cost (legacy label: new spend), policy and snapshot digests, artifacts |

Current v9 cost interpretation: the unchanged `expected_new_spend` field, exposed
as `plan.new_spend` in a product case, records the proposal's planned purchasing
and manufacturing cost, including retained existing documents. Existing stock is
treated as sunk cost. The first-version label “new spend” does not mean newly
created purchase value or an increase over the previous plan. Planned margin uses
purchase/manufacturing coverage revenue as its denominator, excluding finished stock, and is not measured accounting profit. `decision_metrics.schema_version=2` and `margin_basis=new_spend_coverage_v2` identify the corrected definition; version 1 remains historical and cannot establish current compliance. The full `sales_value` is retained for context, while `new_spend_revenue` is the margin denominator.
Customer sales-order budget caps are not a new-purchase authorization limit.
Without a measured baseline, `cost_delta` remains unknown. Historical numeric
records and the task's original financial constraints are unchanged.

Plan ids must be 64 hexadecimal characters. Sources are restricted to
`run_dir/plans/<id>` or an exported
`harbor/job/*/agent/recovery/<id>`. Symlinks, oversized files, and directory
traversal are rejected.

## Approvals

| Endpoint | Behavior |
| --- | --- |
| `POST /api/runs/{run_id}/plans/{plan_id}/approvals` | Creates an approval, `201`; body `{"ttl_seconds": 60-3600}` |
| `GET .../approvals` | Lists approvals with state `valid`, `expired`, `used`, or `revoked` |
| `POST .../approvals/{nonce}/revoke` | Revokes an approval and burns the one-time flag |

An approval binds:

- `run_id`
- `plan_id`
- `policy_sha256`
- `snapshot_sha256`
- `approver` (session uid)
- `issued_at_utc`
- `expires_at_utc`
- a random `nonce`

A plan must carry `policy_sha256` to be approvable. Without it, the endpoint
returns `409 plan_not_approvable`; this guarantees that the executor's policy
recheck can succeed. Only one unexpired, unused approval may exist for a plan;
a duplicate returns `409 approval_exists`. Revocation allows a new approval.

Approvals and revocations append to `<approvals_root>/audit.jsonl` under a file
lock. An approval is a product-mode input; autonomous benchmark runs use a
pre-authorized mode and are recorded separately.

## Knowledge Q&A

`POST /api/knowledge/answer` requires a session.

Request:

```json
{"question": "How does ForgeFlow separate raw score from evaluation_valid and environment_chain_ok?", "limit": 5}
```

Response (the `score` field is omitted from this example because it depends on
the indexed corpus):

```json
{
  "found": true,
  "passages": [
    {
      "path": "docs/en/evaluation.md",
      "start_line": 10,
      "end_line": 25,
      "text": "..."
    }
  ],
  "citations": ["docs/en/evaluation.md:10-25"]
}
```

The endpoint returns HTTP 200 even when nothing is found:

```json
{"found": false, "passages": [], "citations": []}
```

Retrieval is deterministic and offline. It returns verbatim passages, not a
generated answer. The `limit` is applied after the score floor. A passage has at
most 40 lines and 2000 characters.

The underlying helper accepts a `locale` keyword. `zh` is the default and keeps
the legacy result keys exactly: `found`, `passages`, `citations`. With
`locale="en"` it indexes only `docs/en/**/*.md` and includes `"locale": "en"` in
the result. An unsupported locale raises `ValueError`; the HTTP layer maps
`ValueError` or `OSError` to a 503 `knowledge_unavailable`.

The current HTTP route calls the default locale unless the API layer explicitly
passes one. The English corpus is available through the helper now; the route's
locale exposure is owned by the API integration layer.

## Assistant answer

`POST /api/assistant/answer` first retrieves evidence, then optionally calls an
OpenAI-compatible model. Model selection order:

1. A model passed in the request with a browser-owned key.
2. A locally saved workspace model configuration.
3. The platform environment configuration.

On success the result has `mode: model` with the generated answer plus the
original passages and citations. On failure it falls back to `mode: retrieval`
with the verbatim passages and a `warning`; the endpoint does not return 5xx just
because the model failed.

## Model settings

| Endpoint | Behavior |
| --- | --- |
| `GET /api/settings/model` | Masked description only; never returns a key |
| `PUT /api/settings/model` | Saves a local workspace configuration; review instance returns `403 settings_read_only` |
| `DELETE /api/settings/model` | Clears a local configuration; review instance returns 403 |
| `POST /api/settings/model/test` | Optional model override; returns `{ok, latency_ms}` or `{ok: false, error}` |

The masked view contains `configured`, `source` (`platform`, `workspace`, or
`none`), `profile`, `base_url`, `model`, `has_key`, `key_hidden`, `can_override`,
and `updated_at`. The review instance also marks the view read-only.

## Security properties

- The service does not need `NEBIUS_API_KEY` and does not read
  `/etc/odoo/api_key`.
- It has no code path that can start an ERP write.
- Run paths are built from `data_root` and validated; arbitrary disk paths are
  rejected.
- Responses set `X-Content-Type-Options: nosniff`, `Referrer-Policy:
  no-referrer`, and `Cache-Control: no-store`.
- Deployment defaults to a Secure cookie; local HTTP must explicitly set
  `FORGEFLOW_SECURE_COOKIE=false`.
- The review container runs as non-root with a read-only root filesystem, dropped
  capabilities, and no Docker socket.

## SSE event stream

`GET /api/runs/{run_id}/events` returns `text/event-stream`.

- The first connection immediately receives a `snapshot` with `run_id`, `state`,
  `score`, `evaluation_valid`, and `attestation`.
- New snapshots are sent when the state or score changes.
- A terminal state sends `end`.
- If no terminal state appears within 30 seconds, the server sends `end` and
  closes; the client should reconnect or fall back to polling.
- The stream never sends model tokens, reasoning text, or raw tool output.
  Reconnecting is idempotent and does not rerun the task.

## Not included

- Server-side event sequence numbers or replay after a disconnect. Clients
  reconnect with a fresh snapshot.
- A durable event history store.
- A separate "reject with reason" approval endpoint; revocation expresses that
  action today.
- Per-user quotas or rate limits for the platform model key.
- The real-Odoo D1 prepare, web approval, and apply product loop. The current
  web-created run is autonomous; see [limits.md](limits.md).
