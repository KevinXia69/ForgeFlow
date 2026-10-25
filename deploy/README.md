# ForgeFlow API deployment

Container deployment for the first-version ForgeFlow product service: a
read-only FastAPI app (`forgeflow.api.app:create_app`) over recorded runs, plus
a writable approval store. The container runs as an unprivileged user and holds
no model API key and no ERP credential.

## Prerequisites

- Docker Engine with the Compose plugin (`docker compose version`).
- A checkout of this repository. The stack bind-mounts the repository read-only
  and reads recorded runs from `runs/` when that directory exists.
- Port 8000 free on the host. The published port binds `127.0.0.1` only.
- The runs directory must be readable by uid 10001 (regular `0644` files and
  `0755` directories are fine; a `0700` private tree is not).

## Configure

Run the commands below from this `deploy/` directory:

```bash
cp .env.example .env
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

Put the generated value into `.env` as `FORGEFLOW_SESSION_SECRET`. It must be at
least 32 characters; the placeholder shipped in `.env.example` is deliberately
invalid so the service refuses to start until the secret is replaced.

Set `FORGEFLOW_INVITE_CODES` to one or more comma-separated invite codes, each
at least 16 characters. Keep `FORGEFLOW_SECURE_COOKIE=false` for local plain
HTTP and set it to `true` when the service is served behind TLS.

## Start

From this `deploy/` directory:

```bash
docker compose up --build
```

The service listens on <http://127.0.0.1:8000> and its unauthenticated health
endpoint is <http://127.0.0.1:8000/api/health>. Compose also uses that endpoint
as the container health check. Add `-d` to run detached.

## Data layout

| Host | Container | Access | Purpose |
| ---- | --------- | ------ | ------- |
| `../runs/` | `/data/runs` | read-only (`:ro`) | recorded runs |
| `forgeflow-approvals` volume | `/data/approvals` | read-write | approval records |

The runs mount is read-only: the service can list and read recorded runs but
cannot modify or delete them. Do not mount the runs directory writable; the
approval flow never needs to write there.

The approvals volume is the named volume `forgeflow-approvals` (created by
Compose and independent of the container lifecycle). Approval records and the
append-only `audit.jsonl` live there. The image initializes `/data/approvals`
for uid/gid 10001, and a fresh named volume inherits that ownership. The API
writes its run registry there during startup, even with the runner disabled.
Inspect the volume with:

```bash
docker volume inspect forgeflow-approvals
```

`docker compose down` keeps the volume and its approvals; only
`docker compose down -v` deletes it.

## Rotate invite codes and the session secret

Invite codes are configuration, not database rows. To rotate them:

1. Edit `FORGEFLOW_INVITE_CODES` in `deploy/.env`.
2. Recreate the service: `docker compose up -d --force-recreate api`.

Sessions issued with a removed code stay valid until their TTL expires (12
hours by default). For immediate global revocation, rotate the session secret
the same way: replace `FORGEFLOW_SESSION_SECRET` and recreate the service, which
invalidates every existing signed cookie. Secret rotation is the only way to
revoke a session before its TTL, so treat the secret as sensitive.

## Run without Docker

From the repository root, with host paths instead of the `/data` container
paths from `.env.example`:

```bash
uv sync
FORGEFLOW_DATA_ROOT=./runs \
FORGEFLOW_APPROVALS_ROOT=./approvals \
FORGEFLOW_SESSION_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
FORGEFLOW_INVITE_CODES=local-invite-code-0001 \
FORGEFLOW_SECURE_COOKIE=false \
uv run forgeflow api --host 127.0.0.1 --port 8000
```

The `api` subcommand is the same entry point the image uses. Host mode writes
approvals to `./approvals`; the directory is created during API startup.

## Security note

The service never holds the write key and cannot execute ERP writes. It only
reads recorded runs and stores approval records. An approval is a signed record
for the executor's product mode to consume; issuing or revoking one does not
contact the ERP. Model keys and ERP credentials stay with the experiment CLI and
executor and are never passed to this container. Do not add model or ERP
credentials to `deploy/.env`; nothing in this stack reads them.

The container adds defense in depth: it runs as non-root uid/gid 10001, uses a
read-only root filesystem with `/tmp` on tmpfs, drops all Linux capabilities,
sets `no-new-privileges`, and publishes port 8000 only on `127.0.0.1`. Terminate
TLS in front of the service and set `FORGEFLOW_SECURE_COOKIE=true` before
exposing it beyond the local host.


## Knowledge base

The image copies `README.md`, `docs/` and `contracts/` into `/app` and sets
`FORGEFLOW_KNOWLEDGE_ROOT=/app`, so the Q&A endpoint can cite the project's own
documents from inside the container.
