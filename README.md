# ForgeFlow

**Recover purchasing and production plans, from disruption to verified ERP changes.**

ForgeFlow combines an AI workbench, document knowledge, constrained planning and
human-approved execution in a local Odoo 20 environment. Investigate a supplier
cancellation or production disruption, review a concrete recovery plan, approve
its exact actions, and inspect the resulting ERP records and receipts.

[Download](https://github.com/KevinXia69/ForgeFlow/releases) ·
[Installation guide](docs/LOCAL_INSTALL.md) ·
[Capabilities](docs/en/current-capabilities.md) ·
[中文](README.zh-CN.md)

![The current public ForgeFlow workbench home screen](docs/images/local-workbench-home.png)

*Current public workbench; the local installer uses the same interface.*

## Install on your computer

**Docker must be installed and running in Linux-container mode.** Use Docker
Desktop on Windows or macOS, or Docker Engine with Compose v2 on Linux. We
recommend Engine 28 or newer; see the [network boundary](docs/LOCAL_INSTALL.md#before-starting).
The release launcher does not require host Python or Node.js.

| Computer | Release package | Start after extracting the complete package |
| --- | --- | --- |
| Windows | [ForgeFlow-v3.11.0-Windows.zip](https://github.com/KevinXia69/ForgeFlow/releases/download/v3.11.0/ForgeFlow-v3.11.0-Windows.zip) | Double-click `Start-ForgeFlow.cmd` |
| macOS | [ForgeFlow-v3.11.0-macOS.zip](https://github.com/KevinXia69/ForgeFlow/releases/download/v3.11.0/ForgeFlow-v3.11.0-macOS.zip) | Open `Start-ForgeFlow.command` |
| Linux | [ForgeFlow-v3.11.0-Linux.zip](https://github.com/KevinXia69/ForgeFlow/releases/download/v3.11.0/ForgeFlow-v3.11.0-Linux.zip) | Run `bash start.sh` |

The launcher opens a one-time local setup page. Choose your chat provider, enter
your own service keys, save the generated private invitation code, and wait for
the local services to become ready. Then sign in at
**[127.0.0.1:17800/app/](http://127.0.0.1:17800/app/)**.

First startup downloads or builds images and initializes a fresh synthetic ERP
workspace. It needs internet access and may take several minutes. If the public
image download is unavailable, the complete package includes a source-build
fallback; it does not require a private registry account.

**Release gates:** downloadable packages are created only after native launcher
checks on Windows, macOS and Linux, plus real empty-stack tests on Linux amd64
and arm64. Launcher checks use a Docker substitute; stack tests use synthetic
ERP records and fixture external services. They do not certify every provider
account or a complete Docker Desktop installation on every host OS.
See the [verification scope](docs/LOCAL_INSTALL.md#installation-acceptance).
Unreleased source checkouts have not necessarily completed these gates.

## Bring your own service keys

The [public workbench](https://forgeflowagent.com/app/) already provides the
configured DeepSeek V4.1 Flash 1017 model with Max reasoning. Reviewers can sign
in and use it without entering provider keys. Its Settings page offers an
optional, temporary personal model override; it does not replace that default.

For a local installation, configure your own services:

| Service | Choose or provide | Used for |
| --- | --- | --- |
| Chat and investigation | Select a supported provider below | Assistant reasoning, tool calls and declared image capabilities |
| Web search | **DeepSeek** API key | The official search tool's auxiliary model; shared with official DeepSeek chat, otherwise a separate key |
| Document processing | **DashScope** API key | DocumentMind parsing of uploaded enterprise material |
| Semantic search | **SiliconFlow** API key | Embeddings and reranking for evidence-backed retrieval |

Official DeepSeek chat needs three keys in total: DeepSeek, DashScope and
SiliconFlow. Other cloud chat providers also need their own chat key, making
four. Local Ollama does not require a chat-provider key, but the other services
still do. Search calls have their own provider usage costs.

The local setup offers DeepSeek, Nebius, OpenAI, Anthropic, Google/Gemini,
OpenRouter, DashScope/Qwen, SiliconFlow, Ollama and custom endpoints. Protocols
include OpenAI Chat Completions, OpenAI Responses, Anthropic Messages and the
Google native route. See the [provider and protocol matrix](docs/LOCAL_INSTALL.md#provider-and-protocol-matrix)
for supported combinations and capability limits.

All service roles are configured in the setup page. Qdrant, PostgreSQL
and Odoo run locally; their internal credentials are generated for your
installation. Provider usage is charged to your accounts. The application is
self-hosted, but model calls and document processing use the chosen external
services. [Configuration and privacy details](docs/LOCAL_INSTALL.md#keys-and-privacy).

## What you can do locally

| Capability | Included workflow |
| --- | --- |
| Ask with evidence | Stream an assistant answer, attach a temporary file or supported image, and open versioned source citations |
| Build a knowledge base | Upload enterprise material, inspect parsing status and original blocks, and retrieve published documents within the signed-in scope |
| Try the three examples | Ask about procurement policy, inspect a reviewed parameter comparison, or explore the clearly labeled historical synthetic recovery case |
| Inspect ERP facts | Look up supported purchasing, sales, inventory, BOM and manufacturing records in the local Odoo instance |
| Investigate disruptions | Follow source events and tracked tasks through evidence collection and recovery preparation |
| Plan recovery | Prepare supported multi-product purchasing and multi-level BOM / multiple-MO alternatives under explicit constraints |
| Review and execute | Approve a specific plan and action set, apply authorized changes to the local ERP, then inspect receipts and independent readback |
| Resume work | Keep configuration, documents, tasks and ERP records across normal stops and container replacement |

Chat remains read-only. Business changes require the authorized approval and
execution path. The included ERP records are synthetic, but approved actions
operate on a real local Odoo database. This is separate from the archived
example, which is read-only. Material substitution, arbitrary ERP plug-and-play
and whole-factory scheduling are outside the supported scope.

[Workbench guide](docs/en/workbench-guide.md) ·
[Technical guide](docs/en/technical-guide.md) ·
[Capability boundaries](docs/en/current-capabilities.md)

## Stop, update keys and preserve your data

- **Stop:** run `Stop-ForgeFlow.cmd` on Windows or `bash stop.sh` on macOS/Linux.
  The launcher drains active work and preserves all named Docker volumes.
- **Resume:** run the same start launcher. Existing installation identity and data
  are reused; startup is not a factory reset.
- **Change keys:** run `Start-ForgeFlow.cmd -Configure` or `bash start.sh --configure`.
  Leave an existing key blank to retain it; changing chat provider requires its key.
- **Upgrade:** stop the old installation successfully, back up its named volumes,
  and follow the new release's migration notes before starting its package.
  There is no automatic database rollback. Do not delete volumes to fix a startup error.

[Operations and troubleshooting](docs/LOCAL_INSTALL.md#stop-resume-and-upgrade).

## Development and verification

The release contains the public source used to build the workbench. Docker
source builds use the included lock files; application code, Odoo add-on,
control migrations, runtime scripts and reviewed reference material are all
included. No production database, user upload, invitation or provider key is
shipped.

```bash
git clone https://github.com/KevinXia69/ForgeFlow.git
cd ForgeFlow
uv sync --frozen --extra planning --extra dsh-sdk
uv run pytest tests/test_distribution.py -q
python3 scripts/check_distribution.py .
```

For a complete source deployment, use the Docker instructions in the
[installation guide](docs/LOCAL_INSTALL.md#from-source). A Python dependency
install or a successful frontend build alone does not initialize the ERP,
control database, knowledge index or background services.

## Recorded research results

The historical product-approval workflow achieved **100/100 on each of two
selected ERP-Bench tasks** in synthetic Odoo 19 environments:

| Task | Applicable original-verifier rules | Recorded score |
| --- | ---: | ---: |
| Supplier cancellation (`2280`) | 51 / 51 | 100 / 100 |
| Workcenter outage (`2299`) | 95 / 95 | 100 / 100 |

These October 2026 results identify evaluated source `bf880f2`; they are not the
installation acceptance result for the current Odoo 20 release, customer
production validation or measured financial savings.

[Supplier-cancellation report][a-report] · [Original score][a-score] ·
[Workcenter-outage report][b-report] · [Original score][b-score] ·
[Product and evidence guide](docs/en/product-guide.md)

## License and contributions

ForgeFlow is licensed under [Apache License 2.0](LICENSE). ERP-Bench, Pi,
DeepSeek Harness, Odoo, PostgreSQL, Harbor, OR-Tools and other dependencies retain
the notices in [NOTICE](NOTICE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
The reference pack documents its separate source attribution and licenses.

Keep credentials, setup links and customer records out of issues and commits.
Report security concerns privately to the repository owner.

[a-report]: https://github.com/KevinXia69/ForgeFlow/blob/9c673c8d479b238a18c489fab0dbac057fb72977/docs/evidence/2026-10-28-next-phase/A-final/report.json
[a-score]: https://github.com/KevinXia69/ForgeFlow/blob/9c673c8d479b238a18c489fab0dbac057fb72977/docs/evidence/2026-10-28-next-phase/A-final/original-reward.raw.json
[b-report]: https://github.com/KevinXia69/ForgeFlow/blob/9c673c8d479b238a18c489fab0dbac057fb72977/docs/evidence/2026-10-28-next-phase/B-final/report.json
[b-score]: https://github.com/KevinXia69/ForgeFlow/blob/9c673c8d479b238a18c489fab0dbac057fb72977/docs/evidence/2026-10-28-next-phase/B-final/original-reward.raw.json
