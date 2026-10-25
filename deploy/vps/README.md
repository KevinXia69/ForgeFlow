# ForgeFlow on a Linux VPS

This runtime serves the actual approval-gated ERP workflow. The initial supported
business environment is ERP-Bench scenario A, task 2280. The seed is a controlled
benchmark business dataset; successful deployment does not connect a customer's
production ERP automatically.

The Odoo image uses the repository's `planning-metadata-v1` data profile. It
copies existing per-product planning facts into workcenter public notes; the
projection verifies that all non-note values remain unchanged. Its original
scenario file, derived file, hashes and diff are preserved. Original setup and
verifier code remain unchanged, but this is a derived visibility condition,
not the unmodified benchmark-data condition.

## Operating model

Nginx terminates HTTPS and forwards to one API worker on loopback. The API runs as
`forgeflow-api`; a fixed launcher runs the model harness as `forgeflow-dsh`. Only
the API UID can reach the root executor through its Unix socket. Docker, Odoo's
HTTP listener, PostgreSQL and ERP credentials are private. The assistant has
scoped read-only evidence tools. A user approves the exact prepared plan before
the executor performs business writes and invokes the original verifier.

One ERP generation belongs to one reviewer at a time. Ownership persists across
restarts and completed executions. Other reviewers can sign in and read their
own history, but cannot claim the occupied ERP. This is intended for 3–5 invited
reviewers taking turns, not isolated concurrent ERP tenants. The administrator
rotates to a fresh generation between reviewers, retaining prior evidence.

## Installation

Use Ubuntu 24.04 x86-64, Docker and Python 3.12. The initial instance is 4 vCPU,
8 GiB RAM and 160 GiB disk. Install the project with `planning` and `dsh-sdk`
extras, the API versions in `deploy/vps/requirements.txt` and its built public frontend. Fetch and verify the
pinned sources with `python scripts/fetch_upstream.py`.

Build `deploy/vps/Dockerfile.odoo` as `forgeflow-odoo:vps1`. Follow
[odoo-runtime.md](odoo-runtime.md) for its persistence contract and
[RUNTIME.md](RUNTIME.md) for Node, the fixed DSH dependencies and Linux identities.
Keep `runtime.env`, `runtime.json`, backup keys and OAuth credentials outside the
checkout, root owned, with mode 0600 or stricter.
Use `runtime.example.json` and `runtime.env.example` as structural templates.
Generate a unique high-entropy session secret and separate random reviewer
invitations; set exact trusted hosts and your actual model endpoint and key.
Set `allow_initialize: true` only for an explicitly new empty sandbox, then return
it to `false` after successful initialization. The example intentionally defaults
to refusing initialization and has no working credentials.

Only an explicitly enabled empty sandbox may initialize. Normal starts validate
the recorded image, container, volume, generation, seed receipt, API key and task
policy. Never delete these files to make a failed initialization or restart pass.
Diagnose the retained instance or create a separate explicitly new instance.

Install the provided Nginx configuration after adjusting its public IP and
certificate paths. The API listener stays on `127.0.0.1:8000`; Odoo is published
only on `127.0.0.1:18069`. Expose ports 80 and 443, and restrict SSH to the
operator's verified source address with key authentication.

## HTTPS and renewals

A Let's Encrypt IP certificate avoids a required domain purchase. Use Certbot
5.4 or later, `--preferred-profile shortlived`, `--ip-address` and HTTP webroot
validation. Test with the staging CA before requesting a production certificate.
IP certificates last approximately six days, so the included renewal timer
checks every six hours and reloads Nginx after successful renewal. Test
`certbot renew --cert-name forgeflow-ip --dry-run` and inspect the systemd timer.
Do not serve the staging certificate to reviewers.

## Service operations and evidence

Use `systemctl start|stop|restart forgeflow-runtime`. Its supervisor preserves
Odoo and starts the trusted executor plus low-privilege API as a unit. Odoo
restarts pass through the supervisor's generation checks; Docker's independent
restart policy is disabled. Do not run a second API worker or
an independent executor against the same sandbox.

Before public handoff, record a real HTTPS login, model answer, prepared plan,
approval, ERP writes, readback and original-verifier success. Verify a repeated
approval returns the same execution without duplicate writes, other reviewers
cannot read or mutate the case, and service/container restart preserves results.
A passing health check or a synthetic unit test alone is insufficient.

The included backup workflow is an explicit maintenance operation: stop API and
executor, then Odoo, and create an authenticated encrypted logical backup with
matching filestore and execution state. See [odoo-runtime.md](odoo-runtime.md).
Cloud machine snapshots are an additional layer, not proof of logical recovery.
Google Drive upload requires a separate private runtime OAuth configuration;
the assistant's connected Drive does not grant server access. Until configured,
backup receipts explicitly identify archives as local only.

Retain an independent copy of the backup encryption key. Verify archive staging,
logical database restoration and a complete isolated application/verifier run
before claiming the full recovery procedure is proven.
