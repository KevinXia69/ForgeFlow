# Failures, Limits, and Known Gaps

ForgeFlow keeps failed runs and missing measurements in the record. The purpose
of this document is to make the boundaries explicit so that a successful run is
not mistaken for a general capability.

## Current scope and historical records

Use [current capabilities](current-capabilities.md) for supported execution scope,
margin versioning, candidate priorities and current deployment entry points.
Complete execution supports one finished product and a single-level BoM.
Default preservation is optimized within a quantity plan, not guaranteed to
minimize all ERP writes. Cost/capacity preferences change the original
preservation priority, while the original verifier keeps scoring the repair task.
Historical margins below retain their original semantics; version 2 uses
purchase/manufacturing coverage revenue and excludes finished stock.

## Historical v9 validation boundary (2026-10-23)

The following dated observations are retained as history. They are not current
hosted deployment status or proof of acceptance for a later code revision.

The Ask start page is the default entry in the ForgeFlow React workbench; Cases keeps its own route. DSH SDK/CLI is a
read-only question-answering bridge, not an approval executor. Native Web is no
longer the business homepage and is disabled by default. Review never exposes
it. Historical cases and questions remain readable without an executor; writes
require separate capability, ownership and plan-eligibility checks.

The host controls MCP evidence scope. Scope changes retire old snapshots,
SDK context and SSE replay; raw hidden reasoning is not returned as evidence.
A real runtime inspection initially exposed extra native tools; that failed
record is retained. After tightening the composition, real Nebius requests
exposed exactly six read-only MCP tools. Cross-owner denial, no writes, session
resume and HTTP SSE reconnect passed using synthetic cases; evidence is under
the installed runtime is checked, not every future
version, and does not establish an Odoo execution or a new deployment. SSE replay
is process-local. Browser refresh and Stop-then-new-question passed in both case
and knowledge chat; this is not durable generation across an API restart. A
missing resumed turn returns an explicit error and does not start another model
request. Stop ends delivery to the browser; provider interruption is not confirmed.

Executor connection health is not ERP readiness. Missing cost deltas, margins,
delivery evidence and strict verifier results remain unknown. A successful
execution or a score above a threshold alone is insufficient for a verified
outcome. Real local plan revision is unavailable until structured changes can
remain bound to the reviewed contract; a synthetic revision test is not proof
of live revision support. The legacy new_spend amount is planned purchasing and
manufacturing cost, including retained documents; it is not a cost increase.
Planned margin uses that same basis, not measured accounting margin. A failed
selected-case read suspends write actions until that case is successfully read
again, even when the connection and list requests are healthy.

Recorded plans with a dispatch journal but no complete success receipt are now
shown as unconfirmed, with possible partial execution. A prepare-only record is
not proof that no execution occurred. Corrupt or contradictory evidence remains
unknown; journal dispatch and verified counts are not committed ERP-write counts.
Both affected historical records were rechecked through the real API and browser.

The new G4 2283 run failed validly at 17.14 (42/45 rules), with an unfinished
purchase update. Its recorded agent wall time was 876.29 seconds despite a
600-second configured budget; the discrepancy remains unexplained. A separate
real sandbox reproduced a singleton JSON-2 read-shape mismatch and passed
one-shot date and quantity controls after a strict normalization fix. This does
not reconstruct the old run's unknown after-state or establish a task pass.
The new 2285 run failed validly at 0 (41/44), without calling the native recovery
tools. It therefore did not exercise the new missing-demand feedback. Source
files and isolation were verified, but its effective model tool schema was not
recorded. After the singleton fix, the separate 2283 r2 passed all 45 original
rules with valid, strict and chain checks true; agent time was 193.225 seconds.
Its single execute completed all 13 journal steps. This is one development
regression, not a stability estimate or a single-factor causal comparison. The
2285 workflow-guided r2 timed out: raw 0, 41/44 PASS, evaluation_valid=false,
environment_chain_ok=false and strict=null. Nine native prepare calls were
rejected; no prepared plan, execute or journal was recorded. Eight bounded
reasons were details_unavailable and one change_set_rejected; missing-demand
feedback was never triggered. Its real model effectiveness remains unverified.
Actual ERP write counts are unknown. Recorded agent time was 950.492 seconds
against the unchanged 600-second setting; UTC and process timing disagreed, with
cause unknown. It is not a valid success-rate sample or a within-budget result.
A bounded follow-up found eight exact broken_offer_missing errors; a naming
mismatch had hidden them behind details_unavailable. The final client maps only
that exact plan_rejected reason to the existing broken_offer_not_found enum.
Controlled live-constructor/socket/Python/JS tests passed without widening the
feedback schema or execution permissions. No new model or ERP trial validated
its effect on task completion. Historical held-out results remain
unchanged and these exposed tasks are not a new held-out set.

