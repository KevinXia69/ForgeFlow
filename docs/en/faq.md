# FAQ

## What is ForgeFlow in one sentence?

ForgeFlow helps a production planner review an exception, approve one recovery
plan, and verify what actually changed in the ERP using the task's original
verifier.

The local product requires human approval before execution. The separate
autonomous benchmark uses a single Pi agent without human approval; it does not
replace the product's approval flow. Both use the root executor for ERP writes,
and business chat remains read-only.

## Does ForgeFlow replace ERP-Bench or Odoo?

No. ERP-Bench supplies the tasks, Odoo environment, and original verifier. Odoo is
the system under test. ForgeFlow adds the agent, recovery planner, executor,
permission boundary, product API, and evidence tooling around them.

## What does a score of 100 mean?

It means the original ERP-Bench verifier gave a raw score of 100, which is above
its `passed` threshold of 99. For a ForgeFlow strict pass, every applicable rule
must also pass, the evaluation chain must be valid, no trial exception may be
present, and the Harbor scalar score must agree with the raw score. A model saying
it succeeded is not enough.

## Does G4's 2/4 mean only half of all repair tasks work?

No. The original G4 was a pre-registered four-task held-out batch. It produced two
strict passes (2292 and 2290 at 100.0), one failure at 98.5 (2283 with one origin-traceability
rule), and one zero (2285 with five prepare rejections and zero writes). The
correct reading is 4 attempts, 4 valid, 2 strict. It says nothing reliable about
the full task population and must not be extrapolated to every repair-plan task.

Later development regressions are separate. The new 2283 r2 passed all 45 original
rules at 100 points, with valid and strict results and 193.225 seconds of recorded
agent time. Its earlier failed runs remain in the record. The 2285 workflow-guided
r2 timed out after nine rejected native prepare calls, with no prepared plan or
execute. Its raw 0 / 41 of 44 result has valid=false and strict=null, and is not a
valid success-rate sample. Missing-demand feedback was not triggered. These exposed
tasks do not become a new held-out set or change the original G4 denominator.

## Why is 2288's 3/3 not a 100 percent success rate?

The three strict passes are a single-arm series with the same combined
intervention. There is no matched control arm, and three runs on one task do not
establish a precise or generalizable success rate. The result supports "2288 passed three times under this
combined configuration"; it does not support "2288 is solved" or a stable success
rate. The earlier failures are retained.

## Was 2288 a failure in the old held-out batch?

It was a timeout, not a business failure. `p5-2288-pi-r1` hit a 600-second
`AgentTimeoutError` and produced invalid evaluation output. The old held-out
batch therefore has three valid strict passes (2287, 2296, 2294) out of four
planned attempts; the fourth is a timeout. Do not write "4/4 passed".

## What does the current G3 evidence establish?

The earlier contract-mode series ended without a prepared plan. That historical
gap is now distinct from the v9 2280 trial: it produced a prepared plan with a
real, matching `policy_sha256` and recorded snapshot under the reviewed contract,
without human approval. Real policy binding is demonstrated.

The complete task still failed: 17.5 points, 47/50 original rules,
`evaluation_valid=true` and `strict=false`. Its partial execution journal does not
establish the final ERP write count. This closes the binding-evidence gap, not the
business task or every contract scenario. See [limits.md](limits.md).

## Has the local product approval loop been demonstrated?

Yes, in dedicated ERP-Bench product sandboxes. Local A r2 executed and read back
the approved plan, then recovered a missing verifier receipt through browser
reconciliation without another ERP write: 100 points and 51/51 original rules.
This was a recovered pass; the earlier preparation and postflight failures remain.

Local B completed browser preparation, one approval, same-plan execution,
readback and a valid strict pass on 95/95 original rules. A separate A response-loss
acceptance kept one authorization and one apply; that was a service-level test,
not browser approval. These results do not establish a customer deployment or a
general success rate. Autonomous research runs remain separate from this approval
flow. The current hosted product has separate VPS acceptance evidence; the
historical Render review service was read-only. See [limits.md](limits.md).

