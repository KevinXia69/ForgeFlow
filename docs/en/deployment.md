# Deployment

ForgeFlow separates autonomous research runs, a local product workbench with an
injected root executor, and a read-only review API. Research runs do not acquire
product approvals. The local product flow requires an eligible plan and a bound,
single-use approval; the public review deployment cannot execute that flow.

## Local research environment

Prerequisites:

- Python 3.12 or newer.
- `uv`.
- Docker and Docker Compose able to run Linux ARM64 or AMD64 containers.
- At least 4 CPUs and 8 GiB of memory for the recommended Colima profile.

Bootstrap:

```bash
python3 scripts/fetch_upstream.py
uv sync --frozen
cp .env.example .env
```

Fill `NEBIUS_API_KEY` locally. The file is git-ignored. Do not put keys in source,
logs, or committed configuration. The default model configuration is:

```dotenv
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1
NEBIUS_MODEL=nvidia/Nemotron-3.5-Super-VL
```

Run a task with the frozen recovery configuration:

```bash
uv run forgeflow run A --agent pi \
  --security-profile permission-split-v1 \
  --planner-profile cp-sat-recovery-v1 \
  --data-profile planning-metadata-v1
```

`A` is `2280_medium_repair_plan_medium`; `B` is
`2299_hard_repair_plan_hard`. A run creates a fresh environment, keeps logs, and
deletes its container after the trial. The original verifier decides the score.
The local runner needs Docker and a model key; the product review service needs
neither.

## Local read-only API without Docker

This starts a review surface for authorized evidence, not a live product executor.
Keep the same session secret across restarts when preserving authenticated owner
identities. The generated secret below is only for a disposable local review.

```bash
uv sync
FORGEFLOW_DATA_ROOT=./runs \
FORGEFLOW_DEPLOYMENT=review \
FORGEFLOW_APPROVALS_ROOT=./approvals \
FORGEFLOW_SESSION_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
FORGEFLOW_INVITE_CODES=local-invite-code-0001 \
FORGEFLOW_SECURE_COOKIE=false \
uv run forgeflow api --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` and sign in with the configured invite code. `api` is
the same entry point the container uses. Historical approval records are readable
evidence; the API cannot create or revoke grants for recorded runs.

Ownerless CLI runs require explicit `FORGEFLOW_PUBLIC_RUNS` configuration, which
defaults to an empty list. Use comma-separated reviewed evidence IDs; a CLI
origin alone does not grant visibility. Owned runs remain visible only to their
authenticated owner.

`FORGEFLOW_SESSION_SECRET` must be at least 32 characters. The shipped placeholder
is deliberately invalid so the service refuses to start until it is replaced.
Invite codes must be at least 16 characters. The secure-cookie flag must be false
only for local plain HTTP.

## Docker Compose

The `deploy/` directory contains a read-only product API stack:

```bash
cd deploy
cp .env.example .env
# set FORGEFLOW_SESSION_SECRET and FORGEFLOW_INVITE_CODES in .env
docker compose up --build
```

The service listens on `127.0.0.1:8000`. The health endpoint is
`/api/health`. Compose also uses that endpoint as the container health check.

Container properties:

- Runtime user uid/gid 10001, non-root.
- Read-only root filesystem, with `/tmp` on tmpfs.
- All Linux capabilities dropped and `no-new-privileges` set.
- `runs/` bind-mounted read-only at `/data/runs`.
- A separate named volume `forgeflow-approvals` mounted writable at
  `/data/approvals`; mode 0700.
- The published port binds `127.0.0.1` only; TLS belongs in front of the service.
- No `/etc/odoo` mount, no Docker socket, no model key, no ERP credential.

The runs mount is intentionally read-only. The service lists and reads recorded
runs but cannot modify or delete them. `docker compose down` keeps the approvals
volume; `docker compose down -v` deletes it.

## Review image

`deploy/Dockerfile.api` is a multi-stage build:

1. Node 22 builds the React workbench from `web/`.
2. The Vite output is copied into the API static directory.
3. Python 3.12 installs the package and the pinned FastAPI, Uvicorn, and
   cryptography versions.
4. The image copies `README.md`, `docs/`, `contracts/`, and `demo-runs/` into
   `/app`, then runs as uid/gid 10001.

