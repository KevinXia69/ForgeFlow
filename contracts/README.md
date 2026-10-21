# Task policy contracts

This directory holds the human-reviewed, version-locked `TaskContract` files
(schema 1). `src/forgeflow/contract.py` parses and validates them.

## What it does

A contract turns the policy in the task statement into something a machine can
check: product, anchor date, order scope, per-order budget caps, margin basis,
disruption identity and forbidden workcenters. `check_request` rejects a request
that disagrees with it before solving; `check_changes` rejects a change set that
violates it before execution.

It covers only the information the task statement allows and the business reads,
and **never** comes from `solution/`, a verifier or any reference answer.

## Rules

- Values must match the task statement. A budget tighter than the contract is a
  conflict too — "more conservative" does not excuse reading the wrong field.
- A display name cannot stand in for an identifier; mixing them fails with
  `identifier_is_display_name` instead of comparing silently.
- Physical actions (receipt confirmation, production completion, stock
  adjustments) are not contract options: the executor accepts only the ten
  planned document actions in `recovery/journal.py`, so such an action cannot
  happen structurally.
- `forbidden_operation_kinds` accepts only `purchase_order` /
  `manufacturing_order`, and it **forbids** that kind of planned document; a
  downed workcenter is constrained separately by `forbidden_workcenters`.

## Files

| File | Task | Contract summary |
| --- | --- | --- |
| `2280-supplier-cancellation-v1.json` | supplier cancellation | 5 sales orders, budgets 3,735–6,828, margin 27.5%, vendor `r27637E67E7_q02` |
| `2299-workcenter-outage-v1.json` | workcenter outage | 12 sales orders, total budget 943,302, margin 26%, downed workcenter `P1223565D60-WC01` |
