---
name: demo-rehearsal
description: Rehearse and narrate the 30-second ForgeFlow demo on four beats, detect to plan to approve to readback and verifier, without overclaiming or showing model names and API keys.
whenToUse: Use when preparing, timing, or narrating the ForgeFlow product demo, writing the spoken script, or deciding what may and may not be claimed on screen.
---

# Demo rehearsal

## The 30-second spine

`detect → plan → approve → readback/verifier`

One case, one disruption, one approval, one verifier result. Do not tour
settings, sessions, architecture, or a second scenario. The audience should
leave able to repeat: the ERP changes only after one human approval, and the
original verifier decides whether it worked.

| Time | Beat | Say | Show |
|---|---|---|---|
| 0–5s | Detect | "A repair resource is down and two customer orders are exposed. ForgeFlow turns that into one bounded case." | Cases queue, then the case's product, orders, and disruption |
| 5–12s | Plan | "It builds a feasible recovery plan and stops. The ERP is still untouched." | Plan scope and affected orders, labelled "planned, not applied" |
| 12–19s | Approve | "The operator approves once, for this exact plan and snapshot. The approval is single-use." | The Cases approval action and its audit record |
| 19–30s | Readback and verifier | "After apply, it reads the order back from the ERP and runs the task's original verifier." | Post-apply order state, then the verifier output |

Read the verifier verdict off the screen. Do not narrate the result ahead of the
artifact; a pause is better than a claim that outruns the evidence.

## What not to claim

- Never say the ERP was written before the approval is visible in the case.
- Never say approval is automatic, implied, optional, or something chat can do.
  Chat is read-only; approval lives on the Cases page.
- Never say "verified" without the task's original verifier output. Readback
  alone is not verification.
- Never describe the public Render instance as live execution. It is a
  read-only fixture; label it as review when it is on screen.
- Never claim a production factory deployment, customer use, endorsement, or a
  success rate from a single run.
- Never say the model decided or executed the plan. The contract is a
  deterministic plan, one human approval, a root executor, and an independent
  verifier.
- If a beat fails, say what failed and switch to recorded evidence. Label the
  recording as recorded; do not replay it as live.

## Screen hygiene

Close terminals, `.env` files, DSH logs, shell history, the network panel, and
anything with an API key, token, invitation code, endpoint, or provider model ID
before the browser is shown. Clear notification banners.

- No model names or model IDs in the narration or on screen. If asked, say the
  model is an implementation detail and return to the approval and verifier
  contract.
- No API keys, bearer tokens, `token=` URLs, or invitation codes, even briefly
  and even in a terminal behind the browser.
- No private customer data, owner UIDs, workspace paths, or raw payload dumps.
- Keep one browser window and one case tab; hide bookmarks and unrelated tabs.

## Timing and recovery

Rehearse with a timer three times. If a beat runs long, cut adjectives, not
evidence. Keep the case loaded before the clock starts.

- Approval takes longer than expected: name the gate and wait. Never click
  approve twice or bypass it to save time.
- Verifier still running at 30 seconds: say "readback done, verifier still
  running; not verified yet." Stop there.
- Live environment fails: switch to the recorded run, say it is recorded, and
  continue from the same beat. Do not improvise a success.
- A question arrives mid-demo: answer in one sentence and return to the current
  beat. Do not open a second narrative.

Close on the loop, not on a feature list: "One case, one approval, a readback
from the system of record, and the original verifier."
