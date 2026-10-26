# Local product workbench

This guide starts a real, disposable ERP-Bench Odoo/PostgreSQL sandbox. The
business records are synthetic benchmark data; approved operations still write
to a real local ERP and are checked by its original verifier. Use the
[VPS deployment guide](../deploy/vps/README.md) for the public service.

## Host and installation

Use Ubuntu 24.04 with Python 3.12, uv, Node.js 24/npm and Docker. A Linux user
must be able to run `docker info` successfully. On Windows, run all commands
inside an Ubuntu WSL2 distribution with Docker integration and keep the checkout
on its Linux filesystem. Native PowerShell cannot run this Unix socket/file-lock
launcher. Allocate at least 4 CPUs, 8 GiB RAM and 20 GiB free disk. Builds require
network access to GitHub, the package registries, Docker Hub and PostgreSQL's
package repository. Apt packages within the pinned base image are not fully
snapshot-pinned, so builds are source-reproducible, not byte-identical.

From the repository root:

```bash
python3 scripts/fetch_upstream.py
uv sync --frozen --extra planning --extra dsh-sdk
uv run forgeflow doctor
cd web && npm ci && npm run typecheck && npm run build && cd ..
uv run python scripts/build_local_image.py --scenario A
```

The image builder verifies `vendor/erp-bench` against `upstream.lock.json`,
derives the declared `planning-metadata-v1` note projection, and leaves its
build context and hashes under `.runtime/local-image-builds/`. `--prepare-only`
stages and checks those inputs without invoking Docker. Upstream files and the
original verifier are preserved. The local image retains the exact upstream
entrypoint expected by `local_entrypoint.py`; the launcher applies its preserving
startup transformation before first boot. Do not substitute the VPS image:
its entrypoint and persistent-volume layout are different.

Initial module installation uses a 900-second setup-only RPC timeout and longer
Odoo request limits, with a 30-minute first-start readiness budget. The original
setup program is retained byte-for-byte behind a small bootstrap wrapper; this
does not change the original verifier or business inputs. A stopped container
fails readiness promptly. Normal resume has a three-minute readiness budget.

## Optional AI assistant

```bash
cp .env.example .env
# Edit .env locally and fill NEBIUS_API_KEY; do not commit it.
uv run python scripts/setup_dsh_harness.py
```

The setup installs the locked npm package `@deepseek-ai/dsh@0.2.1-rc.2`
under `.runtime/dsh/`, matching the Python `dsh-sdk` extra. It writes a public
composition and `.runtime/dsh.env`, with no API key. The launcher reads the
key from the process environment or `.env` when starting the API. It does not
silently install dependencies during startup. Set `NEBIUS_MODEL` and
`NEBIUS_BASE_URL` in the shell before setup to change the assistant endpoint;
then rerun setup with `--configure-only` to reuse the installed package.

The default is Nebius Token Factory with `nvidia/Nemotron-3.5-Super-VL`.
DSH receives scoped evidence tools and cannot approve or execute ERP operations.
Local processes run under your account; this is a development boundary, not
the separate Unix-user isolation used by the VPS. `FORGEFLOW_DSH_ENABLED=0`
disables the sidecar. Planning, approval and execution work without it; the
UI may provide deterministic/retrieval responses and must not claim these
are a successful model turn. The settings dialog's workspace/temporary model
configuration is separate from the DSH process configuration; see
[model settings](en/model-settings.md).

## Start, preserve and inspect

```bash
uv run python scripts/local_workbench.py init
uv run python scripts/local_workbench.py start
curl -f http://127.0.0.1:8000/api/health
```

Open `http://127.0.0.1:8000/app/` and use the development-only invite
`demo-invite-code-0001`. Create a case, inspect a feasible plan, then approve
that plan to execute it. Check the resulting readback and original verifier
receipt; a successful HTTP health check alone proves neither.

```bash
uv run python scripts/local_workbench.py doctor
uv run python scripts/local_workbench.py stop
uv run python scripts/local_workbench.py resume
```

Stop/resume preserves the container, database, approvals and generated session
identity. Do not delete the sandbox directory or change a bound planner bundle
to recover a failure. Use `doctor` and preserve evidence. The explicit `delete`
command requires the full owned container ID and does not erase retained case
evidence; a new experiment needs new sandbox and case roots.

For Scenario B, build its image and set the scenario for every launcher command:

```bash
uv run python scripts/build_local_image.py --scenario B
export FORGEFLOW_LOCAL_SCENARIO=B
uv run python scripts/local_workbench.py init
uv run python scripts/local_workbench.py start
# http://127.0.0.1:8010/app/
```

Scenario A uses API/Odoo ports 8000/18069; B uses 8010/18070. For custom image
tags, pass `--tag` to the builder and the same `FORGEFLOW_LOCAL_IMAGE` to all
launcher invocations. Docker must use the same context for build and launch;
when using the optional Colima `forgeflow` profile, export its `DOCKER_HOST`
before both commands.

## Verification scope

Run `uv run pytest -q` for the offline suite and the frontend's `npm run
typecheck`/`npm run build` for compilation. A full local acceptance additionally
requires image build, fresh initialization, an authenticated case, pre-approval
no-write evidence, approval, readback/original verification, and stop/resume.

The October 25 Windows repair host had no WSL installation or reachable Docker
engine, so verification used an isolated Ubuntu VPS directory with a new virtual
environment, new local image and fresh database. Scenario A initialized, served
the UI, created a real plan, and preserved that case/session across stop/resume.
Both frontend clean builds and 136 local lifecycle tests passed. The assistant
installation was checked without a model call; this bootstrap test did not
approve an ERP write or rerun Scenario B. See the
[bootstrap record](evidence/2026-10-25-local-bootstrap/README.md), including the
initial timeout failure and repair. Hosted acceptance remains separately scoped.