## G3: real policy binding demonstrated; full task still failed

The contract path exists:

- Versioned `TaskContract` files live under `contracts/`.
- `forgeflow run --contract PATH` validates the task id and writes the contract
  id, SHA-256, and task id into the run directory and manifest.
- The executor startup command writes the contract to
  `/var/lib/forgeflow/recovery/policy.json` with mode 0400 and passes
  `--policy-file`.
- `RecoveryHandlers` passes the contract into `service.prepare`.
- Unit tests cover round-trip parsing, request conflicts, policy delivery, and
  cross-task rejection.

The historical gap was a policy-bound prepared plan. Five recorded contract-mode runs,
including the 2288 contract run, ended without a prepared plan:

- `g3-2281-contract-r1`: score 0, valid, zero prepare calls; the agent never
  called prepare.
- `g3-2287-contract-r1`: score 0, valid, context/compaction failure before
  prepare.
- `g3-2287-contract-v2-r1`: score 0, valid, still ended before prepare.
- `g3-2287-contract-v3-r1`: failed, invalid, 600-second timeout.
- `g3-2288-contract-r1`: failed, invalid; 2288 passed 3/3 without a contract, but
  failed without a prepared plan under the contract.

Within that historical G3 autonomous series, no prepared plan with a real
`policy_sha256` was produced. The startup command, manifest, and
unit tests were proven; the policy end-to-end claim was not. That historical
gap is distinct from the later v9 binding result below.

Two further v9 2288 trials also timed out without a prepared plan. The second
used an explicit reviewed-request guidance factor. A bounded diagnostic found
four prepare calls: three matched the reviewed request but were rejected by the
plan contract. That unchanged contract prohibits both purchase and manufacturing
document kinds, including keep and cancel operations. This is a concrete policy
block, not proof that the model never attempted prepare. A native-tool wrapper
had also hidden bounded executor errors behind a generic message; it now forwards
only fixed codes and bounded missing-order IDs, with real process-boundary tests.

The original contract and all failures are retained. The separate
v9-g3-2280-reviewed-request-r1 trial used the existing 2280 reviewed contract and
produced one prepared plan with a matching policy SHA and recorded snapshot,
without human approval. It ended within the 600-second agent budget. This closes
the real policy-binding evidence gap; the original task still failed: 17.5 points,
47/50 rules, evaluation_valid=true and strict=false. The failed rules were
supply_timing_feasible, finished_stock_capacity_compliance and supply_coverage.
It is a different task, not a paired improvement on 2288. Raw verifier scores
from the earlier timed-out trials have evaluation_valid=false and strict=null
and are not valid success-rate samples.

## Product approval: current local A and B evidence

The v9 local A attempt first failed at preparation and later at postflight;
its independent original-verifier diagnostic scored 21.88 with 49/51 rules.
Those records are preserved. A fresh A run completed execution/readback but
initially lacked a verifier receipt because the root process did not inherit
the launcher's Docker environment. After the launcher repair, browser
reconciliation obtained 100 points and 51/51 original rules without another ERP
write. This is a recovered pass, not an uninterrupted first-attempt pass.

