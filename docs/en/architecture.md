# Architecture

ForgeFlow separates four concerns that are easy to mix in an agent demo:

1. The **assistant** investigates through scoped read-only tools; product planning uses CP-SAT and a person chooses and approves the plan.
2. The **root executor** is the only component allowed to write to Odoo.
3. The **original verifier** decides whether the business task passed.
4. The **product API** exposes evidence, preparation, selection and approval routes; a separately controlled executor owns ERP writes.

Keeping those boundaries explicit is the main architectural choice. A successful
model response is not treated as task success; only the external verifier can
produce a score.

Current boundaries and metric versions are defined in [current capabilities](current-capabilities.md).
The hosted human-approved product path is:

```text
Case facts → CP-SAT candidates → versioned plan → user choice and approval
 → controlled ERP executor → journal/readback → original verifier → receipt
```

Candidate priorities can differ while hard constraints stay fixed. New-spend
margin uses purchase/manufacturing coverage revenue, excluding finished stock.
Old metric versions remain historical and cannot authorize a new execution.
Complete execution supports one finished product and a single-level BoM;
read-only collection supports deeper acyclic material relationships.

## Autonomous research runtime flow

The research runtime layers run from the agent to the original verifier:
Harbor starts the Odoo task and Pi agent; the root executor prepares and applies
recovery plans; the original verifier reads the resulting ERP state and writes
its acceptance score. The human-approved product workflow above is separate.

```text
ERP-Bench task (2280, 2299, held-out, or G4)
        |
        v
Harbor 0.24.0 trial
        |
        +--> Odoo container + task seed data
        |
        +--> Pi 0.85.1 single-agent loop
                |
                +--> read-only Odoo credential
                +--> bash / file tools for investigation
                +--> prepare_recovery  (root executor, CP-SAT)
                +--> execute_recovery  (root executor, journal)
                |
                v
        plan documents in Odoo
                |
                +--> readback through the recovery tool
                |
                v
        original ERP-Bench verifier (test.sh / checks.py)
                |
                v
        reward.json + rule results + summary.json
                |
                v
        local run viewer / product API / evidence export
```

The agent never receives `solution/`, `tests/`, or a task-specific reference
plan. The reference solution is used only for `oracle` environment checks. A
`nop` agent performs no business change and is expected to score zero; it is the
control that shows the environment and verifier chain are alive.

## Components

| Component | Responsibility | Evidence or code |
| --- | --- | --- |
| Harbor 0.24.0 | Starts the task environment, agent trial, and verifier | Run `manifest.json`, `harbor-config.json` |
| Pi 0.85.1 | Single-agent tool loop; calls the configured OpenAI-compatible model | Agent logs, `agent/pi.txt` |
| Nebius Token Factory | Hosts the default `nvidia/Nemotron-3.5-Super-VL` model | `manifest.json` model fields |
| Odoo | Business system under test; the only place real plan documents change | Task container, Odoo access logs |
| Recovery planner | Builds a CP-SAT plan over live Odoo data and maps it to a change set | `plan.json`, `change-set.json` |
| Root executor | Validates policy, collects the snapshot, prepares, applies, and stores plans | Unix socket protocol, `recovery/` artifacts |
| Original verifier | Computes the 0-100 score and per-rule results | `verifier/reward.json`, `rule_results.tsv` |
| Product API | Case preparation, candidate selection, approval orchestration, recorded runs and scoped Q&A | `src/forgeflow/api/` |
| React workbench | Plan review and explicit approval through the API; no direct ERP credential | `web/` |
| Knowledge index | Offline BM25 retrieval over project Markdown with line citations | `src/forgeflow/knowledge/` |

## Agent and executor boundary

The agent can read Odoo through a dedicated read-only user and can run local
diagnostics. It cannot read the write key. When a change is needed, it calls
`prepare_recovery` with the business request, not with a snapshot or a plan. The
root executor collects the live snapshot, solves, builds the change set, and
returns a `plan_id`. A later `execute_recovery` call with that `plan_id` performs
the controlled writes and readback.

This ordering matters. If the agent supplied the snapshot or plan, `plan_id`
would only prove that the agent hashed its own input. The executor-side prepare
binds the plan to what the executor actually saw.

The executor protocol is a Unix socket at `/run/forgeflow/executor.sock`, owned
by root and group `forgeflow` with mode 0660. It uses `SO_PEERCRED` peer checks,
one-line JSON frames, a 64 KiB limit, one request per connection, and a closed
set of error codes. The store under `/var/lib/forgeflow/recovery` is root-owned
with mode 0700.

