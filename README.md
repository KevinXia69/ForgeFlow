# ForgeFlow

**Manufacturing recovery, from disruption to verified ERP changes.**

ForgeFlow is an AI-agent workbench for recovering purchase and production plans
after supplier cancellations and manufacturing disruptions. It connects business
investigation, constraint-based planning, human approval, and ERP execution in one
traceable workflow.

[Open the hosted workbench](https://forgeflowagent.com/app/) ·
[Install locally](#install-locally) ·
[Benchmark results](#benchmark-results) ·
[One recovery case](#one-recovery-case) ·
[Architecture](#architecture) ·
[Source setup](#source-setup)

![The ForgeFlow workbench home screen](docs/images/local-workbench-home.png)

*The same workbench runs on the hosted review environment and in a local installation.*

## Hosted or local

ForgeFlow runs in two forms with the same core product loop:

| Mode | Best for | Data and model |
| --- | --- | --- |
| **Hosted workbench** | Reviewing the product without installing Docker | Project-provided access credentials and a preconfigured model |
| **Local installation** | Running the workbench on your own computer | Your own Odoo 20, PostgreSQL and Qdrant data; your own provider keys |

The hosted and local instances do not share data. Both use the same case,
investigation, planning, approval, execution and independent-readback workflow.

```text
Investigate → Prepare a plan → Review and approve → Execute → Read back and verify
```

## Install locally

### One-command installation

On macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/KevinXia69/ForgeFlow/main/install.sh | bash
```

On Windows PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/KevinXia69/ForgeFlow/main/install.ps1 | iex"
```

The command downloads the release, verifies its SHA-256 checksum, installs under
`~/ForgeFlow` (or `%USERPROFILE%\ForgeFlow`) and starts the one-time setup page.
Set `FORGEFLOW_HOME` to choose another directory.

**Docker is required** and must be running in Linux-container mode. Use Docker
Desktop on Windows or macOS, or Docker Engine with Compose v2 on Linux. No host
Python or Node.js installation is required for the release package.

### Release packages

| Computer | Release package | Start after extracting the complete package |
| --- | --- | --- |
| Windows | [ForgeFlow-v3.11.0-Windows.zip](https://github.com/KevinXia69/ForgeFlow/releases/download/v3.11.0/ForgeFlow-v3.11.0-Windows.zip) | Double-click `Start-ForgeFlow.cmd` |
| macOS | [ForgeFlow-v3.11.0-macOS.zip](https://github.com/KevinXia69/ForgeFlow/releases/download/v3.11.0/ForgeFlow-v3.11.0-macOS.zip) | Open `Start-ForgeFlow.command` |
| Linux | [ForgeFlow-v3.11.0-Linux.zip](https://github.com/KevinXia69/ForgeFlow/releases/download/v3.11.0/ForgeFlow-v3.11.0-Linux.zip) | Run `bash start.sh` |

The launcher opens a private one-time setup page. Choose a chat provider, enter
your own service keys, save the generated invitation code, and wait for the local
services to become ready. Then sign in at
**[127.0.0.1:17800/app/](http://127.0.0.1:17800/app/)**.

First startup pulls the published images or, if unavailable, builds the bundled
source fallback. It needs internet access and may take several minutes. The
installer includes source and does not require a private registry account.

See the [installation guide](docs/LOCAL_INSTALL.md) for requirements, provider
configuration, data retention and troubleshooting.

### Bring your own service keys

The hosted workbench provides its configured default model for reviewers. A local
installation uses the user's accounts:

| Service | Choose or provide | Used for |
| --- | --- | --- |
| Chat and investigation | DeepSeek, Nebius, OpenAI, Anthropic, Google/Gemini, OpenRouter, DashScope/Qwen, SiliconFlow, Ollama or a custom endpoint | Assistant reasoning, tool calls and declared image capabilities |
| Web search | DeepSeek API key | Official search tool auxiliary model |
| Document processing | DashScope API key | DocumentMind parsing of uploaded material |
| Semantic search | SiliconFlow API key | Embeddings and reranking for evidence-backed retrieval |

Official DeepSeek chat uses three keys total: DeepSeek, DashScope and SiliconFlow.
Other cloud providers use their own chat key plus the document and semantic keys.
Local Ollama does not need a chat-provider key, but the other services still do.
Provider usage is charged to the user's accounts.

The [provider and protocol matrix](docs/LOCAL_INSTALL.md#provider-and-protocol-matrix)
lists supported routes and capability limits.

## What ForgeFlow delivers

| Capability | Business outcome |
| --- | --- |
| **Evidence-backed investigation** | Connect the disruption to affected orders, existing supply, inventory, and relevant business rules. |
| **Constraint-based recovery planning** | Use OR-Tools CP-SAT to calculate purchase and manufacturing plans under the selected scenario's quantity, timing, resource, and economic constraints. |
| **Approval-bound execution** | Bind a specific plan to an authorized decision, then apply its recorded business actions. |
| **Independent ERP verification** | Read the resulting records back from the ERP and keep the plan, execution journal, and verification result connected. |

## Benchmark results

**100/100 on both selected ERP-Bench recovery tasks.**

| Recovery task | Original verifier score | Applicable rules passed |
| --- | ---: | ---: |
| Supplier cancellation — task `2280` | **100 / 100** | **51 / 51** |
| Workcenter outage — task `2299` | **100 / 100** | **95 / 95** |

*Recorded product-approval workflow acceptance · Odoo 19 synthetic scenarios ·
October 2026 · evaluated source `bf880f2` · original ERP-Bench verifier.*

[Supplier-cancellation report][a-report] ·
[Supplier-cancellation raw score][a-score] ·
[Workcenter-outage report][b-report] ·
[Workcenter-outage raw score][b-score]

These benchmark records are preserved at a fixed public commit. The current
source tree includes the Odoo 20 integration; the table identifies the environment
used for these recorded results.

## One recovery case

A supplier withdraws a **3-unit purchase** linked to a **13-unit sales order**.
The recovery plan coordinates **five sales orders totaling 50 units**.

ForgeFlow replaces the failed purchase while retaining the valid supply
arrangements:

![Supplier recovery case: 3 units of failed supply, 13 units of directly linked demand, and 50 units across five sales orders. Planned coverage uses 7 stock units, 40 retained manufacturing units, and 3 replacement units.](assets/readme/forgeflow-recovery-case-en.png)

**Planned supply:** 7 units from finished stock + 40 from retained manufacturing +
3 from replacement purchasing = **50 units**.

For the directly linked order, the plan covers all **13 units** from the combined
finished stock, retained manufacturing, and replacement supply.

The approved recovery produces **two purchase-document actions** and **seven
verified execution steps**, followed by the **51/51** original-verifier result.

[Recorded case and plan][case-record] · [Execution receipt][a-execution]

## Architecture

ForgeFlow separates model-driven investigation, numerical planning, and business
execution. The control layer persists task progress and validates the authority to
move from one stage to the next.

![ForgeFlow recovery architecture: Business inputs to Router, Investigation Worker, CP-SAT planner, Human approval, Executor, Verifier, and a tracked outcome; shared Control Engine retains context, tasks, durable actions, and authorization.](assets/readme/forgeflow-architecture-en.png)

The workbench assistant explains scoped business evidence through read-only tools.
The investigation runtime can produce structured plan requests. CP-SAT solves the
numerical constraints, and the controlled execution path applies the approved
actions.

Source entry points:
[Router and workers](src/forgeflow/superagent/workers.py) ·
[Plan preparation, execution, and verification](src/forgeflow/superagent/plan_effects.py) ·
[API and workbench operations](src/forgeflow/api/app.py)

## Knowledge and business facts

Document knowledge and ERP records serve complementary purposes. Documents supply
policy and product context; structured ERP queries establish the identifiers,
quantities, dates, and states needed for a recovery decision.

The source includes document-processing and semantic-retrieval modules. The
semantic answer adapter applies server-resolved tenant, document-generation and
access-control scope before retrieval, and supports configured embedding and
reranking services.

[Scoped semantic answers](src/forgeflow/knowledge/semantic_answer.py)

## What the local workbench includes

| Capability | Included workflow |
| --- | --- |
| Ask with evidence | Stream an assistant answer, attach a temporary file or supported image, and open versioned source citations |
| Build a knowledge base | Upload enterprise material, inspect parsing status and original blocks, and retrieve published documents within the signed-in scope |
| Try the three examples | Ask about procurement policy, inspect a reviewed parameter comparison, or explore the clearly labeled historical synthetic recovery case |
| Inspect ERP facts | Look up supported purchasing, sales, inventory, BOM and manufacturing records in the local Odoo instance |
| Investigate disruptions | Follow source events and tracked tasks through evidence collection and recovery preparation |
| Plan recovery | Prepare supported multi-product purchasing and multi-level BOM / multiple-MO alternatives under explicit constraints |
| Review and execute | Approve a specific plan and action set, apply authorized changes to the local ERP, then inspect receipts and independent readback |
| Resume work | Keep configuration, documents, tasks and ERP records across normal stops and container replacement |

Chat remains read-only. Business changes require the authorized approval and
execution path. The included ERP records are synthetic, but approved actions
operate on a real local Odoo database. This is separate from the archived example,
which is read-only.

[Workbench guide](docs/en/workbench-guide.md) ·
[Technical guide](docs/en/technical-guide.md) ·
[Capability boundaries](docs/en/current-capabilities.md)

## Stop, update keys and preserve your data

- **Stop:** run `Stop-ForgeFlow.cmd` on Windows or `bash stop.sh` on macOS/Linux. The launcher drains active work and preserves all named Docker volumes.
- **Resume:** run the same start launcher. Existing installation identity and data are reused; startup is not a factory reset.
- **Change keys:** run `Start-ForgeFlow.cmd -Configure` or `bash start.sh --configure`. Leave an existing key blank to retain it; changing chat provider requires its key.
- **Upgrade:** stop the old installation successfully, back up its named volumes, and follow the new release's migration notes before starting its package. There is no automatic database rollback. Do not delete volumes to fix a startup error.

[Operations and troubleshooting](docs/LOCAL_INSTALL.md#stop-resume-and-upgrade).

## Source setup

### Requirements

- Python **3.12–3.14** and `uv`
- Node.js **20.19+ or 22.12+**, with npm

### Install dependencies and build the frontend

```bash
git clone https://github.com/KevinXia69/ForgeFlow.git
cd ForgeFlow

cp .env.example .env
uv sync --frozen --extra planning

(
  cd web
  npm ci
  npm run typecheck
  npm run build
)
```

The frontend build is written to `web/dist` and is served under `/app/` by the
API. These steps prepare the source checkout and frontend assets; they do not
initialize the local ERP, control database or knowledge index.

### Runtime configuration

The application provides the following API entry point for a configured
environment:

```bash
uv run forgeflow api --host 127.0.0.1 --port 8000
```

Set the API configuration in the process environment before starting it:

| Setting | Purpose |
| --- | --- |
| `FORGEFLOW_SESSION_SECRET` | Session signing secret, at least 32 characters |
| `FORGEFLOW_INVITE_CODES` | Comma-separated invitation codes, each at least 16 characters |
| `FORGEFLOW_DEPLOYMENT` | Deployment mode; use `local` for local development |
| `FORGEFLOW_SECURE_COOKIE` | Use `false` only for plain-HTTP loopback development; keep `true` for HTTPS |

For the persistent Odoo 20 workflow, configure a PostgreSQL control database, an
Odoo 20 instance with the ForgeFlow add-on, and the required service processes.
Model, semantic-search and document-parsing services are configured for the
features being used. Merely copying `.env.example` does not initialize these
services or load their data.

[API settings](src/forgeflow/api/settings.py) ·
[CLI entry points](src/forgeflow/cli.py) ·
[Python dependencies and optional features](pyproject.toml)

## Tests and release gates

Run from the repository root:

```bash
uv run --extra planning pytest tests -q

(
  cd web
  npm run typecheck
  npm run build
)
```

The suite includes external-service and upstream-fixture integrations. Configure
the relevant services for those checks and retain passed, failed and skipped
counts in the test report.

Downloadable packages are created only after native launcher checks on Windows,
macOS and Linux, plus real empty-stack tests on Linux amd64 and arm64. They cover
synthetic ERP and fixture external services; they do not certify every provider
account or every Docker Desktop host. Target-environment and customer acceptance
remain separate requirements. Publishing an image does not approve customer
production writes.

See the [installation acceptance section](docs/LOCAL_INSTALL.md#installation-acceptance)
for the exact scope and limits.

## Repository layout

| Path | Contents |
| --- | --- |
| `src/forgeflow/` | Python application, FastAPI backend, assistant runtime, planning and ERP integration |
| `src/forgeflow/superagent/` | Routing, investigation, persistent control and recovery effects |
| `src/forgeflow/planning/` | Constraint models and action preparation |
| `src/forgeflow/integrations/odoo20/` | Odoo 20 business integration |
| `src/forgeflow/knowledge/` | Document and knowledge services |
| `src/forgeflow/local_workbench/` | Local installer setup, bootstrap and runtime |
| `web/` | React and TypeScript workbench |
| `installer/` | Windows, macOS and Linux launchers |
| `deploy/workbench/` | Local Docker Compose and image definitions |
| `odoo-addons/` | ForgeFlow Odoo add-on code |
| `migrations/` | PostgreSQL control-layer migrations |
| `contracts/` | Task-policy contracts |
| `tests/` | Source-level and integration tests |
| `docs/LOCAL_INSTALL.md` | Local installation, provider and operations guide |

## Public source and upstream projects

This repository contains the application source, Odoo add-on, migrations,
contracts, web client, local installer and tests. Runtime data, upstream benchmark
fixtures, reference solutions, model weights and production credentials are
supplied separately. The recorded benchmark links above resolve to the public
history.

ForgeFlow builds on ERP-Bench, Pi, DeepSeek Harness, Odoo, PostgreSQL, Harbor and
Google OR-Tools. Upstream attribution and version details are maintained in
[NOTICE](NOTICE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Security

Keep credentials and customer records out of the repository. Use dedicated test
ERP instances and synthetic data for experiments. Human approval and server-side
authorization govern business execution. Report security issues privately to the
repository owner.

## License

ForgeFlow is licensed under [Apache License 2.0](LICENSE).
Third-party materials retain the licenses recorded in [NOTICE](NOTICE).

[a-report]: https://github.com/KevinXia69/ForgeFlow/blob/9c673c8d479b238a18c489fab0dbac057fb72977/docs/evidence/2026-10-28-next-phase/A-final/report.json
[a-score]: https://github.com/KevinXia69/ForgeFlow/blob/9c673c8d479b238a18c489fab0dbac057fb72977/docs/evidence/2026-10-28-next-phase/A-final/original-reward.raw.json
[b-report]: https://github.com/KevinXia69/ForgeFlow/blob/9c673c8d479b238a18c489fab0dbac057fb72977/docs/evidence/2026-10-28-next-phase/B-final/report.json
[b-score]: https://github.com/KevinXia69/ForgeFlow/blob/9c673c8d479b238a18c489fab0dbac057fb72977/docs/evidence/2026-10-28-next-phase/B-final/original-reward.raw.json
[case-record]: https://github.com/KevinXia69/ForgeFlow/blob/9c673c8d479b238a18c489fab0dbac057fb72977/README.md#one-case-from-disruption-to-verified-result
[a-execution]: https://github.com/KevinXia69/ForgeFlow/blob/9c673c8d479b238a18c489fab0dbac057fb72977/docs/evidence/2026-10-28-next-phase/A-final/executed.json
