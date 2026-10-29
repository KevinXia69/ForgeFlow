# What can be verified from this repository

Evidence is grouped by the code revision and environment it actually tested.
Current local acceptance, historical hosted observations and autonomous research
are separate records. A source update does not establish a public deployment
upgrade, and a UI replay does not establish an ERP write.

## Current local A and B acceptance

The [October 28 next-phase record](evidence/2026-10-28-next-phase/README.md)
tests business code `bf880f2f1a8881809a6da2d89bcd3ba6724f4f81` against pinned
ERP-Bench `ceba3880af555129b5278e056a0c20f2fb5a0ba9`. These are fresh local
Odoo sandboxes with synthetic benchmark data and real ERP writes. This release
did not update the public deployment.

| Recorded result | Scenario A | Scenario B |
| --- | --- | --- |
| Full planning scope | 5 orders / 50 units | 12 orders / 228 units |
| Directly linked demand / failed supply | 13 / 3 units | 112 / 92 units |
| Planned purchasing and manufacturing cost | USD 4,794.94 | USD 133,394.07 |
| New-spend margin | 76.74981077% | 79.51385318% |
| Original applicable rules passed / score | 51/51 / 100 | 95/95 / 100 |
| Business actions / verified execution steps | 2 / 7 | 23 / 34 |
| Sources | [Report](evidence/2026-10-28-next-phase/A-final/report.json), [selected plan](evidence/2026-10-28-next-phase/A-final/selected.json), [executed case](evidence/2026-10-28-next-phase/A-final/executed.json), [original reward](evidence/2026-10-28-next-phase/A-final/original-reward.raw.json) | [Report](evidence/2026-10-28-next-phase/B-final/report.json), [selected plan](evidence/2026-10-28-next-phase/B-final/selected.json), [executed case](evidence/2026-10-28-next-phase/B-final/executed.json), [original reward](evidence/2026-10-28-next-phase/B-final/original-reward.raw.json) |

Both approvals came from an explicitly authorized HTTP acceptance driver. They
are not human browser approvals or autonomous agent benchmark runs. Preparation,
restart and duplicate-approval checks compare eight named business tables;
the reports do not claim to measure every database write. Source manifests and
original reward hashes bind each record to its tested inputs.

Costs include retained purchasing and manufacturing arrangements. The margin
denominator covers 43 purchased/manufactured units for A and 168 for B, excluding
7 and 60 units of finished stock respectively. These are planning values, not
measured accounting results. The [current capability guide](en/current-capabilities.md)
defines the calculation and the supported one-product, single-level-BoM scope.

The same evidence index separates two recovery checks:

- [Real ERP response-loss recovery](evidence/2026-10-28-next-phase/recovery-final/report.json)
  drops the response after a successful apply, then recovers the same receipt:
  one approval, one apply, seven verified steps and 51/51 original rules.
- [Legacy upgrade exercise](evidence/2026-10-28-next-phase/legacy-upgrade-final/report.json)
  uses artifacts produced by actual old code with a synthetic ERP to test six
  migration states. It is not a real ERP cross-version partial-execution migration.

The optional retention refinement is disabled by default. Solver and integration
tests cover its bounded objective; this evidence does not claim an experimental
ERP deployment or globally minimal ERP edits.

## Historical hosted Scenario A and installation records

The [October 25 VPS record](evidence/2026-10-25-vps/README.md) contains a fresh
case, exact-plan approval, Odoo writes and readback, original reward, isolated
Odoo/PostgreSQL recovery, reviewer rotation and a complete OS reboot. It is bound
to deployment source `67bcb95e3253c245dab49478458aab652af8d05a`. Its original
reward records 51/51 passed rules and a score of 100.

That record distinguishes isolated Odoo recovery from full public API/approval
recovery. A new approval after reboot and whole-site failover on another VPS
were not measured. These are retained operator captures, not an independent
audit or evidence that the latest source is deployed.

