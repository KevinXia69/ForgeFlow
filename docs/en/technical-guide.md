# ForgeFlow technical guide

The workbench combines a React/TypeScript/Vite frontend, a Python/FastAPI API,
Odoo 20, PostgreSQL, and OR-Tools CP-SAT. This guide describes the application;
[local installation](../LOCAL_INSTALL.md) identifies the packaged services and
their configuration. See [current capabilities](current-capabilities.md) for
scope and [the product guide](product-guide.md) for users and evidence limits.

## Investigation, planning and execution

DeepSeek Harness (DSH), its Python SDK and its Node.js runtime provide the
assistant's model/tool loop. A framework name does not identify the inference
model: the configured provider, model and turn metadata do. The local installer
configures the user's own chat service. The browser's temporary model settings
are a separate fallback and do not configure the DSH process.

The control layer persists source events, tasks, assignments, attention,
approvals, effects and service heartbeats. Its source, dispatcher, watchdog and
outbox loops coordinate work. A model worker investigates with authorized tools;
the numerical planner is a program using CP-SAT, not another language model.
The repository also retains Pi/ERP-Bench research integrations; installing the
workbench does not launch a benchmark or enable autonomous research execution.

Odoo 20 plan preparation records the reviewed action set, snapshot and versions.
An authorized human approves that exact plan. Native Odoo roles, company scope
and plan-bound delegation apply again at execution. Changed plans need fresh
approval. Execution receipts and independent readback retain the result or an
explicit unknown state. Chat cannot approve or execute ERP operations.

Multi-level BOM and multi-product preparation consume explicit ERP quantities,
relations, supplies and resources. Missing facts are not unlimited stock. Solver
timeout, infeasible, feasible and optimal are separate outcomes. Material
substitution and arbitrary factory scheduling are not implied by BOM support.

## Knowledge, documents and citations

The semantic service stores 4096-dimensional embeddings in Qdrant and applies a
configured reranker. The server resolves tenant, published generation and ACL
before retrieval. There is no production keyword fallback that silently replaces
an unavailable semantic service. Quoted passages keep their document version,
line range or structured locator; an unavailable or withdrawn source is not a
valid current citation.

Enterprise uploads use the configured DocumentMind service. Office and structured
formats use the included conversion tools where required. Original files,
conversion provenance, parse status, blocks and versions remain associated.
Partial, failed or review-required parsing is displayed explicitly. Temporary
chat attachments are separate from publishing a document to the knowledge base.

The bundled procurement reference pack contains reviewed summaries and links,
not company policy or live product prices. Its installer validates hashes and
updates the document catalog and index. A narrow reviewed parameter comparison
also verifies its sidecar, source bytes, current document versions, publication
and access scope before rendering. The UI labels this `reference_facts` answer;
it is not presented as free-form model generation. Other questions use the
ordinary assistant path.

MCP supplies DSH's fixed read-only evidence tools over standard input/output.
The knowledge tool uses a scoped server capability. The assistant process
receives an explicit environment allowlist, not session secrets or ERP write
credentials. Container boundaries and application authorization have different
roles; the model prompt is not the permission boundary.

## State and acceptance

Odoo business data and control state use PostgreSQL. Other application state
includes account/session records, document originals, attachments, assistant
state and turn journals. Browser chat uses session storage; temporary browser
model keys stay in memory and are cleared on session retirement.

The packaged local services persist their state in named Docker volumes.
Stopping or replacing containers must preserve those volumes. No account,
invitation, provider key, document or production database is shipped in the
release. Initialization creates a new local workspace.

A running API or a fluent answer does not prove a successful ERP change.
Acceptance separately checks plan preparation, authorized approval, execution,
readback and verification. Historical ERP-Bench original-verifier scores belong
to their recorded synthetic task versions, not every current Odoo 20 action.
Neither synthetic acceptance nor a model answer establishes physical delivery,
customer adoption or financial savings.

## Source pointers

- API and scoped assistant: `src/forgeflow/api/`, `scripts/assistant_mcp.py`.
- Persistent orchestration: `src/forgeflow/superagent/`, `migrations/control/`.
- Odoo authorization and actions: `odoo-addons/forgeflow_general/`,
  `src/forgeflow/integrations/odoo20/`.
- Planning: `src/forgeflow/planning/`.
- Documents and semantic retrieval: `src/forgeflow/knowledge/`,
  `src/forgeflow/semantic/`, `knowledge/reference/supply-recovery-v1/`.