## Recovery artifacts

A prepared plan directory can contain:

- `request.json`: the explicit business request.
- `snapshot.json`: the live Odoo state collected by the executor.
- `plan.json`: the solver output and trace.
- `change-set.json`: keep, update, cancel, and create operations mapped to real
  document identities.
- `baseline.json`, `binding.json`, `manifest.json`: the pre-execution state and
  source binding.
- `journal.jsonl`: the persistent operation order, commits, and verification
  events.
- `readback-*.json`, `success.json`: the executor's own final-state check.
- `recovery-export.json`: allowlisted export with per-file SHA-256 values.

The tool accepts only plan document actions implemented in
`recovery/journal.py`. It does not invent goods receipts, production completion,
or inventory adjustments. If an apply result is uncertain, the safe behavior is
to keep the original plan and log, then reconcile from that log; blind retries
are not allowed.

## Verification boundary

`forgeflow.verifier.ERPBenchVerifier` adapts score reading only. The original
`test.sh`, `checks.py`, and scoring files stay unchanged. Harbor 0.24.0 does not
accept ERP-Bench's nested scoring JSON by default, so the adapter extracts the
score without rewriting the verifier.

The raw score is in the range 0-100, and the original `passed` threshold is 99.
ForgeFlow's `strict_success` is stronger: the original task must pass, every
applicable rule must pass, the evaluation chain must be complete, no trial
exception may be present, and the Harbor scalar score must agree with the raw
score. Missing or malformed results become `null`, never a default success.

## Execution profiles

Profiles are explicit experimental factors, not hidden defaults:

| Profile | Purpose |
| --- | --- |
| `planning-metadata-v1` | Adds existing work-center unit time and cost notes to Odoo; numeric task inputs are unchanged |
| `cp-sat-recovery-v1` | Enables `prepare_recovery` and `execute_recovery` |
| `cp-sat-live-v1`, `cp-sat-native-v1/v2` | Read-only planner entry points kept as controls |
| `permission-split-v1` | Read-only agent credential plus root executor write path |
| `verifier-hardened-v1` | Hardens the root verifier execution context with four environment variables |
| `odoo-read-v1` | Compact Odoo read guidance for context-heavy tasks |
| `recovery-workflow-v1` | Workflow guidance for using prepare and execute in order |

Runs with different profiles are reported separately. The project does not merge
security profiles or planner configurations into one success number.

## Product layer

`forgeflow api` is a FastAPI application. Deployment capabilities determine
whether it permits reading, preparation, selection and approval. The hosted
Scenario A installation has persistent per-reviewer APIs, assistants and Odoo
sandboxes behind an HTTPS gateway. A separate executor applies approved plans;
the assistant's generated text is never write authorization. See the
[judge workflow](judge-guide.md) and [deployment guide](deployment.md).

The earlier Render deployment served recorded runs in read-only review mode.
That remains a valid review-only configuration, but it does not describe the
current hosted judge workflow. Historical deployment evidence retains its
original source and data-profile binding.

## Knowledge index

`DocumentIndex` builds a deterministic in-memory BM25 index:

- Chinese locale sources: `docs/**/*.md`, root `*.md`, and
  `contracts/*.json|*.md`, excluding internal next-stage plan files.
- English locale sources: only `docs/en/**/*.md`.
- Symlinks are skipped. Files above 512 KiB are skipped. Total input is capped at
  8 MiB and 5000 passages; exceeding a cap sets `truncated` and stops
  deterministically.
- Passages are split on Markdown headings and blank lines. A passage has at most
  40 lines and 2000 characters, with 1-based line numbers.
- Tokenization uses lowercase ASCII words of length 2 or more plus overlapping
  CJK bigrams. BM25 uses k1=1.2 and b=0.75.
- Search returns only positive-scoring passages, sorted by score, path, and line.
- `answer()` raises the score floor to 0.5 for "found" results and returns no
  passages when the question has no usable tokens or no hit clears the floor.

The index has no vector store, no synonym expansion, and no network access. A
professional term usually has to appear in the corpus in its original form.
`evidence_lines(root, "docs/en/example.md:12-20")` re-reads the exact source lines
and rejects absolute paths, `..`, symlinks, and out-of-range line numbers.

## Non-goals

ForgeFlow does not replace ERP-Bench, does not let the agent read reference
solutions, does not add human approvals to autonomous benchmark runs, does not
claim that model self-reports are evidence, and does not give the assistant or browser a direct ERP write credential. The product API may dispatch an explicitly approved plan to the controlled executor.
