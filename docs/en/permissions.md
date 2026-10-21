# Permissions and Policy Boundaries

The goal of the permission split is simple: the evaluated agent must not hold an
Odoo write credential. The agent may investigate with a read-only credential, but
every business write must go through a root-owned executor that accepts a plan
`plan_id`, not an arbitrary SQL or ORM operation.

This is a behavior change, not merely a tighter key. ERP-Bench task instructions
expect the agent to write through Odoo. For the permission-split profile, the
instruction is derived so that it points at the read-only key and states that
writes are only possible through the controlled tool. The derived instruction has
its own diff and SHA-256, and results under this profile are reported separately
from the historical default profile.

## Credentials and files

| Object | Owner and mode | Who can use it |
| --- | --- | --- |
| `/etc/odoo/api_key` | root, 0600 | Root executor only; read once at startup |
| `/etc/odoo/api_key.read` | root:forgeflow, 0640 | Agent's read-only Odoo investigations |
| `/run/forgeflow/executor.sock` | root:forgeflow, 0660; directory 0711 | Agent processes in group `forgeflow` |
| `/var/lib/forgeflow/recovery` | root, 0700 | Root executor only |
| Approvals store | service uid 10001 in the product container | Product API only |

The read-only user was tested against the real Odoo container. Nineteen models
were readable. `create`, `write`, and `action_*` calls all returned 403.
`ir.config_parameter` also returned 403. The control evidence is recorded in the
P1 feasibility notes, including a self-check that reports `SELFCHECK:ok`.

The read-only key is not a cosmetic restriction. It removes the agent's ability
to perform an arbitrary ORM write even if the model decides to try one. The only
remaining path is the executor socket.

## Executor protocol

The executor is a root-owned daemon started with the task environment. Its
protocol has these properties:

- `AF_UNIX` socket, peer identity checked with `SO_PEERCRED`.
- One-line JSON request and response, 64 KiB frame limit, one request per
  connection.
- A closed error-code set; unknown fields are rejected.
- Endpoints for `prepare`, `apply`, `status`, and `health`.
- `prepare` accepts a `run_id` and the business request. It does not accept a
  caller-provided snapshot, plan, baseline, or change set.
- `apply` accepts a `plan_id` and an optional approval token.
- A prepare idempotency index keyed by run, request hash, normalized snapshot
  digest, source digest, and policy digest. A repeated prepare returns the
  original plan id and marks it reused instead of solving again.
- Health is reported through both a health file and a connection probe. The
  daemon is designed to survive agent-process cleanup and container exit.

The write key is read once at executor startup. The executor store is root-only;
the agent cannot create its own plan directory or fabricate a stored plan.

## Policy contracts

`src/forgeflow/contract.py` implements a versioned `TaskContract`. Contracts are
manually reviewed from public task instructions and live business references;
they are not derived from `solution/`, `tests/`, or a reference plan.

Two checks exist:

- `check_request` runs before solving. It checks product, anchor date, sales
  order range, per-order budget caps, margin, disruption identity, and display
  names versus references. A budget tighter than the contract is still a
  conflict: being "more conservative" does not excuse reading the wrong field.
- `check_changes` runs before execution. It rejects changes that use a work
  center during its outage and changes with no physical basis.

A plan that used a contract records `policy_sha256`. The executor revalidates the
policy during `apply`. An approval is only allowed for a plan with
`policy_sha256`; otherwise the product API returns `409 plan_not_approvable`.

The repository contains contracts for 2280, 2281, 2287, 2288, and 2299. This is
not the same as end-to-end proof. G3 remains incomplete because five recorded
contract-mode runs ended before prepare. See [limits.md](limits.md) for the exact
gap.

## Approvals

An approval is a one-time record bound to:

- `run_id`
- `plan_id`
- `policy_sha256`
- `snapshot_sha256`
- `approver` (session uid)
- `issued_at_utc`
- `expires_at_utc`
- a random `nonce`

The TTL range is 60 to 3600 seconds. Creating an approval writes an append-only
`audit.jsonl` entry under a file lock. Revocation burns the one-time flag.
Only one unexpired, unused approval may exist for a plan at a time; a duplicate
request returns `409 approval_exists`. The executor consumes the token once, and
a failed or expired attempt does not consume it.

The approval is a product-mode input. Autonomous benchmark runs use a
pre-authorized execution mode and do not insert a human approval step. The two
modes are recorded separately in the run manifest.

## Verifier hardening and residual risk

The original verifier runs as root and executes `python3 -` from `/workspace`,
which is owned by the agent user. That creates a shadow-module risk: a file such
as `pathlib.py` or `sitecustomize.py` in the working directory could hijack the
verifier and forge a reward.

The `verifier-hardened-v1` profile injects four variables into the verifier
environment:

- `PYTHONSAFEPATH`
- `PYTHONPATH`
- `UV_NO_CONFIG`
- `BASH_ENV`

Two `nop` controls, one hardened and one baseline, completed with score 0,
`evaluation_valid=true`, and Harbor return code 0. The host `ps` output and the
container `/proc` both confirmed that the four variables reached `test.sh`,
`checks.py`, and the heredoc. A behavior-level shadow-module test with a real
agent is still not complete.

A historical audit covered 37 runs and 1,557 files. Thirty-three runs had complete
verifier artifacts and four were abnormal. All rewards were written 40 to 200
seconds after verification began; no reward appeared early. The scoring formula
was reproducible from the recorded files, and every verifier HTTP request had a
matching Odoo access-log entry. No `pathlib.py`, `json.py`, `sitecustomize.py`,
`uv.toml`, or `.pth` residue was found. The audit still could not prove that
`/workspace` was clean during verification, because the verifier runs as root in
an agent-writable directory. The conclusion is that the 100-point records are
self-consistent, but "not tampered with" requires hardening and attestation, not
after-the-fact file inspection.

`forgeflow run --attest-key` signs verifier artifacts with a local Ed25519 key,
and `forgeflow attest --verify` checks them. A signed record is stronger than an
unsigned file, but it still binds the artifact, not the underlying business
correctness.

## Product API isolation

The product service has no Odoo credentials and no model key requirement. It
reads recorded run directories from a read-only mount. Run paths are constructed
from `data_root` and validated; the API never accepts an arbitrary disk path.
Runs created through the API have an owner, and another reviewer receives 404
rather than a hint that the run exists. CLI-created recorded runs have no owner
and are visible to all signed-in reviewers; that is the intended demo semantic.

The container runs as an unprivileged user, drops all capabilities, uses a
read-only root filesystem, and does not mount `/etc/odoo` or the Docker socket.
Approvals are the only write path, and they write to a separate volume or a
temporary directory, never to the recorded run tree.

## What is still not proven

- No behavior-level shadow-module defense has been demonstrated with a real
  agent.
- No reward is cryptographically bound inside the original verifier itself; the
  project adds post-run attestation artifacts.
- The root executor is a hardened same-kernel boundary, not a separate VM.
- The product API does not implement per-user rate limits for the platform model
  key.
