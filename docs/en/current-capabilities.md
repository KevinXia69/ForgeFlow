# Current capabilities and limits

## What is ForgeFlow?

ForgeFlow is a manufacturing exception-recovery workbench. It connects evidence
about a supplier cancellation or production disruption to a constrained recovery
plan, human approval, controlled ERP changes and independent verification.

The local distribution packages the same current API, planning, Odoo 20 add-on
and assistant interfaces as the application source. Installation and feature
acceptance are recorded separately; source availability is not a passed test.

| Capability | Included implementation | Boundary |
| --- | --- | --- |
| Assistant | DSH stream, scoped tools, images, temporary attachments, citations | Needs a configured model with the requested modalities; no chat approvals or ERP writes |
| Knowledge | Document parsing, versioned blocks, semantic retrieval and reranking | Needs document and semantic service credentials; publication and ACL remain enforced |
| Reviewed comparison | Version/hash-bound reference facts with source controls | Only explicitly reviewed models and parameters; other questions follow the general assistant path |
| Historical example | Archived synthetic A plan and impact graph | Read-only, never substituted for current ERP records |
| ERP facts | Odoo 20 queries for supported purchase, sales, stock and production objects | Requires the local binding and permitted company; unknown relations remain unknown |
| Recovery | Multi-product procurement, multi-level BOM and multiple-MO planning | Bounded supported inputs and action types; no automatic material substitution or arbitrary factory scheduling |
| Human decisions | Plan-version/action-bound approval and scoped delegation | Revocation, expiry, actor role and company checks apply in Odoo as well as the API |
| Execution | Controlled apply, per-operation receipts and independent readback | An incomplete receipt set cannot establish success; unknown outcomes require reconciliation |
| Background work | Source polling, investigation dispatch, watchdog and outbox | Durable task state; restarting services must not replay an unknown business operation |

Current code intentionally distinguishes feasible, optimal, infeasible and unknown
solver results. Quantity, unit, calendar, capacity and missing-fact limits remain
part of the planning contract. A local synthetic exercise does not authorize
connecting to or changing an existing customer ERP.

The historic Odoo 19 benchmark records (51/51 and 95/95 applicable rules) identify
their own source versions and original verifiers. They do not mean the current
Odoo 20 distribution has passed every workflow on every operating system.
Use the release's installation acceptance report for the tested host, architecture,
provider configuration and passed/failed/skipped checks.

[Installation](../LOCAL_INSTALL.md) · [Technical guide](technical-guide.md) ·
[Workbench guide](workbench-guide.md) · [Product and evidence guide](product-guide.md)
