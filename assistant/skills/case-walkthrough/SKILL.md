---
name: case-walkthrough
description: Answer a ForgeFlow case question in plain language, outcome first, with at most two decision numbers, one natural next question, and no claim that approval or apply happened without case evidence.
whenToUse: Use when the user asks what happened in a case or run, what is blocking it, whether recovery was approved, applied, or verified, or how to explain a case to a nontechnical reader.
---

# Case walkthrough

## Lead with the operational outcome

Open with one sentence a plant manager could repeat to someone else: the product,
the affected orders, what disrupted delivery, where recovery stands, and whether
it is verified. Then use two or three short paragraphs, one idea each.

Example opener:

> Two customer orders are exposed because a repair resource is unavailable; a
> recovery plan is ready, but no ERP write has happened yet.

Do not open with a case ID, run ID, state-machine value, or a wall of key-value
rows. If the first screen cannot answer "what happened and what is next?", the
answer is not ready.

## Shape the answer

- Keep **at most two numbers** in the main answer. Choose the two that change the
  decision, such as units affected and days at risk. Leave totals, counts, and
  hashes for a follow-up.
- Translate internal state into normal words: `awaiting_approval` becomes
  "waiting for approval", `applied` becomes "written to the ERP", and `verified`
  becomes "verified by the task's original checker". Keep the raw value in
  parentheses only when it adds information.
- End with **one natural next question**, not a menu of options. For example:
  "Want the order-level impact, or the evidence behind the verifier result?"
- Chat is read-only. When approval is the next gate, say it waits on the Cases
  page. Never ask "do you approve?" in chat and never offer to approve or apply.

## Evidence discipline

Read the case and recorded run with `mcp__forgeflow__list_cases`, `get_case`,
`list_runs`, `get_run`, and `search_evidence`. Do not infer workflow state from a
plan preview, progress text, chat history, or the executor's narration.

Never claim that approval, apply, readback, or verification happened unless the
case or run record says so. In particular:

- A plan existing is not approval.
- Approved is not applied.
- Applied is not verified; readback and the original verifier are separate
  evidence.
- If the record does not show it, say "not verified in this repository" and name
  the artifact that would prove it.

## Identifiers

Keep `case_id`, `run_id`, `plan_id`, nonces, and hashes out of the prose. In
running text, use a short label such as `case 384585ef` only when a reader needs
to distinguish two cases.

IDs belong only in a **final reference block**, and only when the reader asked
for provenance or an ID is genuinely required:

```text
参考/IDs
case_id: <value>
run_id: <value>
plan_id: <value>
```

Omit identifiers the reader did not ask for. Never repeat a long hexadecimal
string in the middle of an answer.

## Do not

- Do not paste raw JSON, tool traces, policy internals, private payloads, or
  secrets.
- Do not name the model, provider, endpoint, or API key in the answer.
- Do not use stock scaffolding such as "first, second, in conclusion" or "it is
  worth noting".
- Do not claim the public review instance executes anything; it is a read-only
  fixture.
