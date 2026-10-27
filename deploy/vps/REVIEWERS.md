# Independent reviewer environments

The hosted deployment can assign one complete persistent environment to each
reviewer invite. Each environment has its own API and assistant Unix users,
session secret, case and assistant files, executor socket, Odoo database and
filestore, immutable generation, and systemd service. The existing generation
lease remains enabled inside each environment. A reviewer's login never releases
another reviewer's lease or reseeds business data.

One common HTTPS application uses the invitation to select a fixed backend. The
gateway keeps only invitation hashes and its own routing secret. It has no ERP
key, Docker access, executor socket access, or case-store access. Actual case
authorization is still checked by the selected API with that instance's own
session secret. Credentials belong in private testing instructions, not Git.

## Operator installation

This installer extends an already verified VPS installation. It requires the
root-owned legacy `runtime.json`, `runtime.env` and `backup.json`, with six
distinct invitations: the first retains access to historical evidence; the next
five become `judge1` through `judge5`. It reuses the verified immutable Odoo image
and the existing encrypted-backup key. It never changes the legacy service,
reverse proxy or ERP. Review the source before running as root.

```sh
cd /opt/forgeflow
# Two disposable acceptance environments with newly generated private invites.
.venv/bin/python scripts/vps_reviewers.py prepare qa1 qa2
.venv/bin/python scripts/vps_reviewers.py start qa1 qa2
.venv/bin/python scripts/vps_reviewers.py gateway --public-tenant qa1 --include-qa

# After real preparation/approval/isolation/restart acceptance, create untouched
# official reviewer environments. Their invitations are retained privately.
.venv/bin/python scripts/vps_reviewers.py prepare judge1 judge2 judge3 judge4 judge5
.venv/bin/python scripts/vps_reviewers.py start judge1 judge2 judge3 judge4 judge5
.venv/bin/python scripts/vps_reviewers.py gateway --public-tenant judge1
```

The installer writes `/etc/forgeflow/reviewers.json` with private invitations
(mode 0600), per-instance `.json`, `.env` and `.backup.json` under
`/etc/forgeflow/instances`, and gateway configuration at
`/etc/forgeflow/gateway.json` (root:forgeflow-gateway, mode 0640). It validates
existing identity and credential bindings on retry and preserves them. Successful
initialization changes `allow_initialize` to false. Judge services are enabled at
boot; QA services are not. Never copy the private index or environment files into
the public repository or terminal output.

Judge APIs use loopback ports 8101–8105 and Odoo uses 18101–18105. QA uses
8106/8107 and 18106/18107. Runtime directories such as `/run/forgeflow-judge1`
are independent of the historical service's `/run/forgeflow` lifecycle.

Install the gateway unit after the installer has created its restricted Unix
account and configuration. The gateway uses the same Python environment as the
API, including FastAPI, Uvicorn and httpx:

```sh
install -o root -g root -m 0644 deploy/vps/forgeflow-judge-gateway.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now forgeflow-judge-gateway
```

After writing a new gateway configuration, restart that unit so it reloads the
fixed backend map. Validate it over loopback before changing public routing.
Adapt `nginx-reviewers.conf` to the installation's actual HTTPS certificate and
host. Back up the active Nginx site configuration, replace its upstream with
`127.0.0.1:8090`, run `nginx -t`, then reload Nginx. Preserve the HTTP ACME
challenge route. A failed validation must retain the previous working site.
Do not expose any reviewer API, Odoo, or the gateway port directly to the Internet.

The operator can verify all five invitations without creating cases or writing
ERP records:

```sh
.venv/bin/python scripts/vps_gateway_acceptance.py \
  --base-url https://YOUR_HOST --names judge1 judge2 judge3 judge4 judge5 \
  --fresh --output /root/forgeflow-reviewer-access.json
```

`--fresh` requires empty case lists and unclaimed generations. Omit it after
reviewers have begun work. The check also verifies origin restrictions, secure
cookies, signed routing, mixed-session rejection and logout. It intentionally
uses ten login attempts; wait for the one-minute login limit before repeating.

Each Odoo container initially has a 1 GiB memory budget and one CPU; each
API/executor/assistant service initially has a 512 MiB memory budget and one CPU.
These are resource caps, not reservations or a claim that five simultaneous
heavy operations fit in RAM. Measure `MemoryPeak`, Docker usage, host available
RAM, latency, and failed/OOM states during real workflows before enabling a new
capacity claim. The expected initial use is a few reviewers at different times,
with occasional overlap. No dedicated GPU is required for remote model inference.

## Maintenance and backups

For an application update, follow the [case Q&A release and acceptance guide](../../docs/case-qa-release.md).
Instances share the application checkout and Python installation: stop every
service using those paths before replacing backend code or frontend assets.
Data isolation alone does not make an in-place code update instance-local.

Operate only the intended service, for example
`systemctl restart forgeflow-runtime@judge1`. A normal restart preserves the
database, generation, ownership and evidence; it does not reset the scenario.
Never start an additional API worker against one instance.

Back up an instance only after stopping its service and its Odoo container. Use
its matching `/etc/forgeflow/instances/judge1.backup.json` with `vps_backup.py
create`. The backup reads the matched runtime configuration to acquire that
instance's lifecycle locks and verify that its writer unit is stopped. Another
reviewer's service can keep running. Backup keys stay outside all archives.

An explicit fresh-scenario reset is still an offline administrator operation
using `vps_rotate.py --config /etc/forgeflow/instances/judge1.json`. It preserves
the prior container, volume and case evidence, and requires an already stopped
service/container. Update the corresponding backup bindings after rotation.
No periodic cleanup or automatic lease timeout may destroy a reviewer's work.