The [October 25 Pacific / October 26 UTC reviewer record](evidence/2026-10-26-reviewers/README.md)
tests two disposable workspaces with concurrent preparation, real model answers,
approved Scenario A ERP execution, original acceptance, duplicate approval and
cross-instance isolation. It measures row counts and ordered row-content hashes
in twelve named business tables. Public routing and the five untouched official
workspaces have separate checks. This records occasional two-person overlap,
not five simultaneous heavy operations or the current local A/B release.

The [October 25 isolated installation record](evidence/2026-10-25-local-bootstrap/README.md)
documents a new image and database, locked dependency installation, real planning
and preservation after stop/resume. Original failures and fixes are retained.
It did not approve business writes or call a model.

## Tests and interface checks

| Check | Reproduction entry or recorded result | What it establishes |
| --- | --- | --- |
| Backend regression suite | [Final validation](evidence/2026-10-28-next-phase/validation/backend.json); `uv run pytest -q` after pinned dependency setup | 3,062 tests and 28 subtests passed; 40 environment or historical-material checks skipped with reasons in the [log](evidence/2026-10-28-next-phase/validation/backend-bf880f2.txt). This is not a hosted acceptance run. |
| Both frontend builds, pure tests, smoke and parity | [Frontend validation](evidence/2026-10-28-next-phase/validation/frontend.json) records individual commands, source revisions and results | Separates build checks, mock/demo checks and real-case reads. |
| First-use interface audit | `bash scripts/first_look_audit.sh`; [recorded result](evidence/2026-10-28-next-phase/validation/first-look.json) | Starts a read-only API with copied historical cases and runs 11 UI/API checks. It never starts the ERP sandbox or executes approval. The retained run is from `74a990a`, before final CSS. |
| Final real-case layout | [Layout script](../web/e2e/checks-layout.mjs), [result](evidence/2026-10-28-next-phase/validation/checks-layout.json) | Reads existing A/B cases in English/Chinese at 1440/390 widths. Eight combinations passed; the script creates no case and approves no plan. |
| Mock approval and receipt behavior | `web/e2e/verify-approval-gate.mjs`, `web/e2e/verify-success-narrative.mjs` | Exercises interface behavior against recorded fixtures and a mock API, not ERP execution. |
| Completed model-turn persistence | `tests/test_api_stream_turns.py::test_completed_turn_survives_a_restart_at_the_api_level` | Tests the completed-turn replay contract without initiating a second model request. |

The first-look command is an interface audit, not a command for the entire test
suite or the business approval workflow. [Screenshots and their source manifest](media/next-phase/README.md)
identify final A captures separately from earlier persisted A/B cases rendered
with final CSS. Keyboard, contrast and reflow checks do not constitute screen
reader validation or human usability research.

## Historical fixture and autonomous research

[`web/e2e/fixtures/approved-case.json`](../web/e2e/fixtures/approved-case.json)
is an older recorded case replayed by UI harnesses. It retains its historical
values and margin semantics; it is not the source for the current README's A/B
figures. Its 51/51 rules, `execution.verifier.score` of `100.00` and two action
records belong to that older case. Replaying it approves nothing in an ERP.

The autonomous path calls Nebius Token Factory and is evaluated separately from
the product's explicit approval path. The [dated research record and limits](en/limits.md#historical-v9-validation-boundary-2026-10-23)
retain individual run IDs, failures, timeouts and bounded successes. For example,
the separate 2283 retry passed 45 original rules, while the 2285 retry timed out
without a prepared plan; neither is a general success-rate estimate. Later
product A/B acceptance does not replace or upgrade those research results.

Full private run directories contain prompts, tool traces and model output and
are not shipped wholesale. Published summaries and receipts state their own
measured scope. Use the [current capability guide](en/current-capabilities.md)
for implementation boundaries and the dated records above for what was actually
observed.
