---
name: evidence-chase
description: Inspect ForgeFlow case and run evidence, separate planned from applied from verified, say not verified when proof is missing, and never expose secrets, credentials, or private payloads.
whenToUse: Use when the user asks how a result was verified, what evidence exists for an approval or ERP write, why a status is trustworthy, or which artifact would prove a claim.
---

# Evidence chase

## Start from the claim, then find the artifact

Restate the claim in one line before searching: a plan exists, an approval was
issued, the ERP was written, a readback was taken, or the original verifier
returned a result. Each claim needs a different artifact.

Use host-provided facts when they already cover the claim. Otherwise use the
read-only evidence tools for the missing part:

1. `mcp__forgeflow__list_cases` and `get_case` for the case record, plan
   reference, approval state, and linked run.
2. `mcp__forgeflow__list_runs` and `get_run` for execution status, plan binding,
   verifier output, and recorded traces.
3. `mcp__forgeflow__search_evidence` for a specific identifier, order, product,
   or error phrase.

For code-level claims, cite an authorized source only when actually supplied.
If repository evidence is unavailable to the scoped tools, say so. Never read
`.runtime/` or `runs/` directly, never call or invent `approve`/`apply`, and
never reconstruct missing evidence from memory.

## Separate planned, applied, and verified

| Claim | Minimum evidence | Not enough |
|---|---|---|
| Plan proposed | A plan ID or hash recorded in the case or run, with its scope | Chat text, a UI preview, a progress message |
| Approved | A single-use approval record bound to run, plan, policy, snapshot, and expiry | "The user said yes", a DSH approval, a button click |
| Applied | An executor result or journal/change-set entry tied to that approval | The plan, the approval, or a progress event |
| Read back | Post-apply state read from the system of record | The executor's own narration |
| Verified | The task's original verifier output tied to the same run | Readback alone, a reward from another run |

If the chain stops early, say exactly where it stops:

- Plan only: "planned, not applied."
- Approval only: "approved, not applied."
- Apply without readback: "applied, not read back."
- Readback without verifier: "read back, not verified."
- Missing evidence: "not verified in this repository", then name the artifact
  that would verify it, such as the run's verifier output, a readback snapshot,
  or an approval receipt.

Before saying a field was not recorded, distinguish an omitted summary from a
completed read of the relevant evidence. Tool failure or pagination does not
prove absence. Keep the answer and its field references on one snapshot and
version. Count document changes as document entries, not database transactions.
Original task-rule verification and ERP readback do not prove physical delivery
or customer sign-off. Do not reinterpret task checks as legal certification.

When sources disagree, name the conflict and the authority order: original
verifier, then readback from the system of record, then executor result, then
case progress, then chat narration. `presentationMeta`, screenshots, and
progress text are projections, not evidence.

## Protect secrets and private data

Never expose API keys, access tokens, invitation codes, `.env` values, private
customer data, owner `uid`, `trial_uri`, `workspace_root`, `trials_dir`, absolute
home paths, or hidden reasoning.

- Summarize private payloads: say which field or entity changed, not the raw
  request or response body.
- Do not quote credentials, even partially, even to prove they exist.
- Redact identifiers when they are not needed; when they are, keep them short.
- Do not dump a full evidence file. Quote the smallest snippet that supports the
  claim and say what was omitted.
- If the user asks for a secret or a private payload, refuse briefly and explain
  the read-only boundary. Do not imply that a file is safe to read just because
  a path is reachable.

## Answer shape

Lead with the verification verdict in one sentence. Then give the shortest
evidence chain that supports it, using record names rather than raw dumps.
Answer each requested part. Ask another question only when the user must supply
essential missing information, not as a mandatory ending.

Do not soften an absent result with "should be", "likely applied", or "probably
verified". Missing evidence is a finding, not a gap to fill with confidence.
