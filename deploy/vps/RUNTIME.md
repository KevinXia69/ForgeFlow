# Host runtime installation

The host must have Python 3.12, Node.js 24, Docker, sudo, and a root-owned checkout
at `/opt/forgeflow`. The application virtual environment is
`/opt/forgeflow/.venv`. Install the Python project with its `planning` and
`dsh-sdk` extras and `deploy/vps/requirements.txt`. The current project pins the Python DSH SDK
to `0.2.1rc2`; the matching official npm package is **`@deepseek-ai/dsh`**, pinned
here to `0.2.1-rc.2`. Do not install the unrelated unscoped package.

Run these installation commands as root after building the frontend and Odoo
image. They do not initialize or reset business data.

```sh
install -d -m 0755 /etc/forgeflow /var/lib/forgeflow /opt/forgeflow/dsh
id forgeflow-api >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin forgeflow-api
id forgeflow-dsh >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin forgeflow-dsh
install -m 0644 deploy/vps/dsh/package.json /opt/forgeflow/dsh/package.json
install -m 0644 deploy/vps/dsh/package-lock.json /opt/forgeflow/dsh/package-lock.json
npm ci --prefix /opt/forgeflow/dsh --omit=dev --no-audit --no-fund
install -m 0755 deploy/vps/forgeflow-dsh-node /usr/local/bin/forgeflow-dsh-node
visudo -cf deploy/vps/forgeflow-dsh.sudoers
install -m 0440 deploy/vps/forgeflow-dsh.sudoers /etc/sudoers.d/forgeflow-dsh
install -m 0644 deploy/vps/forgeflow-runtime.service /etc/systemd/system/forgeflow-runtime.service
systemctl daemon-reload
```

Keep `/etc/forgeflow/runtime.env` root-owned with mode `0600`. It holds the
existing session secret, reviewer invite codes, exact `FORGEFLOW_TRUSTED_HOSTS`
and model endpoint/name/key. Never include its contents in deployment logs or
Git. `/etc/forgeflow/runtime.json` is root-owned and may contain only nonsecret
paths and instance configuration. The supervisor generates the public DSH
composition and low-privilege launch configuration from these trusted inputs.

The supervisor invokes `scripts/vps_provision.py --config ...` to preserve or
explicitly initialize the sandbox, then starts the executor as root and the API
as `forgeflow-api`. Only the API UID passes the Unix peer gate. The API's model
process enters `forgeflow-dsh` via the fixed launcher; it receives no session
secret, invite codes, ERP key, Docker environment or executor socket setting.
The DSH SDK internally merges inherited variables, so the final sudo/launcher
environment reset is required even when the Python adapter uses a whitelist.
The service allows the root supervisor to set group-inheritance bits on the
shared evidence directories. `RestrictSUIDSGID=true` is incompatible with that
layout and causes `chmod` to fail before the API starts; the explicit UID split,
fixed sudo entry point and remaining service sandbox restrictions stay enabled.

The root sandbox remains mode `0700`; the API reads a copy of public request
facts at `/run/forgeflow/api-config/request.json` and fetches specific immutable
plan artifacts through the peer-gated RPC. Scoped assistant snapshots reside in
the separate setgid evidence directory and are read-only to the assistant UID.
Root case stores, grants and ERP credentials are never shared with that UID.
The supervisor holds `.runtime/lifecycle/workbench.lock` throughout startup and
child execution, excluding concurrent backup, environment rotation or a second
supervisor. Stop this service and the Odoo container before the documented backup.
The backup starts a temporary PostgreSQL-only container while Odoo remains stopped,
so the database, filestore and approval evidence describe the same stopped state.

The pinned DSH subprocess package's reviewed postinstall only restores the
executable mode on node-pty's packaged spawn helper. Its exact version is allowed
in package.json for npm's install-script policy. Other unapproved lifecycle
scripts remain disabled; the Linux x64 Koffi and node-pty prebuilds have been
checked to load without them.

Verify `sudo -l -U forgeflow-api`, both process UIDs, socket ownership/mode,
absence of Docker group membership, a real model turn with scoped MCP tools,
and an approval/execution/original-verifier cycle before allowing reviewers.
Use `systemctl enable --now forgeflow-runtime` only after the provisioner image,
configuration, persistence and HTTPS reverse proxy are ready. A child exit
terminates its sibling and triggers a bounded systemd restart; stopping waits
for an in-flight executor operation instead of killing it at the API timeout.

The Odoo container must use Docker restart policy `no`. The supervisor checks
its process state and boot identity every ten seconds with a five-second command
timeout. A stopped, restarted, replaced or uninspectable container stops the API
and executor. Systemd then re-enters the provisioner, which verifies the volume,
generation, planner provenance and task/request bindings before starting it.
An existing container with a different restart policy is rejected, never silently
updated. Missing metadata, incomplete initialization or a damaged ready volume
requires administrator diagnosis; restart never authorizes a new seed. Preserve
failed generations and use the explicit administrator rotation for a new one.
