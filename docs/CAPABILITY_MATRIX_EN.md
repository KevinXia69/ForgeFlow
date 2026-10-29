# ForgeFlow Capability Matrix (Public-Safe)

> Historical document. Deployment and capability statements below describe their recorded date and are not the current capability overview. See [current capabilities](en/current-capabilities.md) and the [judge guide](en/judge-guide.md).

Status as of 2026-10-22. Scope: synthetic ERP-Bench repair-plan tasks, single-level BoM, 600-second agent budget. This matrix contains no customer data, credentials, or endorsement claims.

| Capability | Status | Evidence | User-visible label |
| --- | --- | --- | --- |
| `project_qa` | implemented | Cited-document Q&A is available; a 25-question Chinese acceptance set across 10 categories passed. English corpus acceptance is not yet recorded. | Document Q&A |
| `historical_review` | implemented | Read-only views for runs, plans, approval records, and evidence are available; the public review instance is read-only. | Evidence review |
| `live_prepare` | local-only | Real Odoo prepare/plan runs in the local benchmark runner. Development A passed 3/3 and B passed 2/3 strict; strict task success in G4 was 2/4. Not exposed by the hosted product. | Live plan (local demo) |
| `live_execute` | local-only | A controlled executor writes to a local Odoo container, reads back the result, and the original verifier scores it. The public review instance cannot execute. | Controlled execution (local demo) |
| `product_approval` | local real executor | On a Mac with a local Odoo sandbox and host root executor, prepare → browser approval → same-plan apply → readback → original verifier passed at 100.0 (51/51); public review still shows a labelled fixture because no executor is injected. | Live approval (local workbench) |
| `worker_available` | protocol only | An in-memory worker lease protocol and tests exist; no deployed worker or Docker socket is connected. | Live worker (offline) |

## Status meanings

- **implemented**: available in the current product surface, with evidence from the repository.
- **local-only**: demonstrated only in a local benchmark or demo environment; not available on the hosted instance.
- **not implemented**: no end-to-end capability; partial components or tests may exist.

## Reading notes

- All live results are small-sample benchmark results, not a success rate, production SLA, or claim of general repair-plan coverage.
- The public site supports review, knowledge Q&A, and evidence inspection only. It cannot start or execute a live Odoo task.
- The product approval chain is real on the local workbench; public live execution, always-on laptop deployment, final video/slides, and any factory deployment remain outstanding.
- No customer factory, production data, endorsement, or pilot commitment is claimed.
