# Evaluation and Recorded Results

ForgeFlow reports two things separately: the original ERP-Bench score and the
integrity of the evaluation chain. A high score with an invalid chain is not a
pass. A valid chain with a low score is still a business failure. The project
never merges those cases into one "success rate".

## Scoring definitions

- **Raw score:** the original ERP-Bench score, 0 to 100. The original `passed`
  threshold is 99.
- **`evaluation_valid`:** the original verifier artifacts are complete and
  readable. This is an admission condition for analysis, not a task pass.
- **`environment_chain_ok`:** the environment, initialization, and verifier chain
  worked. A false value is a hard failure signal; the score should not be read as
  agent capability.
- **`strict_success`:** the original task passed, every applicable rule passed,
  the chain is complete, no trial exception occurred, and the Harbor scalar score
  agrees with the raw score.
- **`nop` control:** an agent that makes no business change. It should score 0
  with a valid environment. It proves the chain is alive; it is not a solution.
- **`oracle` control:** the reference path used only to validate the environment.
  It does not measure the Pi agent.
- **Timeout:** the agent exhausted its time budget. A timed-out run with no valid
  evaluation is not counted as a business failure and is not counted as a pass.

The scoring adapter reads the original score but does not rewrite `test.sh`,
`checks.py`, `reward.json`, or the rule results. Missing results stay `null`.

## Experimental setup

The repeated and held-out batches use this frozen combination unless a row says
otherwise:

- Model: `nvidia/Nemotron-3.5-Super-VL` on Nebius Token Factory.
- Agent: Pi 0.85.1 single-agent loop.
- Data: `planning-metadata-v1`.
- Planner and write path: `cp-sat-recovery-v1`.
- Security: `permission-split-v1`.
- Thinking: provider default; output limit left to the server.
- Agent budget: 600 seconds for the frozen and held-out batches.
- Verifier: original ERP-Bench verifier, unmodified.

Development A is `2280_medium_repair_plan_medium` (supplier cancellation).
Development B is `2299_hard_repair_plan_hard` (work-center outage).

Every group uses the same model and configuration inside the group. A different
planner, data profile, security profile, thinking mode, or output policy is a
separate experimental factor and is reported separately.

## Results

### P5 repeated development runs

| Task | Runs | Raw score | Rules | Interpretation |
| --- | --- | --- | --- | --- |
| A | 3 | 100.0, 100.0, 100.0 | 51/51 each | 3/3 strict; no observed variance in this sample |
| B | 3 | 100.0, 0.0, 100.0 | 95/95 on the two passes | 2/3 strict; group mean 66.67, min 0, max 100 |
| A nop | 3 | 0.0 each | not applicable | Environment valid |
| B nop | 3 | 0.0 each | not applicable | Environment valid |

The B failure `p5-B-perm-pi-r2` is a model-context failure, not an executor
failure. `recovery-route.jsonl` is empty, so no prepare or apply reached the
socket. The trajectory ends with `stopReason=length` and a failed compaction
retry after two 8192-token output stops. The agent ran out of context before it
could write a valid request. This is why the result is described as "current
model completion on B", not "permission split causes failures".

The three-run sample supports the statement "A passed three times under this
frozen configuration". A confidence interval can be calculated under suitable
assumptions, but it is wide; the sample does not establish a stable success rate
or support extrapolation to other repair-plan tasks.

### Old held-out batch

Four tasks were frozen before their first run:

| Task | Type | nop | Pi result | Reading |
| --- | --- | --- | --- | --- |
| 2287_medium_repair_plan_medium | Supplier cancellation | 0.0, valid | 100.0, 51/51, strict | Valid pass, signed |
| 2288_medium_repair_plan_medium | Supplier cancellation | 0.0, valid | 0.0 but invalid, `AgentTimeoutError` | 600-second timeout; not a business failure |
| 2296_hard_repair_plan_hard | Work-center outage | 0.0, valid | 100.0, strict | Valid pass, signed |
| 2294_hard_repair_plan_hard | Work-center outage | 0.0, valid | 100.0, strict | Valid pass, signed |

The correct summary is **3 of 4 valid strict passes, with 1 timeout**. It is not
"4/4 passed". The timeout run had no usable business evaluation. A larger budget
would be a new experiment and must be labeled as such.

### G1: targeted 2281 repair

Task 2281 was exposed before this fix and is therefore a regression task, not a
new held-out task.

| Stage | Score | Rules | prepare / writes |
| --- | --- | --- | --- |
| Before the fix (`p5-heldout-2281-pi-r1`) | 0.0 | 50/53 | Six prepare attempts failed; zero writes |
| After the fix (`g1-2281-pi-r1`) | 100.0 | 0 failed rules | Strict pass, signed |

