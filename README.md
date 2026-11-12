# ForgeFlow

ForgeFlow is a workbench for manufacturing production and delivery exceptions.
It follows a concrete product loop:

```text
case → plan → one-time approval → controlled execution → ERP readback → original verifier
```

Business chat is read-only. Only a company-authorized approver can approve a
specific plan; execution is performed by the controlled executor and checked
against the ERP afterward.

## What is ForgeFlow?

- **Case:** identifies affected orders, failed supply, existing commitments and
  the current decision state.
- **Plan:** uses OR-Tools CP-SAT to compute a feasible recovery plan under
  deadlines, budgets, stock, supplier and capacity constraints.
- **Approval:** binds a specific plan and the captured ERP state to an expiring
  approval.
- **Execution:** applies only the approved plan and keeps an operation journal.
- **Verification:** reads the ERP back and records the original task verifier's
  result.

The optional assistant is a read-only DSH agent that explains scoped case and
execution evidence through tools. It cannot approve or execute. CP-SAT remains
the planning solver; the assistant is not the planner.

## What is in this repository

- `src/forgeflow/`: FastAPI backend, Odoo 20 integration, control layer,
  planning integration and assistant runtime.
- `web/`: React + TypeScript workbench source. It builds into `web/dist`.
- `tests/`: source-level and integration tests. Some deep benchmark tests need
  the upstream fixtures described in `NOTICE`.
- `contracts/`: reviewed task-policy contracts.
- `migrations/`: PostgreSQL control-layer migrations.
- `odoo-addons/`: ForgeFlow Odoo add-on code.
- `pyproject.toml`, `uv.lock`: Python dependencies.
- `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md`: license and attribution.

This is a source subset, not a distribution of upstream task data, model
weights, container images, customer data or production credentials.

## Requirements

- Python 3.12 or newer
- `uv`
- Node.js 20.19+ or 22.12+ and npm for the web client

## Setup

```bash
cp .env.example .env
# Fill in only the credentials needed for your local setup.
uv sync --frozen
cd web
npm ci
npm run typecheck
npm run build
```

The built frontend is served under `/app/` by the ForgeFlow API. See
`deploy/` only if you are adapting the example deployment; do not reuse
production credentials or private infrastructure values.

For a local configuration, set at minimum:

```text
FORGEFLOW_SESSION_SECRET
FORGEFLOW_INVITE_CODES
FORGEFLOW_DEPLOYMENT=local
```

The assistant and model bindings are optional. The planning, approval and
execution paths work independently of the chat model.

## Tests

```bash
uv run --extra planning pytest tests -q
```

Some tests are skipped when their external database, Odoo or upstream fixture
is not configured. A skipped test is not a successful integration run.

## Security

- Never commit `.env`, API keys, session secrets, invitation codes or customer
  data.
- Use a dedicated test ERP and synthetic records for local experiments.
- The assistant is read-only; approval and execution require the product's
  authorization path.
- Report security issues privately to the repository owner rather than opening
  a public issue with credentials or exploit details.

## License

ForgeFlow is licensed under the Apache License 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE) for the license text and third-party attribution.
