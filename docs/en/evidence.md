# Evidence and Provenance

Every ForgeFlow result is supposed to answer three questions:

1. What exactly was run?
2. What did the original verifier produce?
3. Can the recorded artifacts be checked later without rerunning the task?

The project prefers an awkward recorded failure over a clean unsupported claim.
Missing values stay `null`; they are not filled from the current repository or
from memory.

## Independent reviewer acceptance

The [new reviewer record](../evidence/2026-10-26-reviewers/README.md) separates
two disposable environments used for real concurrent preparation, model calls,
approval and original verification from the five official judge environments.
It records duplicate approval, cross-instance isolation and service restarts.
Preparation-only and repeated-approval measurements compare all row contents
in twelve listed business tables, not the entire Odoo database. Public HTTPS
routing and host-reboot checks have their own evidence in that record.

The [local bootstrap record](../evidence/2026-10-25-local-bootstrap/README.md)
documents a separate fresh image/database, case planning and stop/resume. It
preserves failed attempts and actual executed source hashes; it does not claim
local model calls, approved writes or Scenario B runtime coverage.

## Initial hosted VPS acceptance, October 25, 2026

The [published VPS record](../evidence/2026-10-25-vps/README.md) is bound to source
commit `67bcb95e3253c245dab49478458aab652af8d05a`, an immutable Odoo image, and the
new live case. It includes redacted preparation/approval/write/readback captures,
the original 51-rule reward bytes, isolated database/Odoo restoration, rotation,
and complete OS reboot records with an integrity manifest.

This is real sandbox execution on synthetic Scenario A benchmark data using the
`planning-metadata-v1` visibility profile. The files distinguish isolated Odoo
recovery from full ForgeFlow public API/approval disaster recovery, which was not
tested. A new case approval after reboot and an independent zero-write database
measurement before approval are also not claimed. These are inspectable operator
captures, not an independent attestation; the older UI fixture is separate.

## Run directory layout

A run lives under `runs/<run_id>/`. The exact Harbor trial directory is generated
by Harbor; `summary.json` records the raw paths.

| Path | Contents | What it establishes |
| --- | --- | --- |
| `manifest.json` | Model, budget, command, profiles, data/planner/security configuration, redacted settings | Run conditions |
| `implementation/` | Compressed snapshot of the implementation before the run, per-file SHA-256, capture time | What code was present |
| `task/` | Execution copy and source digest; derived compatibility, network, and optional data-visibility diffs | What the task environment received |
| `harbor-config.json` | Harbor's resolved run configuration | Effective harness settings |
| `harbor-output.log` | Harbor output | Harness-level events |
| `harbor/job/.../verifier/` | Original reward, rule results, checks, and logs | Business score |
| `summary.json` | Original conclusion plus ForgeFlow's strict judgment | Final recorded result |
| `attestation/` | Signed verifier artifacts and signature status | Post-run integrity |
| `supplemental-evidence/` | Independent execution analysis and source hashes | Secondary review |

`implementation/` is a before-run snapshot, not a frozen execution environment.
A run still loads from the workspace and records workspace changes at the end.
The project does not claim the execution was frozen to the snapshot unless it
was.

## Recovery artifacts

When `cp-sat-recovery-v1` is used, a prepared plan directory under
`agent/recovery/<plan_id>/` can contain:

| File | Role |
| --- | --- |
| `request.json` | The explicit business request submitted by the agent |
| `snapshot.json` | Live Odoo state collected by the executor |
| `plan.json` | Solver output, trace, and solve calls |
| `change-set.json` | Keep, update, cancel, and create operations mapped to real documents |
| `baseline.json` | Pre-execution business state |
| `binding.json` | Binding between the change set and the baseline |
| `manifest.json` | Plan-level source and policy metadata |
| `journal.jsonl` | Persistent operation order, commits, and verification events |
| `readback-*.json` | Executor readback after writes |
| `success.json` | Executor's own success record for the plan |
| `recovery-export.json` | Allowlisted export with sizes and per-file SHA-256 |

`execute_recovery` writes and reads back plan documents. A tool readback is not
the same as an original verifier pass. The external verifier still decides task
success. If an apply result is unknown, the correct artifact is the retained
journal and original plan id; a blind retry is not evidence.

## Verification chain

The original ERP-Bench verifier runs `test.sh` and `checks.py` and produces
`reward.json`, rule results, and logs. Harbor 0.24.0 does not accept the nested
scoring JSON directly, so `forgeflow.verifier.ERPBenchVerifier` adapts score
reading only. It does not rewrite the verifier files.

`summary.json` preserves the original conclusion and adds:

- `strict_success`: original pass, all applicable rules pass, complete chain,
  no trial exception, and score agreement.
- `evaluation_valid`: the verifier artifacts are complete and readable.
- `failed_rule_count` and rule-level detail where available.
- Attestation status and evidence paths.
- Missing fields as `null` when the artifact does not contain a measurement.

An nop control proves that the chain can run and that a no-op scores zero. It does
not prove that a Pi run succeeds. An oracle control validates the reference path;
it is not an agent result.

## Attestation

`forgeflow run --attest-key` signs verifier artifacts after the run with a local
Ed25519 key. `forgeflow attest --run-dir ... --verify` checks the signature and
reports its status. A signed run has a stronger integrity record than an unsigned
file.

Attestation has limits:

- It signs the post-run artifact, not the live verifier process.
- It does not make the original verifier intrinsically tamper-proof.
- It does not prove the business answer is correct beyond what the verifier
  scored.
- A missing or invalid signature must be reported, not treated as a pass.