A separate A run deliberately lost the successful apply response and the first
successful verification response. The next reconciliation returned the same
case-bound receipt; apply and authorization each happened once. All eight
business-table digests stayed unchanged after the original apply. This was an
explicitly authorized service-level acceptance, not browser approval or an
autonomous benchmark run.

B completed a fresh browser approval loop for a production-line outage:
prepare, one approval, same-plan execution, readback and 95/95 original rules
with a valid strict pass. Exact approval replay and reconciliation returned the
same execution receipt and left all eight business-table digests unchanged.
These are synthetic ERP-Bench product environments; no customer ERP deployment
or fleet-wide success rate is established.

The root executor may recover a missing verification receipt only after checking
the consumed approval, complete execution journal, original policy/snapshot/source
bindings and fresh ERP readback. A recorded verifier failure cannot be rerun into
a pass. Unknown pending verification stays unknown; only proven pre-launch
failures may safely retry. Public review remains read-only and historical run
approval endpoints cannot issue grants. Autonomous benchmark execution stays
separate from product approval.

## G2: 2288 is a small single-arm result

The three consecutive 2288 strict passes are useful but narrow:

- All three repeats use the same combined intervention.
- There is no matched control arm in the series.
- The earlier failures (no guidance, guidance without candidate feedback) are
  separate runs, not a randomized comparison.
- The result cannot be separated into the effect of compact reading, candidate
  feedback, and the allocation fix.
- Three samples do not give a confidence interval or a stable success rate.

The correct statement is "2288 passed 3/3 strict under this combined configuration
in a single-arm series". The incorrect statement is "2288 is now solved" or "2288
has a 100 percent success rate".

## Model-context failures

Several failures come from the model running out of context or output budget, not
from the executor:

- `p5-B-perm-pi-r2` produced two 8192-token output stops and a failed compaction
  retry. `recovery-route.jsonl` stayed empty; no prepare or apply reached the
  socket. Odoo writes were zero.
- The first 2288 attempt spent most of its 600 seconds on data reading, 12 failed
  prepare attempts, and generation. Only about 33 seconds were measurable Odoo
  backend execution. The run ended with `AgentTimeoutError`.
- The G1-only 2288 retry failed earlier, before prepare, with two consecutive
  8192 output-length stops and a failed compaction retry.

A `stopReason=length` event by itself is not proof of failure; successful runs
also contain length stops and compaction events. The failure depends on whether
the agent can still produce a valid tool call or write after the truncation.
The project has not implemented bounded dumps, deduplication, or a retry cap, so
the read-volume problem is mitigated by guidance, not solved generally.

## Generalization limits

The recorded evidence covers only a small set of ERP-Bench repair-plan tasks:

- Two development tasks: 2280 and 2299.
- One exposed regression task: 2281.
- Old held-out tasks: 2287, 2288, 2296, 2294.
- G4 held-out tasks: 2285, 2283, 2292, 2290.

G4's result is 2/4 strict. The old held-out result is 3 valid strict passes out of
4, with one timeout. Neither is a success rate for all repair-plan tasks, and
neither supports a claim about ERP-Bench's full set (the project has not run all
300 tasks). Task selection intentionally used only public metadata and fixed
hashes, not reference answers or historical model scores, but selection rules do
not make a small sample representative.

The B development task has a 2/3 result, driven by one context overflow. The A
task's 3/3 result has zero variance in this sample but only three observations.
A confidence interval cannot be computed from three samples, and a zero-variance
sample is not proof of deterministic success.

## Task-shape and planner limits

The recovery prototype supports a bounded family of purchase and manufacturing
plan documents. It does not support every ERP-Bench task shape. Known rejection
cases include:

