# Local installation

[English README](../README.md) · [中文说明](../README.zh-CN.md)

The v3.10.0 package starts the workbench, an independent synthetic Odoo 20 ERP,
its PostgreSQL database, the PostgreSQL control database, Qdrant, source/task
services and the document worker. It uses the public application source and
your provider accounts. It does not connect to the hosted demonstration's
workspace or copy its data.

## Before starting

- Windows: Docker Desktop with a working Linux-container backend, normally WSL2.
- macOS: Docker Desktop running Linux containers.
- Linux: Docker Engine and Compose v2, with permission to use the local daemon.
- Reserve approximately 4 CPUs, 8 GiB RAM and 20 GiB free disk for the initial
  stack; larger uploads and retained histories need additional storage.
- Internet access is needed for images or source-build dependencies and for
  model, document-processing, embedding and reranking API calls.

Use a current Docker Desktop or Docker Engine 28 or newer. Docker documents that
older engines can expose localhost-published ports to other machines on the
same local network. The launcher checks the local context and Linux-container
mode, but does not enforce a minimum engine version. See
[Docker's port-publishing documentation](https://docs.docker.com/engine/network/port-publishing/).

The launcher accepts a local Docker context. It refuses a remote Docker daemon
so a local installation cannot accidentally be created on another machine.
Host Python, Node.js, PostgreSQL and Odoo installations are unnecessary for the
release package. CPU architecture acceptance is listed separately below; do not
infer native Apple Silicon support from the presence of a macOS launcher.

## First run

1. Download the package for your system from
   [GitHub Releases](https://github.com/KevinXia69/ForgeFlow/releases), verify its
   published checksum, and extract the entire archive into a normal writable folder.
   Keep `compose.yaml`, the launchers and the bundled `source/` together.
2. Start Docker. Run `Start-ForgeFlow.cmd` on Windows,
   `Start-ForgeFlow.command` on macOS, or `bash start.sh` on Linux.
3. The launcher opens a private one-time setup link on `127.0.0.1:17801`.
   Choose a chat profile and supply its service keys below.
4. Save the generated invitation code shown by the authorized setup page.
   It grants access to this installation; there is no shared default code.
5. Wait for initialization and health checks. Open
   [http://127.0.0.1:17800/app/](http://127.0.0.1:17800/app/) and sign in.

Setup links expire and cannot be reused after saving. Run the launcher again if
the link expires. If automatic browser opening is unavailable, use
`bash start.sh --no-open` or `Start-ForgeFlow.cmd -NoOpen`; treat the printed link
as private. Do not paste it into an issue or screenshot.

Saving checks key format; it does not prove the account is funded or the service
is reachable. Initialization must publish the reference index successfully before
the application is presented as ready. A failed startup is not a successful
installation and should not be bypassed by disabling the relevant service.

## Keys and privacy

| Setup field | Service | Scope |
| --- | --- | --- |
| Chat provider and API key | Provider matrix below | DSH chat and investigation; selected model capabilities apply |
| DeepSeek search API key | DeepSeek | Official web-search auxiliary model; official DeepSeek chat reuses its chat key |
| Document API key | DashScope | DocumentMind parsing; original and converted document content may be submitted |
| Semantic API key | SiliconFlow | Qwen3 Embedding 8B with 4096 dimensions and Qwen3 Reranker 8B |

Official DeepSeek chat uses three keys in total. Other cloud chat choices use
four: their chat key, DeepSeek search, DashScope and SiliconFlow. Ollama's local
chat endpoint needs no cloud chat key, but search/document/semantic services
still require their respective credentials. Web search invokes an auxiliary
DeepSeek model and incurs provider usage independently of the chat provider.

The public workbench retains its configured DeepSeek V4.1 Flash 1017 / Max
default; visitors do not need to configure keys. Personal browser overrides are
optional, stay in tab memory, and affect Home and live-case Q&A. Historical
examples remain local reference views. They do not change the shared platform
default or background investigations. Hosted overrides accept only permitted
public HTTPS hosts; a custom public host also needs administrator allowance.

## Provider and protocol matrix

| Local setup profile | Available protocol routes |
| --- | --- |
| DeepSeek | OpenAI Chat Completions; OpenAI Responses; Anthropic Messages |
| Nebius | OpenAI Chat Completions |
| OpenAI | OpenAI Responses; OpenAI Chat Completions |
| Anthropic | Anthropic Messages |
| Google / Gemini | Google native; OpenAI Chat Completions |
| OpenRouter | OpenAI Chat Completions; Anthropic Messages |
| DashScope / Qwen | OpenAI Chat Completions |
| SiliconFlow | OpenAI Chat Completions |
| Ollama | Local OpenAI-compatible endpoint |
| Custom | OpenAI Chat Completions, OpenAI Responses or Anthropic Messages |

These are connection routes, not proof that every model or account supports
tools, images or the same reasoning controls. Setup lets you choose the model
and endpoint, explicitly declare image input, and test the connection. Changing
an endpoint or choosing an unknown model does not inherit an image-capability
claim. Google native uses the SDK's built-in catalog provider. Local HTTP is
limited to approved loopback/container-host endpoints in local setup; browser
overrides require a service-allowed HTTPS endpoint. A remote installer cannot
reach the visitor's local Ollama instance.

The setup configures the server runtime, including the assistant's tool/stream
path. This is distinct from temporary model settings inside the browser.
DeepSeek uses the reviewed Max reasoning configuration. A different model or
provider can change output quality, modality support, latency and cost.

Qdrant runs inside the local stack with a generated key and local TLS trust.
The installer also generates internal database, session and ERP credentials.
The user does not need a separate hosted Qdrant account.

Keys are stored in the private Docker configuration volume with restricted
filesystem permissions. **They are not encrypted at rest.** The Docker daemon
and an administrator of your computer can access them. Backups containing that
volume must stay private. Credentials are not included in the source, release
archive or ordinary application logs.

Prompts and the authorized evidence used for a response are sent to the chosen
model provider. Enterprise uploads use the document service; indexed text uses
the embedding/reranking service. Local hosting does not mean offline inference.
Use data you are authorized to send to those services.

The setup and application bind to loopback. Do not expose these development
ports publicly by changing them to `0.0.0.0` without a separate deployment and
authorization review. Sign-out revokes the local browser session; normal Docker
shutdown preserves application records and is not an account deletion.

## What is included

| Workflow | Required services | What to verify |
| --- | --- | --- |
| Assistant text, images and temporary attachments | Chat, API, DSH | A real provider response and usable attachments, not a fabricated local success |
| Enterprise document knowledge | DashScope, SiliconFlow, Qdrant, control DB, worker | Parsing completes; published sources are searchable and versioned blocks open |
| Procurement reference question | Installed reference pack and semantic/model path | External guidance stays separate from company authorization |
| Reviewed product comparison | Authorized current catalog and reference sidecar | Explicit reference-facts label, correct source conditions and source viewer |
| Historical impact example | Bundled historical projection | Historical banner, S00004 scope, no automatic model send or ERP write |
| ERP lookup and tasks | Local Odoo, control bindings | Real local purchase, sales, stock and production records are readable |
| Recovery preparation | Odoo facts, model investigation where used, CP-SAT | A concrete supported plan with complete facts and explicit checks |
| Approval and execution | Authorized actor, native add-on, supervisor | Approval binds the exact plan; execution produces receipts and readback |
| Continuation | Named volumes and durable service state | Stop/resume retains tasks, documents, identity and ERP results without replay |

The synthetic ERP is a real local database. Approval/execution exercises change
its records. The historical example is a separate read-only projection and must
never be imported as a live pending approval. The release does not include
production business records, benchmark answer keys or current hosted task queues.

## Stop, resume and upgrade

| Action | Windows | macOS / Linux |
| --- | --- | --- |
| Start or resume | `Start-ForgeFlow.cmd` | Open `Start-ForgeFlow.command` or run `bash start.sh` |
| Stop | `Stop-ForgeFlow.cmd` | `bash stop.sh` |
| Update provider keys | `Start-ForgeFlow.cmd -Configure` | `bash start.sh --configure` |
| Keep browser closed | Add `-NoOpen` | Add `--no-open` |

The stop flow stops new work, checks active tasks and waits for running services
to drain. If active work does not become idle, it reports that state instead of
killing a worker or pretending shutdown succeeded. Resolve the original task
and retry; do not create a duplicate operation to escape an unknown outcome.

The Compose project is `forgeflow-local`. Configuration, service secrets, Odoo
filestore, both PostgreSQL databases, Qdrant data/snapshots and application data
use named Docker volumes. Application data includes uploaded originals,
attachments, assistant state and journals. A normal stop or container replacement
retains these volumes. The source archive is not a backup of them.

For an upgrade:

1. Read the target release's migration/compatibility notes.
2. Stop the existing installation with its own stop launcher and confirm success.
3. Back up the named volumes while the stack is stopped, including configuration
   and both databases. Keep the complete set together and private.
4. Extract the new package and use its launcher with the same local Docker
   context and project. Inspect readiness and retained state before business work.

There is no automatic database downgrade or rollback. Returning to an older
image after a schema change is not a restore. Do not run `docker compose down -v`,
delete Docker volumes, reset Docker Desktop or remove a runtime binding as a
routine troubleshooting step: those actions can destroy retained records.

## From source

The complete release includes the reviewed public `source/` directory and a
`compose.source.yaml` override. If prebuilt images cannot be pulled, the launcher
builds these local sources automatically. This still needs the public base images
and language-package registries; it is not an offline installer. It does not
download or execute another source repository to make the fallback work.

Contributors can build the two image roles from a source checkout with:

```bash
docker compose -p forgeflow-local \
  -f deploy/workbench/compose.yaml \
  -f deploy/workbench/compose.source.yaml build setup odoo-init
```

Image construction is separate from first-run configuration, initialization and
acceptance. Use the release bundle's launchers for the complete setup flow.
Application source, migrations, Odoo add-on, runtime scripts, DSH lock, conversion
template and reference pack are included; the old filtered export is not used.

For Python/frontend development, see `pyproject.toml`, `uv.lock` and
`web/package-lock.json`. The release frontend is built inside Docker and copied
into the API's static application directory. Running `npm run build` in the
checkout alone writes `web/dist`; it does not start a configured workbench.

## Troubleshooting

- **Docker unavailable:** start the local Docker daemon and check Linux-container
  mode. The launcher does not install Docker or change to a remote context.
- **Image pull failed:** keep the bundled `source/` and source Compose override.
  Source builds still require internet access; no private registry token is needed.
- **Setup expired:** rerun the launcher. Do not reuse an old fragment URL.
- **Invalid or unavailable provider:** check account access, billing, region and
  the selected key. Reconfigure with the launcher; do not put keys into Git.
- **Document/index initialization failed:** retain the original state and inspect
  the reported service. A healthy API alone is not knowledge acceptance.
- **Operation unknown:** inspect and reconcile the original task and receipts.
  Do not reset the installation or duplicate the execution.

From the extracted package, `docker compose -p forgeflow-local -f compose.yaml ps`
shows local service status. Diagnostic output and logs can still contain business
content; review and redact before sharing them.

## Installation acceptance

Downloadable releases are created only after the following gates pass. A source
checkout, an existing launcher or a passing health endpoint does not establish
that a release has completed them.

| Target | Required release gate and its limits |
| --- | --- |
| Linux amd64 and arm64 | Native launcher checks and real empty PostgreSQL/Odoo/Qdrant stacks, API/ERP checks, synthetic approval/execution/readback, and stop/resume preservation |
| macOS | Native shell launcher and package checks with a Docker substitute; this does not certify a complete installation on every Docker Desktop host |
| Windows | Actual PowerShell launcher and package checks with a Docker substitute; this does not certify a complete installation on every Docker Desktop host |
| Apple Silicon / ARM | The Linux arm64 image must pass on an arm64 runner; this is separate from host-specific Docker Desktop acceptance |

Empty-stack tests use real local databases and synthetic ERP records, with
fixture external services for repeatable, credential-free CI. They do not
establish universal model quality, provider account permissions, quotas, image
capabilities or document-service availability. Provider-specific checks have
their own bounded scope.

Source checks, HTTP wizard tests and synthetic browser tests are separate from
a complete installation using real provider services. The release acceptance
record must identify the OS/architecture, source/image versions, providers,
feature results, failed/skipped checks, restart preservation and logout behavior.
README screenshots show the current public interface and are labeled accordingly;
the local installer uses the same interface. Screenshots do not replace the
separate local-stack installation and persistence checks.

Additional feature acceptance includes all three examples, a real model turn,
document parsing/indexing and source viewing, ERP lookup, supported synthetic
prepare/approve/execute/readback, service continuity and stop/resume preservation.
Historical benchmark scores and a passing `/api/health` do not replace these checks.
