# Current capabilities and metric definitions

This is the current documentation entry point. Deployment evidence is versioned:
[October 25](../evidence/2026-10-25-vps/README.md) and
[October 26](../evidence/2026-10-26-reviewers/README.md) describe historical runs.
A source change does not establish that the hosted environment has been upgraded.
Use a release's recorded commit, plan basis and fresh acceptance evidence to
identify the running version.

## Product workflow and supported scope

The hosted judge workflow supports Scenario A in an isolated, persistent Odoo
sandbox: prepare, inspect, choose, explicitly approve, execute, read back and
run the original verifier. Local/test environments also support Scenario B.
The read-only review bundle cannot execute unless a deployment explicitly
provides and authorizes that capability. Autonomous research runs are separate
from the human-approved product workflow. The assistant does not approve writes.
See the [judge guide](judge-guide.md) and [deployment guide](deployment.md).

Complete recovery execution supports one finished product and a single-level
BoM. Read-only collection can follow multi-level acyclic BoMs; collection does
not establish complete execution support for those models. The
`planning-metadata-v1` profile copies existing standard minutes, manufacturing
cost and lead time into workcenter notes. It changes visibility, not those
business values; results must identify this profile.

## New-spend margin

For `new_spend_coverage_v2`, revenue is the sum of sales prices multiplied by
finished quantities supplied through purchase or manufacturing. Finished stock
is excluded. Planned cost includes all planned purchases and manufacturing,
including retained documents; it is neither new-document value nor a cost delta.

```text
new-spend margin = 1 - planned cost / purchase-and-manufacturing coverage revenue
maximum planned cost = coverage revenue × (1 - effective floor)
cost headroom = maximum planned cost - planned cost
```

The effective floor is the stricter of the original floor and a user's added
floor. Customer budgets use each complete sales order's value, separately.
Zero revenue and zero cost produce a not-applicable margin, not a fabricated
100%; positive revenue with zero cost produces 100%. Missing evidence is unknown.

New metrics use schema version 2 and bind to the plan and snapshot. Historical
version 1 values retain their original meaning and are labeled legacy; they do
not establish current margin compliance. An old unstarted plan needs a fresh
preparation and approval. Partial or uncertain execution must follow its original
journal and the controlled recovery procedure, not a new-plan replay.

## Candidate objectives and acceptance

| Preference | Optimization priority | Tradeoff |
| --- | --- | --- |
| Preserve quantities (default) | Minimize quantity deviations on existing purchase/manufacturing paths, then cost | Old manufacturing retention is optimized within the quantity plan; no global minimum ERP-write claim |
| Minimize planned cost | Minimize modeled purchase/manufacturing cost | May cancel usable commitments; changes original preservation priority |
| Preserve manufacturing capacity | Minimize standard manufacturing minutes | May increase purchasing and reduce retention; changes original preservation priority |

Candidates share facts, hard constraints and cost definitions. Objective,
selection and approval are bound to the chosen plan. Original acceptance still
scores the task's repair objective, with unchanged rules and reference result.
Passing business constraints need not produce full original-task marks for an
alternative objective. Always show the actual recorded score.

## Scope and execution counts

| Scenario | Total planning demand | Directly linked order demand | Identified failed supply |
| --- | --- | --- | --- |
| A | 5 orders, 50 units, due in 8–10 days | Alpine Theater, 13 units, due in 10 days | One 3-unit finished-goods purchase |
| B | 12 orders, 228 units | Six orders, 112 units | Three manufacturing orders, 53 + 37 + 2 = 92 units |

These are benchmark facts, not general logic or counts to hardcode for new cases.
The UI follows recorded disruption relationships; missing relationships remain
unknown. Linked demand is not failed supply, and missing direct links do not rule
out indirect effects. Receipts distinguish business document actions, document
readbacks and verified execution steps. None is a database-write count. Retained
manufacturing quantity does not imply unchanged related workorder records.

## Optional retention experiment

The default-off `fixed-totals-retention-v1` experiment applies only to the default
repair objective after its original primary and cost optima are proven. It fixes
purchase, workcenter and stock totals, then adjusts supported allocations to
retain more original manufacturing quantity. It does not establish a global
minimum number of document changes or ERP writes.

The optional plan metadata distinguishes `improved`, `unchanged`, `fallback`
and `skipped`, with retained quantity before/after and a bounded reason.
Timeout, unsupported scope, unproven optimum or failed validation leaves the
independently validated original plan in use. Choosing cost/capacity priority
skips this experiment. An improved plan receives its own identity and approval.
