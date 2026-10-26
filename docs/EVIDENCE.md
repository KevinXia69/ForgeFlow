# What can be verified from this repository

This page exists so a reader can find the proof without a folder of run logs.
Harness checks are reproducible from the pinned commit. Hosted observations are
identified separately as retained operator captures, with their source/image
binding and unmeasured boundaries.

## Independent reviewers and local installation

The [October 25 Pacific / October 26 UTC reviewer record](evidence/2026-10-26-reviewers/README.md)
tests two independent disposable environments with concurrent preparation,
real model answers, approved ERP execution, original acceptance, duplicate
approval and cross-instance isolation. It measures unchanged row counts and
ordered row-content hashes in twelve named business tables before and after
preparation and repeated approval; it does not claim to hash every Odoo table.
Public routing and the five untouched official workspaces have separate checks
in that record. The deployment targets sequential judges with occasional
two-person overlap, not five simultaneous heavy operations.

The [isolated local installation record](evidence/2026-10-25-local-bootstrap/README.md)
documents a new image and database, locked dependency installation, real case
planning and preservation after stop/resume. Its original failures and fixes
are retained. It did not approve business writes or call a model.

## Initial hosted acceptance

The [2026-10-25 VPS record](evidence/2026-10-25-vps/README.md) publishes the new
case, exact-plan approval, real Odoo writes/readback, original reward, isolated
Odoo/PostgreSQL recovery, reviewer rotation, and complete OS reboot evidence.
It is bound to deployment source `67bcb95e3253c245dab49478458aab652af8d05a`,
not to the older frontend fixture. The original reward's 51/51 passed rules and
100 score can be read directly in the repository.

The record distinguishes application recovery inside the isolated Odoo container
from full public ForgeFlow API/approval recovery. A new approval after reboot,
an independent preparation-only zero-write measurement, and whole-site failover
on another VPS are not claimed. Publication makes the operator captures
inspectable; it does not turn them into an independent audit.

## The claim, and where it is checked

| Claim | How it is checked today |
| --- | --- |
| A case moves exception → plan → one approval → apply → readback → original acceptance | [VPS live capture](evidence/2026-10-25-vps/live-acceptance.json) and [original reward](evidence/2026-10-25-vps/original-reward.json); interface replay: `web/e2e/verify-approval-gate.mjs`, `verify-success-narrative.mjs` (mock API, deterministic) |
| The approval gate never approves a plan with an unrecorded required check | `web/e2e/verify-approval-gate.mjs` (22 assertions) |
| The private workbench and the English review bundle tell the same story | `uv run python scripts/check_web_parity.py`, `web/e2e/verify-bundle-parity.mjs` |
| The interface is readable at 14px and meets WCAG AA contrast | `scripts/check_typography.py`, `web/e2e/contrast.mjs` |
| A finished turn survives a process restart and is not re-billed to the model | `tests/test_api_stream_turns.py::test_completed_turn_survives_a_restart_at_the_api_level` |
| The whole suite passes on a clean clone | `python3 scripts/fetch_upstream.py && uv sync --frozen && uv run pytest -q` |

Run everything at once: `bash scripts/first_look_audit.sh` (starts its own
read-only API, runs 11 checks, writes screenshots to `.runtime/`).

## What is simulated, and what is not

- **Simulated**: the UI harnesses above drive a mock or a fake executor. They prove
  the interface and the decisions are consistent, not that an ERP was written.
- **Real**: the acceptance programs that judge a run are the task's own, taken
  from ERP-Bench; a run's score comes from them, never from this repository.
- **Real sandbox execution**: the VPS record contains actual Odoo writes and
  readback over synthetic benchmark data. Synthetic data does not imply a fake
  executor, and real sandbox execution does not imply customer production ERP.
- **Real model**: the autonomous research path calls Nebius Token Factory. Where
  a run's evidence is quoted, its run id is the source; no score here is
  produced by the mock.

## Where the numbers come from

The older case used by the acceptance harnesses is a real recorded run, kept in the
tree at [`web/e2e/fixtures/approved-case.json`](../web/e2e/fixtures/approved-case.json).
It is the receipt behind the figures quoted on the front page, not the receipt
for the new VPS deployment:

| Field in that file | Value |
| --- | --- |
| `execution.verifier.passed_rule_count` / `applicable_rule_count` | 51 / 51 |
| `execution.verifier.score` | 100.00 |
| `execution.writes` | 2 documents |
| `execution.readback.records` | 2 records |
| `result.strict_success` | true |

Business identifiers in it are the benchmark's own synthetic records. No
credential, session key or invite code appears in it, and no test approves
anything: the harnesses replay the recorded case against a mock API.

The full run directories (prompts, tool traces, model output, verifier logs) are
not shipped. They live with the run records and contain execution traces that do
not belong in a product repository.

## Status detail

This is the list the README summarises, kept here so the front page stays about
the product.

| Area | State |
| --- | --- |
| Product path: case, plan, one approval, apply, readback, acceptance | Verified, with a receipt and re-runnable assertions |
| Local workbench and English review bundle | Both run; 8 parity comparisons and 26 accessibility checks agree |
| Contrast | 193/193 text runs (workbench), 196/196 (review bundle) meet WCAG AA |
| Screen reader | Not measured. Keyboard, contrast and reflow are |
| Public deployment | Hosted Scenario A live workflow recorded on 2026-10-25; [receipt and limits](evidence/2026-10-25-vps/README.md). Isolated Odoo recovery and normal OS reboot passed; whole-site disaster failover and a new approval after reboot remain untested |
| Human usability review | Removed from the plan; there are no human-subject findings |
| Autonomous research path | Three consecutive runs completed and failed: three rules unmet, zero score, and the recorded plan was never applied |
| Partly exercised capabilities | Recorded as not triggered, never counted as done |

These are boundaries and unmeasured areas, not known defects: the defects found
during the last review round were fixed before publication, and each fix has a
test (see the commit history and `tests/`).