The pre-fix failure had two named classes: a change-set guard rejected a legal
one-unit purchase surplus, and a live-input error came from passing a supplier
display name where a live vendor reference was required. Both failed before any
ERP write. The fix allowed a minimum-order-quantity surplus without permitting
arbitrary overbuying and resolved broken offers by vendor reference inside the
closure. A and B also passed at 100.0 strict in the same regression batch.

This is a one-task targeted fix. It does not show that the prototype now handles
arbitrary supplier-cancellation tasks.

### G2: 2288 same-budget recovery

After the G1 fix alone, 2288 still scored 0 and did not reach prepare; it failed
on two 8192-token output stops and compaction retry. Two further interventions
were then tested:

1. `odoo-read-v1` compact read guidance.
2. Returning bounded candidate offer keys to the model as error feedback.

| Run | Read guidance | Candidate feedback | Result | prepare | Writes |
| --- | --- | --- | --- | --- | --- |
| `g2-2288-pi-r1` | none | none | 0.0, context exhausted before prepare | 0 | 0 |
| `g2-2288-read-v1-r1` | yes | none | 0.0; entered prepare, 5 failures | 5 | 0 |
| `g2-2288-read-v1-r2` | yes | yes | 100.0 strict, 0 failed rules | succeeded | Executor |
| `g2-2288-read-v1-r3` | yes | yes | 100.0 strict, 0 failed rules | succeeded | Executor |
| `g2-2288-read-v1-r4` | yes | yes | 100.0 strict, 0 failed rules | succeeded | Executor |

The result is a **single-arm 3/3 strict series**: all three repeats used the same
combined intervention, with no matched control arm. The improvement combines the
allocation fix, compact reading, and bounded feedback; it is not evidence that any
one component alone caused the gain. Intermediate failures are retained. This is
a small sample and is not a stable success rate.

### G4: pre-registered held-out set

G4 froze four previously unexposed tasks before running them. The batch was run
with the frozen code and configuration, no code changes or tuning inside the
batch, and one Pi attempt per task. All four nop controls scored 0 with a valid
environment.

| Task | Type | nop | Pi score | Valid | Strict | Failure |
| --- | --- | --- | --- | --- | --- | --- |
| 2285_medium_repair_plan_medium | Supplier cancellation | 0 | 0.0 | yes | no | 3 rules; five prepare rejections for unselected active demand; zero writes |
| 2283_medium_repair_plan_medium | Supplier cancellation | 0 | 98.5 | yes | no | 1 hygiene rule: missing origin traceability on a component purchase order |
| 2292_hard_repair_plan_hard | Work-center outage | 0 | 100.0 | yes | yes | none |
| 2290_hard_repair_plan_hard | Work-center outage | 0 | 100.0 | yes | yes | none |

The research reading is **4 attempts, 4 valid, 2 strict passes, 2 non-passes**.
The product reading is **2 of 4 attempts completed within budget and passed
strictly; 2 completed but did not repair; no timeouts and no invalid runs**. It
must not be written as "4/4 passed", and the two passes must not be extrapolated
to all repair-plan tasks.

## Interpretation rules

- Report the denominator. A timeout, invalid run, or failure remains in the
  attempted count for the product reading; it is excluded only from the valid
  business sample, and that exclusion must be stated.
- Do not combine different security, data, planner, thinking, or output policies
  into one average.
- Do not call a single run stable. State the assumptions and uncertainty of any
  confidence interval; three repeats do not establish a stable success rate.
- Do not use development or exposed tasks as new held-out evidence. G0's exposure
  register marks 2280, 2281, 2287, 2288, 2294, 2296, 2299, 2285, 2283, 2292, and
  2290 as exposed; they can be regression tests only.
- Do not treat a solver success, a tool readback, or a model statement as task
  success. Only the original verifier produces the score.
- Keep failed, rejected, and timed-out runs in the report with their actual
  configuration.

## Knowledge acceptance is separate

The 25-question knowledge acceptance set checks retrieval behavior: intended
document prefix, exact citation resolution, no identity leakage, no fabricated
success strings, unknown questions returning `found=false`, determinism, and
read-only behavior. It covers 10 categories and uses 25 questions. It is an
acceptance set for the knowledge feature, not a benchmark of answer quality and
not evidence about ERP task success.

## What the current evidence does not establish

- A success rate over ERP-Bench's full task set. The project has not run all 300
  tasks.
- A stable success rate for B, 2288, or repair-plan tasks in general.
- That the G3 contract path works end to end.
- That the D1 product approval loop works end to end.
- That a 100-point run is tamper-proof without the verifier hardening and
  attestation caveats described in [permissions.md](permissions.md) and
  [evidence.md](evidence.md).
