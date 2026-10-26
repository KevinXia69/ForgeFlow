# Third-party sources and licenses

Direct-source and service-document references checked 2026-10-25.

This file records the upstream sources, runtime dependencies, and build inputs
that ForgeFlow reuses directly. It does not change third-party terms or grant
rights to third-party material under ForgeFlow's Apache-2.0 license. Dependency
installation is not evidence that a manufacturing task passed acceptance;
results come from the product's own runs.

## ERP-Bench

- Upstream: [agentic-labs/erp-bench](https://github.com/agentic-labs/erp-bench).
- Pinned commit: `ceba3880af555129b5278e056a0c20f2fb5a0ba9`.
- License: **CC0 1.0 Universal (CC0-1.0)**, verified against the
  [pinned LICENSE](https://github.com/agentic-labs/erp-bench/blob/ceba3880af555129b5278e056a0c20f2fb5a0ba9/LICENSE).
  Full text is also at `vendor/erp-bench/LICENSE` after
  `python3 scripts/fetch_upstream.py`.
- Reused: Odoo task environments, procurement/planning reference solvers,
  original acceptance programs, and task statements. The fetched upstream tree
  is checked against `upstream.lock.json`; ForgeFlow integration and generated
  run artifacts are separate from that source tree.

## Pi Agent Harness

- Locked source: [agentic-labs/pi-mono](https://github.com/agentic-labs/pi-mono),
  matching `upstream.lock.json`.
- Pinned commit: `08dc60bc52d89d6823a9738cc90b1916e5e446e5`, package version `0.85.1`.
- License: **MIT**, verified against the
  [pinned LICENSE](https://github.com/agentic-labs/pi-mono/blob/08dc60bc52d89d6823a9738cc90b1916e5e446e5/LICENSE).
  Copyright (c) 2025 Mario Zechner; the complete MIT notice is retained in `NOTICE`.
- Reused: the single-agent tool loop, model connectivity, and session handling
  used by the autonomous research path. The fetched source tree is kept separate
  from ForgeFlow's integration; local trial patches must be recorded with the trial.

## DeepSeek Harness (DSH)

- Upstream: [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness).
- License: **MIT**, with Copyright (c) 2026 DeepSeek; see the
  [upstream LICENSE](https://github.com/deepseek-ai/deepseek-harness/blob/master/LICENSE).
- Python assistant SDK: `deepseek-harness-sdk==0.2.1rc2` in `pyproject.toml` and
  `uv.lock`; the [exact PyPI release](https://pypi.org/project/deepseek-harness-sdk/0.2.1rc2/)
  declares MIT.
- Assistant CLI: `@deepseek-ai/dsh@0.2.1-rc.2` in `deploy/vps/dsh/package-lock.json`;
  the [exact npm release metadata](https://registry.npmjs.org/@deepseek-ai%2fdsh/0.2.1-rc.2)
  declares MIT.
- Both web clients lock `@deepseek-ai/dsh-client-store`,
  `@deepseek-ai/dsh-util-code-language`, `@deepseek-ai/dsh-util-workspace-path`,
  and `@deepseek-ai/dsh-client-ui-primitives` to `0.2.1-rc.2`, with MIT license
  metadata. The UI primitives package is installed as `@forgeflow/dsh-primitives`.
- Ported source: `ReasoningRow`, `DisclosureRow`, `TextShimmer`, and their CSS
  modules from `0.2.1-rc.2`, adapted for ForgeFlow's React state and stream data.
  The source paths and adaptation notes are retained in
  [web/src/dsh/README.md](web/src/dsh/README.md) and
  [web-public/src/dsh/README.md](web-public/src/dsh/README.md). Complete MIT notices
  remain in [web/src/dsh/LICENSE](web/src/dsh/LICENSE) and
  [web-public/src/dsh/LICENSE](web-public/src/dsh/LICENSE).

The current web manifests no longer depend on `@assistant-ui/react`; the old
notice describing it as the current chat runtime has been replaced by the DSH
entries above. Package licenses remain applicable alongside the ported-source
notices.

## Runtime and build frameworks

- **FastAPI**, **React 19**, **Vite** — MIT.
- **TypeScript** — Apache-2.0, as recorded in the web lockfiles and
  [upstream license](https://github.com/microsoft/TypeScript/blob/main/LICENSE.txt).
- **Harbor 0.24.0**, **OR-Tools**, **playwright-core** — Apache-2.0.
- **Odoo 19 Community** — LGPL-3.0 for the core, verified against its
  [19.0 LICENSE](https://github.com/odoo/odoo/blob/19.0/LICENSE). Odoo notes that
  some bundled components have other compatible licenses. ForgeFlow's VPS
  task image builds from `odoo:19` at the digest in `deploy/vps/Dockerfile.odoo`.
- **PostgreSQL 18** — [PostgreSQL License](https://www.postgresql.org/about/licence/).
  The VPS task image installs it from the PostgreSQL package repository.

Odoo and PostgreSQL binaries are installed when building the environment; this
Git repository supplies build instructions. Installed package/image notices
continue to apply. Distribution of a built container image must retain the
licenses and other materials required by its included components.

## Hosted inference: Nebius Token Factory and NVIDIA Nemotron

ForgeFlow's default integration sends API requests to
`https://api.tokenfactory.nebius.com/v1` using the configured model identifier
`nvidia/Nemotron-3.5-Super-VL`. This repository distributes integration code and
configuration examples, not model weights or provider credentials.

- Current service documents: [Nebius Services Agreement](https://docs.nebius.com/legal/agreement),
  [Token Factory Supplemental Terms](https://docs.nebius.com/legal/token-factory),
  and the incorporated [Acceptable Use Policy](https://docs.nebius.com/legal/aup).
  The supplemental terms cover API integration into customer products and
  incorporate applicable model terms. A software license does not replace the
  service agreement, account conditions, or end-user obligations.
- The [legacy Token Factory terms](https://docs.tokenfactory.nebius.com/legal/terms-of-service)
  say the updated agreement applies from September 28, 2026, except for specified
  customers whose active September 15 reserve has not expired. This source review
  does not determine an account's reserve or negotiated order terms.
- NVIDIA's [official Nemotron-3.5-Super-VL BF16 model card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-BF16)
  identifies **OpenMDW-1.1** and links the
  [OpenMDW License Agreement version 1.1](https://openmdw.ai/license/1-1/).
  This is the model publisher's reference; it does not establish Nebius's exact
  hosted weight revision or precision.
- Calling a hosted model does not distribute its weights. If a later release
  distributes model materials, OpenMDW-1.1 requires retaining the agreement and
  applicable copyright and origin notices. Model/provider names remain their
  owners' trademarks; no endorsement or blanket rights clearance is implied.

## Build inputs and review scope

- `uv.lock`, `web/package-lock.json`, `web-public/package-lock.json`, and
  `deploy/vps/dsh/package-lock.json` record resolved Python/JavaScript versions.
  Their manifests describe which dependencies and optional groups are used.
- `upstream.lock.json` pins ERP-Bench and Pi archives by commit and SHA-256.
- Container bases and operating-system package installation instructions are
  recorded in `deploy/`. The VPS Odoo base is pinned by digest; PostgreSQL is
  constrained to major version 18, and operating-system package versions can
  change between builds.

This review checked the direct sources and licenses identified above, retained
ported-source notices, lockfile metadata, and linked service/model documents.
It is not a complete license audit of transitive packages, operating-system
packages, container contents, model training data, or all use-specific rights.
For the resolved dependency graph use `uv tree` and `npm ls --all` in each
JavaScript package directory, then consult each installed package's own license.
