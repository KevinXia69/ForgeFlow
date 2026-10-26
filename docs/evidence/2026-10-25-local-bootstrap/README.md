# Isolated local workbench bootstrap check

These are operator-recorded checks performed on October 25, 2026 Pacific time
(October 26 UTC). They validate the repaired local installation path separately
from the hosted judge deployment. They are not an independent attestation,
benchmark score, whole-site recovery test, or a claim that every platform works.

## Environment and isolation

The Windows development host had no WSL installation or reachable Docker Desktop
Linux engine. Container checks therefore ran in a separate source directory on
the Ubuntu 24.04 AMD64 VPS, using a fresh Python 3.12.3 virtual environment,
separate container, database, case store, lifecycle records, socket, and loopback
API/Odoo ports 8200/18200. The active hosted service was not reused as the test
workbench. Docker reused existing base/package layers, then built the new local
image from its staged source inputs; no existing runtime image was relabelled
and counted as a build.

The tested input was a source archive of the current worktree, not a new Git
clone. [Source hashes](source-hashes.json) identify the actual executed bytes
for the changed scripts and relevant dependencies. `raw_sha256` hashes those
Linux-directory bytes. `canonical_lf_sha256` replaces CRLF with LF solely to
compare against GitHub's normalized text; it does not claim another test of
those normalized bytes. JSON receipts in this directory are copied unchanged
from the test directory.

## Results

| Check | Result |
| --- | --- |
| Both frontends, independent directories without node_modules | `npm ci`, `npm run typecheck`, `npm run build` passed on Windows; large bundle warnings remain |
| New Linux Python environment | `uv sync --frozen --extra planning --extra dsh-sdk` passed |
| Locked upstream inputs | ERP-Bench `ceba3880af555129b5278e056a0c20f2fb5a0ba9` and pi-mono `08dc60bc52d89d6823a9738cc90b1916e5e446e5` downloaded and verified |
| Local Scenario A image | Built from `scripts/build_local_image.py` and `deploy/local/Dockerfile.odoo` |
| Local Scenario B | Final build inputs staged/verified with `--prepare-only`; its image and runtime were not tested |
| Assistant installation | Locked npm install, composition generation and CLI startup passed: Node 24.22.1, DSH CLI 0.2.1-rc.2, Python SDK 0.2.1rc2 |
| Local lifecycle regression | `tests/test_local_workbench.py`: 136 passed, including first-boot/resume timeout and stopped-container checks |
| Fresh Scenario A initialization | Original Odoo setup completed; local API/executor started successfully |
| HTTP and authentication | Health and `/app/` returned 200; unauthenticated cases returned 401; invite login returned the specified 201 |
| Real case planning | New case `029d62ca327e46709a77294c588b268b` reached `awaiting_approval` with no approval or execution receipt |
| Preservation | Same session could read the same case after stop/resume |
| Cleanup | Both test containers stopped; API/executor PID records cleared; no test processes remained |

The successful image ID is
`sha256:d23d570b3b2b39234083e786f00b797b1eafe892e5d8661d159343383af9244f`.
See [continuation result](continuation-check.json) and [cleanup](cleanup.json).

## Failures and corrections retained

The [initial attempt](initial-failure.json) failed because the original setup
client's module-install RPC timed out after 60 seconds. On the successful fresh
attempt, loading the 76 required Odoo modules alone took 125.36 seconds. The
local image now wraps the unchanged original setup program with a setup-only
900-second RPC timeout and longer Odoo request limits. The launcher's first-boot
readiness budget is 30 minutes; normal resume remains bounded at three minutes.
Original verifier source and business inputs were not changed by this repair.

The [next check](initialization-check.json) records successful `init` and `start`
but an `AssertionError`: the test expected login HTTP 200 instead of the API's
documented 201. Correcting that assertion and resuming the same new instance
produced the successful continuation result. Neither failure was discarded or
reported as an end-to-end pass. Failed and successful containers and local
evidence were retained after stopping their services.

## Not covered

The running assistant was disabled during the bootstrap interaction; the DSH
installation and CLI version check did not call a model. This test did not
approve an ERP write or run the original business verifier. It did not test a
fresh Scenario B container, macOS/ARM64, Windows native execution, five-judge
concurrency, or whole-site disaster recovery. Hosted approval/execution/model
checks have separate evidence and must not be inferred from this bootstrap test.