The signature experiment `attestation-nop-20261021-01` showed that signing does
not change the score. The 100-point `permission-split-pi-20261021-09` run was
signed and verified.

## Provenance fields

`scripts/experiments.py manifest` builds a versioned configuration fingerprint
from recorded run data. It reads only the supplied run directories and does not
start containers, call models, or write into `runs/`.

Fingerprint fields include:

- Source commit when the manifest records it, or `null`; implementation snapshot
  digest.
- Upstream lock digest and commits.
- Environment image ref and digest when recorded.
- Agent (`pi`, `nop`, or `oracle`).
- Task id and input digest.
- Model id, provider, thinking mode, and sampling parameters.
- Agent and setup budgets, output limit and policy, context window, attempts,
  concurrency, and retries.
- Data, planner, security, and guidance profiles with explicit content digests
  where available.
- Contract id, digest, source, and enabled flag, or `null` when not bound.
- Verifier adapter, adapter version, and content digest.
- Approval mode (`evaluation` or `product_approval`) and its source.
- Business anchor date and start snapshot digest when derivable.

`mixed_config` compares normalized fingerprint leaves across runs. Fields such as
`run_id`, timestamps, scores, and token counts are result fields, not
configuration, and do not trigger a mixed warning. Because task id, anchor date,
and start snapshot are part of the fingerprint, cross-task or cross-instance
comparisons are usually mixed by design; the `mixed_fields` list explains why.

The product reading is separate:

- `attempted` is the denominator and includes invalid and timed-out runs.
- `evaluation_valid` and `strict_success` come directly from `summary.json`.
- `completed_within_budget` is conservative: false for timeout or invalid runs,
  false when elapsed time exceeds the recorded budget, true only when completed,
  valid, and timed, and `null` when the inputs are unknown.
- `failure_stage` is one of runner, agent, trial, verifier, budget, or business.
- Costs and tokens remain `null` when the provider or summary did not report
  them.
- Both `manifest` and `report` JSON exports retain `runs[].product.usage_accounting`
  and `cost_completeness` alongside the recorded numbers. Token accounting can
  be complete while cost is `not_reported`; partial cost is `incomplete`, and
  historical or unrecognized accounting is `unknown`. Use these fields before
  comparing model costs. The additive fields do not change configuration
  fingerprints, business-result totals, or historical source files.

## Historical verifier audit

The P1 audit examined 37 runs and 1,557 files:

- 33 runs had complete verifier artifacts; 4 were abnormal.
- All rewards were written 40 to 200 seconds after verification began. None was
  written before verification.
- All 33 run scores were reproducible from `rule_results.tsv`, `reward.txt`,
  `spend.json`, `optimality.json`, and the reward file.
- Every verifier HTTP request had a matching Odoo access-log entry; there were
  zero orphan requests.
- No `pathlib.py`, `json.py`, `sitecustomize.py`, `uv.toml`, or `.pth` residue was
  found in the 1,557 files.
- The two 100-point runs had no native command writes.

The audit also found the limit: it could not prove `/workspace` was clean during
verification, because the root verifier executes `python3 -` in an agent-owned
directory and reward files lack a run id, timestamp, or signature. The 100-point
records are self-consistent, but the anti-tamper claim depends on execution
hardening and attestation, not on reading files afterward.

## Key run ids

| Result | Run id |
| --- | --- |
| Development A, workflow plus guidance | `workflow-a-off-20261020-01` |
| Development B, workflow plus guidance | `workflow-b-off-20261020-01` |
| P5 A repeats | `permission-split-pi-20261021-09`, `p5-A-perm-pi-r2`, `p5-A-perm-pi-r3` |
| P5 B repeats | `permission-split-pi-b-20261021-01`, `p5-B-perm-pi-r2`, `p5-B-perm-pi-r3` |
| Old held-out passes | `p5-2287-pi-r1`, `p5-2296-pi-r1`, `p5-2294-pi-r1` |
| Old held-out timeout | `p5-2288-pi-r1` |
| G1 2281 regression | `g1-2281-pi-r1` |
| G2 2288 recovery | `g2-2288-read-v1-r2`, `g2-2288-read-v1-r3`, `g2-2288-read-v1-r4` |
| G4 results | `g4-2285-pi-r1`, `g4-2283-pi-r1`, `g4-2292-pi-r1`, `g4-2290-pi-r1` |

The names are recorded identifiers, not a promise that the directories are present
in a source-only checkout. `runs/`, `.runtime/`, and `vendor/` are git-ignored;
the lock file can rebuild upstream source, but recorded run evidence must be
preserved separately.

## Citation resolution

Knowledge answers cite `path:start-end`. `evidence_lines(root, citation)`:

- Parses the citation strictly.
- Rejects absolute paths, `..`, `.` components, symlinks, and paths outside the
  knowledge root.
- Rejects line numbers below 1, reversed ranges, and ranges beyond the file.
- Re-reads the source and returns the exact lines.

This is how a retrieval result remains checkable: the citation is not a label on a
generated summary; it resolves to the same verbatim text the index stored. The
acceptance tests assert exact text equality between the passage and the resolved
lines, which catches citation drift.

## What the evidence does not include

- Model reasoning text, full system prompts, or raw tool responses. The public UI
  shows structured fields and cited document text, not hidden chain-of-thought.
- Secrets, API keys, Odoo credentials, or invite codes. Those stay in ignored local
  configuration or platform environment variables.
- A guarantee that every historical run directory is present in a fresh clone.
- A guarantee that an unsigned reward is authentic. Unsigned evidence is useful
  but should be labeled as such.
