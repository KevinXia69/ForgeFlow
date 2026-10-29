# Delivery state and worktree boundary

> Historical document. Deployment and capability statements below describe their recorded date and are not the current capability overview. See [current capabilities](en/current-capabilities.md) and the [judge guide](en/judge-guide.md).

> 2026-10-24. Written for v13 task **T39**; updated when the repository was made public and committed.

## One line

The worktree was deliberately dirty from 2026-10-23: the v10–v14 implementations and their evidence lived only in the working tree and were never committed. That ended on 2026-10-24, when the work was committed in five groups and the repository became public.

## Why it stayed uncommitted for so long

1. **An explicit boundary.** The handoff and the session rules both said "do not commit, push or deploy unless asked". Nobody asked until 2026-10-24.
2. **Committing would have cut evidence from code.** Several evidence directories describe "the current worktree plus one run". Committing first and editing afterwards would break that correspondence.
3. **The baseline was still moving.** v13 still had unfinished tasks, so committing would have frozen an incomplete state as a version.

**The cost was real**: no revert point, and `runs/` and `.runtime/` hold the only copies of some evidence.

## What was committed, and what was not

| Commit | Contents |
| --- | --- |
| `323bf62` | `src/`, `tests/`, `web/`, `web-public/`, `scripts/`, `contracts/`, `pyproject.toml`, `uv.lock` — 335 files |
| `67ab18f` | `docs/` including 1501 evidence files, `README.md`, `PROJECT_MEMORY.md`, `AGENTS.md`, the progress log — 1542 files |
| `5affccd` | The stage D record and the earlier v4–v7 plan drafts |
| `49d86c2` | Test dependencies in the `dev` group, so a clean clone can run the suite |
| `9f20580` | A guard on the one test that read `runs/` without skipping |

**Never committed** (still excluded by `.gitignore`, each checked):

- `.runtime/` — sandboxes, session keys (`api-session.key`), approvals, real cases
- `runs/` — real execution records and verifier output
- any `.env` or credential

Checks run before committing:

```
$ git diff --cached --name-only | grep -E "^\.runtime/|^runs/|\.env$|api-session"   # empty
$ scan sk- / nvapi- / AKIA / PRIVATE KEY across 460 staged files                      # empty
$ scan FORGEFLOW_SESSION_SECRET=<20+ chars> across all evidence                       # empty
```

`demo-invite-code-0001` appears in the README and in tests. It is the fixed local development invite code (`scripts/local_workbench.py:71`); the session key is generated separately, so it is not a credential.

## Repository visibility

`KevinXia69/ForgeFlow` became public on 2026-10-24. The older `ForgeFlow-Public` export was renamed `ForgeFlow-archive` and then deleted by the owner; there is one repository now.

## If more commits are needed

Split by "can be verified on its own", not by date:

1. **Product and UI**: `web/src`, `web/src/styles.css`, `web-public/src`, their e2e suites.
2. **Server and contracts**: `src/forgeflow/**`, `contracts/**`, `tests/**`.
3. **Evidence and docs**: `docs/**`, `PROJECT_MEMORY.md`, the progress log, `README.md`.
4. **Never**: `.runtime/**`, `runs/**`, `.env`, any key or invite code.

## Known cleanup leftovers

### Port 8021: an ownerless listener (v14 E10)

```bash
netstat -an | grep '\.8021 '
# tcp4  0 0 127.0.0.1.8021  *.*  LISTEN
# tcp6  0 0 ::1.8021        *.*  LISTEN

lsof -nP -iTCP:8021                # empty: no owning process visible
curl -m 2 http://127.0.0.1:8021/   # http:000: it listens but answers no HTTP
```

It is not an API this repository started (those answer `/` and have an owner in `lsof`). Cleanup steps, in order:

1. `sudo lsof -nP -iTCP:8021 -sTCP:LISTEN`, then kill the PID. If it belongs to Docker/Colima forwarding, use `colima stop` instead.
2. `colima stop && colima start`. Colima was already stopped while the port stayed open, so this is expected to fail, but it is cheap.
3. Reboot — the only guaranteed way to clear an orphan socket.
4. If it survives a reboot, check `launchctl list | grep -i 8021` and `~/Library/LaunchAgents`.

**Port convention**: 8021 is reserved and unusable. Scripts that need a temporary server take a random free port and fail if it is taken (`first_look_audit.sh`, `verify-bundle-parity.mjs`). Fixed-port scripts use 4170–4199.

## Before any future release

1. Run `bash scripts/first_look_audit.sh` and confirm `summary.json` reports no failures.
2. Run `uv run pytest -q`, the two front-end `typecheck` / `build` / `smoke` suites.
3. Run `uv run python scripts/check_web_parity.py` and `scripts/check_typography.py`.
4. Check the tree for key material, invite codes and private paths.
5. Only push or deploy with explicit permission.