The exported English review image contains its English corpus at `/app/docs/en/`.
`FORGEFLOW_KNOWLEDGE_ROOT=/app` makes the packaged documents available to knowledge
Q&A; only documents present in the build context are copied. Run review and
historical approval reads do not grant any ERP write capability.

The image has no model key and no ERP credential. It sets:

```text
FORGEFLOW_DEPLOYMENT=review
FORGEFLOW_DATA_ROOT=/app/demo-runs
FORGEFLOW_APPROVALS_ROOT=/tmp/approvals
FORGEFLOW_KNOWLEDGE_ROOT=/app
FORGEFLOW_RUNNER_ENABLED=false
```

The review API does not issue or revoke approval grants. Its configured temporary
approval path is a storage setting, not an enabled workflow. A rebuild discards
any temporary records; local live product approvals belong to the separately
configured executor and case store.

## Render review instance

The public review instance is declared in `render.yaml`:

- Service name `forgeflow`, Docker runtime, branch `main`.
- Dockerfile `deploy/Dockerfile.api`; build context is the repository root.
- Region Singapore, plan starter, automatic deploy trigger off.
- Health check path `/api/health`.
- `FORGEFLOW_DEPLOYMENT=review`.
- `FORGEFLOW_RUNNER_ENABLED=false`.
- `FORGEFLOW_SECURE_COOKIE=true`.
- `FORGEFLOW_KNOWLEDGE_ROOT=/app`.
- Invite code supplied through the platform environment, not the repository.
- Session secret generated by the platform.

Recorded URL: `https://forgeflow-yzo9.onrender.com`. Invite codes are distributed
separately. The instance was verified previously for health, the React home page,
review configuration, invite-code sign-in, four demo runs, and knowledge answers
with citations. It does not expose a Docker socket or an ERP write path.
That historical deployment check is not proof that the current v9 worktree has
been published. No new deployment is claimed here.

The first deployment attempt failed with exit code 127 because Render parsed a
`dockerCommand` incorrectly. The fix was to let the image use its own `CMD` and
have the CLI read the platform `PORT`.

## Operations

Health and security headers:

```bash
curl -fsS https://<host>/api/health
curl -sI https://<host>/ | grep -i "x-content-type-options\|referrer-policy\|cache-control"
```

Invite-code rotation:

1. Change `FORGEFLOW_INVITE_CODES`.
2. Recreate the service (`docker compose up -d --force-recreate api` on Compose,
   or redeploy on Render).

Existing sessions remain valid until their TTL (12 hours by default) unless the
session secret is also rotated. Rotating `FORGEFLOW_SESSION_SECRET` invalidates
all signed cookies immediately; it is the only way to revoke a session before its
TTL. Treat the secret as sensitive.

Approval boundaries:

Historical run approval records can be inspected, but their create and revoke
endpoints return `409 historical_evidence_read_only`. A configured approval
directory or Compose volume does not enable those mutations.

Real product approval is a separate local Cases flow. The authenticated owner,
current case, plan eligibility and executor connection are checked before a
single-use grant can authorize the bound plan. Case audit and execution/readback
receipts record that flow; an approval alone is not proof of an ERP write or a
strict verifier pass. See [limits.md](limits.md).

## Verification checklist

- Unauthenticated `/api/runs` returns 401.
- Two invite-code sessions cannot see each other's owned runs. Ownerless CLI runs
  are visible only through an explicit `FORGEFLOW_PUBLIC_RUNS` allowlist; there
  is no default sharing of historical runs.
- For an authorized existing historical plan, approval create and revoke return
  `409 historical_evidence_read_only`; no new grant or revoke record is written.
- Review capabilities disable product creation and approval. Recorded approval
  reads remain available only within the caller's evidence scope.
- After a restart, `/api/runs` still reads recorded runs, and leftover `running`
  entries become `interrupted`.
- `/api/health` returns `{"ok": true}`.
- Security headers are present: `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.

## What is not deployed

- The public instance does not run tasks, start containers, or write to Odoo.
- Local product acceptance has passed separately: recovered A r2 reached 51/51
  original rules without another ERP write, and B completed browser approval,
  execution and readback with 95/95 original rules. These dedicated ERP-Bench
  sandboxes are not a public live service or a customer deployment. See
  [limits.md](limits.md).
- The public instance does not have a custom domain or a separately managed TLS
  certificate; the platform terminates TLS.
- The review image does not host a durable, writable approval workflow.
- Per-user model-key quotas and rate limits are not implemented.