## Can reviewers start runs or change ERP data on the public instance?

The hosted product lets a judge create a case, inspect a plan, and approve a
bound plan for real execution in their synthetic ERP-Bench sandbox. Follow the
[judge guide](judge-guide.md). The API and assistant cannot access the Docker
socket or ERP write key; the separate executor enforces approval and scope.
Autonomous research runs remain disabled. Historical run approval and revoke
endpoints still return `409 historical_evidence_read_only`; recorded evidence
does not become a new authorization. The former Render review service is
suspended and is not the current demo.

## Is the knowledge answer generated by a model?

The knowledge endpoint retrieves verbatim passages from project Markdown and
returns file-and-line citations. It does not generate a summary. The hosted
assistant uses Nebius/NVIDIA Nemotron through DSH with scoped evidence tools.
The direct assistant fallback can generate an answer from retrieved passages;
if that model call fails or no key is available, it returns retrieval evidence
and a warning. These answer modes do not establish ERP execution or verification.

## Why do English questions only cite English documents?

The knowledge layer has explicit locales. The `en` locale indexes only
`docs/en/**/*.md`, so it cannot cite documents or contracts outside that subtree,
including the root README. The `zh` locale keeps the other configured corpus and
excludes the English subtree. This prevents cross-locale citations. The default
locale is `zh` for backward compatibility; callers opt in to `en`.

## What happens if a question is outside the corpus?

The function returns `found=false` with an empty passage and citation list. It
does not call a model and does not invent an answer. A question with no usable
tokens does the same. The HTTP knowledge endpoint returns 200 with the empty
result, not an error.

## How do I verify a citation?

Citations use `path:start-end`, for example
`docs/en/evaluation.md:10-25`. `evidence_lines(root, citation)` re-reads the
source file and returns exactly those lines. It rejects absolute paths, `..`,
symlinks, out-of-range line numbers, and malformed citations. The returned text
must match the passage text; this catches citation drift.

## Where is the evidence for a recorded result?

Start with the run directory's `summary.json` and `manifest.json`, then inspect
`harbor/job/.../verifier/` for the original reward and rule results. Recovery
runs also contain `agent/recovery/<plan_id>/` with request, snapshot, plan,
change-set, baseline, binding, journal, readback, and success artifacts. Signed
runs have an attestation. See [evidence.md](evidence.md) for the full layout.

## Does a signed run prove the business result is correct?

No. Attestation signs the verifier artifacts with a local Ed25519 key and detects
later changes to those files. It does not prove that the original verifier ran in
a clean workspace, and it does not replace the original scoring logic. The
verifier-hardened profile reduces the shadow-module risk but has not been tested
against a real agent attack.

## What can the project legitimately claim today?

Historical research includes 3/3 strict for development A, 2/3 for development B,
three valid old held-out passes plus one timeout, a targeted 2281 regression fix,
single-arm 2288 3/3 under a combined intervention, and original G4 2/4 strict.
Those denominators remain unchanged.

Current evidence adds real G3 policy binding with a failed full task, a separate
2283 r2 valid strict pass, recovered local A and browser-approved B product loops,
and scoped read-only question answering. The 2285 workflow-guided r2 ended in an
invalid timeout; its missing-demand feedback remains unverified with a real model.
The current public product has separately documented
[VPS acceptance evidence](../evidence/2026-10-25-vps/README.md); those checks do
not change historical research denominators. The project cannot claim a general ERP-Bench success rate,
all contracts solved, or production readiness. See [limits.md](limits.md) for the
configuration and acceptance boundaries.

## Why keep failed runs in the repository?

Because deleting a failure would change the denominator and hide the conditions
that produced the successful runs. The project's rule is to preserve failed,
rejected, timed-out, and invalid runs, report the denominator, and describe each
result under its actual configuration.