- Partial fulfillment.
- Mixed currencies or units.
- Ambiguous supplier offers or multiple offers that cannot be disambiguated.
- Multiple bills of materials or multiple operations.
- Exact allocation requirements that the change-set prototype does not cover.
- Out-of-scope demand that the scope guard refuses.

When an input is missing, ambiguous, or unsupported, the tool is expected to
reject it rather than read hidden scenario files or invent data. A rejection is
not a successful plan. The read-only planner configurations are controls; they
return a target state, not a bound list of document edits, so they must not be
looped into `create` calls.

The agent must map abstract plan references and relative dates to real document
ids, perform the allowed modifications, and read the resulting business state.
The external verifier, not the tool, decides whether the task passed.

## Knowledge-retrieval limits

The knowledge feature is deliberately simple:

- No vector search and no synonym expansion. Chinese text is matched by
  overlapping bigrams; English text is matched by lowercase ASCII words of length
  two or more. A domain term normally has to appear in the corpus in its original
  form.
- The answer is a set of verbatim passages with file and line citations. The
  retrieval endpoint does not generate a summary. The optional assistant endpoint
  can generate text, but it must fall back to retrieval and must not invent
  unsupported facts.
- A passage is at most 40 lines and 2000 characters. A concept spread across a
  long section may be split, and a question may need terms from the exact chunk.
- The score floor is 0.5. A question with no usable tokens or no hit above the
  floor returns `found=false`. This is a precision-oriented choice; it can miss a
  relevant passage whose wording does not overlap.
- The Chinese corpus and English corpus are separate. English retrieval only sees
  `docs/en/**/*.md`; Chinese retrieval sees the existing Chinese docs and skips
  the English subtree. An English question cannot cite Chinese documents, and a
  Chinese question cannot cite the English corpus.
- The corpus is not machine-translated. The English documents are maintained
  English source documents, not a live translation of the Chinese docs.
- If `docs/` is absent from an installation, the index is empty and answers
  return `found=false` instead of failing the request.

The 25-question acceptance set is not a benchmark of answer quality. It checks
citation behavior, identity and fabrication guards, unknown-question handling,
determinism, and read-only behavior.

## Verifier and evidence limits

- The verifier runs as root in an agent-writable workspace. The hardened profile
  injects four environment variables and has passed nop controls, but a
  behavior-level shadow-module attack with a real agent has not been run.
- Reward files have no run id, timestamp, or signature generated inside the
  verifier. Attestation signs the post-run artifacts; it does not make the
  original verifier intrinsically tamper-proof.
- The 37-run, 1,557-file audit found no shadow-module residue and all reward
  timestamps after verification began. It still cannot prove the workspace was
  clean at verification time.
- Missing fields remain `null`. A missing cost, token count, or artifact is not
  zero.

## Product and operational limits

- Invite holders can consume the hosted platform model through authenticated
  endpoints; the key itself is never returned. Hosted requests have process-local
  rate limits, but no durable per-user token/spend budget. See
  [model settings](model-settings.md).
- The hosted product can execute approved plans in dedicated synthetic
  ERP-Bench sandboxes. The API and assistant cannot access the Docker socket or
  ERP write key; a separate executor enforces scope. Autonomous research runs
  are not exposed through the public product.
- Hosted case, approval and ERP records persist separately. The legacy review
  image remains read-only and its temporary storage is not the hosted service's
  persistence design. Isolated ERP restore and normal reboot evidence do not
  establish whole-site disaster recovery onto another VPS.
- The project does not implement automatic rollback, automatic retry of an
  unknown write, or a human approval step inside autonomous benchmark runs.

## Not claimed

ForgeFlow does not claim:

- A stable success rate on ERP-Bench or on all repair-plan tasks.
- That G3 contracts are proven end to end.
- That D1 product approvals are complete.
- That every 100-point run is tamper-proof without the stated hardening and
  attestation caveats.
- That the knowledge base is a general-purpose chatbot or a source of facts
  outside its indexed documents.
