# Third-party sources and licenses

Verified 2026-10-20.

This file records the upstream source, runtime frameworks and frozen build inputs
that ForgeFlow reuses directly. It does not choose a license for ForgeFlow's own
code and does not change the terms of third-party material. A component being
fetched, built or declared as a dependency is not evidence that a manufacturing
task passed acceptance; results come from the product's own runs.

## ERP-Bench

- Upstream: [agentic-labs/erp-bench](https://github.com/agentic-labs/erp-bench).
- Pinned commit: `ceba3880af555129b5278e056a0c20f2fb5a0ba9`.
- License: **CC0 1.0 Universal (CC0-1.0)**, verified against the LICENSE file at
  that commit. Full text: `vendor/erp-bench/LICENSE` after
  `python3 scripts/fetch_upstream.py`.
- Reused: Odoo task environments, the procurement/planning reference solver, the
  original acceptance programs and the task statements. ForgeFlow does not
  modify the upstream tree; the pinned file tree is verified by
  `scripts/fetch_upstream.py` and recorded in `upstream.lock.json`.

## pi-mono

- Upstream: [badlogic/pi-mono](https://github.com/badlogic/pi-mono).
- Pinned commit: `08dc60bc52d89d6823a9738cc90b1916e5e446e5`.
- License: **MIT**, verified against the LICENSE file at that commit.
- Reused: the single-agent tool loop used by the autonomous research path. The
  upstream tree stays unmodified; any local patch is recorded per trial.

## Runtime frameworks

- **FastAPI** — MIT.
- **Odoo 19** (bench container) — LGPL-3.0; used as the ERP under test, not
  distributed with this repository.
- **React 19**, **Vite**, **TypeScript** — MIT.
- **@assistant-ui/react** — MIT.
- **OR-Tools** — Apache-2.0.
- **playwright-core** — Apache-2.0.

## Build inputs

- `uv.lock` and `web/package-lock.json` pin every dependency version used to
  build and test the product.
- `upstream.lock.json` pins the two upstream archives by commit and SHA-256 so a
  reader can reproduce the exact source tree this repository was verified with.
- Container images referenced by `deploy/` and the local workbench are pinned by
  tag and recorded in the deployment files.

## Not covered

This file does not list transitive dependencies. Run `uv tree` or
`npm ls --all` for the full graph, and consult each package's own license.
