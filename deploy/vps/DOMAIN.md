# Add forgeflowagent.com while retaining the IP entry point

This operator procedure adds and maintains the domain entry alongside the
existing IP entry. Deployment status belongs in dated verification records;
the configuration examples here do not establish that every check has passed.
The domain's Alibaba Cloud DNS configuration is:

| DNS name | Type | Value | TTL |
| --- | --- | --- | --- |
| `@` | `A` | `168.144.242.231` | 600 seconds |

Record certificate issuance, routing, authenticated access and renewal checks
before changing the public entry link. This procedure covers the apex domain;
it does not include `www` or a wildcard certificate.

## Routing

The existing IP entry uses the `forgeflow-ip` certificate and the invitation
gateway described in [REVIEWERS.md](REVIEWERS.md). The named virtual hosts in
[nginx-domain.conf](nginx-domain.conf) use the same Nginx instance:

```text
https://168.144.242.231/   — forgeflow-ip certificate     —┐
                                                       ├─ Nginx → 127.0.0.1:8090 gateway
https://forgeflowagent.com/ — forgeflow-domain certificate ┘          → existing fixed reviewer APIs
```

Keep the IP virtual host, IP certificate lineage and its existing
`forgeflow-cert-renew.timer`. Both HTTPS entries use the same gateway, invitations
and persistent environments. Do not redirect the IP entry to the domain during
this change. Reviewer APIs and Odoo remain bound to loopback.

## DNS and HTTP challenge preparation

Confirm the apex A record from authoritative DNS and public resolvers before
requesting a certificate. Check for an unintended AAAA record; any published
address must actually serve the challenge. Domain registration alone does not
establish DNS readiness.

The existing default HTTP virtual host in
[nginx-reviewers.conf](nginx-reviewers.conf) serves the ACME challenge location
for unmatched Host names, including the domain before its named virtual host is
installed. Once DNS points to this server, that route supports initial issuance
without a temporary HTTP configuration. The final
[nginx-domain.conf](nginx-domain.conf) includes its own named HTTP challenge route
and redirects ordinary domain requests to HTTPS.

Put a temporary non-secret probe in
`/var/www/html/.well-known/acme-challenge/`, confirm its exact contents through
the domain's HTTP URL, then remove that probe. Preserve this location for future
renewals, including when normal application paths redirect to HTTPS. Validate
Nginx configuration changes with `nginx -t` before reloading.

HTTP-01 validation retrieves a challenge over **port 80**. With working DNS and
this reachable webroot, issuance and renewal can run automatically without an
Alibaba Cloud AccessKey or DNS plugin. Registration/DNS control is only needed
to establish the address record. See [Let's Encrypt HTTP-01](https://letsencrypt.org/docs/challenge-types/#http-01-challenge).

## Separate certificate and renewal timer

Reuse the server's existing Certbot installation and configured ACME account.
Issue a separate domain certificate; do not replace or expand `forgeflow-ip`:

```sh
/opt/forgeflow-certbot/bin/certbot certonly \
  --webroot -w /var/www/html \
  --cert-name forgeflow-domain -d forgeflowagent.com \
  --non-interactive
```

This uses the normal domain issuance path, without the IP certificate's
`--ip-address` or short-lived profile options. If ACME account setup is needed,
complete it separately with the operator's chosen contact details.

After successful issuance and the consistent trusted-host update described below,
install [nginx-domain.conf](nginx-domain.conf) alongside the existing IP site,
validate Nginx and reload. It uses the `forgeflow-domain` certificate files under
`/etc/letsencrypt/live/forgeflow-domain/` and retains the existing proxy behavior:
upstream `127.0.0.1:8090`, the original `Host`, HTTPS forwarding, disabled response
buffering and 960-second streaming timeouts. The IP remains the separate
`default_server`; do not substitute it into the domain's forwarded Host or Origin.

Use the checked-in
[forgeflow-domain-cert-renew.service](forgeflow-domain-cert-renew.service) and
[forgeflow-domain-cert-renew.timer](forgeflow-domain-cert-renew.timer). The service
renews only `forgeflow-domain` and validates/reloads Nginx after successful
renewal. The persistent timer checks every six hours at 00:00, 06:00, 12:00 and
18:00 in the system's timezone, with up to 900 seconds of randomized delay.

After installing the units as root-owned files under `/etc/systemd/system`, run
`systemctl daemon-reload` and enable the new timer with
`systemctl enable --now forgeflow-domain-cert-renew.timer`. Keep both certificate
timers enabled. Test the domain renewal and reload hook explicitly:

```sh
/opt/forgeflow-certbot/bin/certbot renew \
  --cert-name forgeflow-domain --dry-run --run-deploy-hooks \
  --deploy-hook "/usr/sbin/nginx -t && /usr/bin/systemctl reload nginx"
systemctl list-timers forgeflow-domain-cert-renew.timer forgeflow-cert-renew.timer
```

Certbot's deploy hook runs after successful renewal; a dry run needs
`--run-deploy-hooks` to exercise it. A successful timer invocation alone may mean
the certificate was not yet due, so retain the dry-run result as well as timer
state. See the [Certbot renewal guide](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates).

## Append the hostname consistently

Before allowing application traffic through the new HTTPS entry, privately back
up the active Nginx and application configuration. Add the exact hostname
`forgeflowagent.com` while preserving the existing IP and any existing loopback
entries. Use no scheme, path or wildcard in the trusted-host lists.

| Existing configuration | Field to update |
| --- | --- |
| `/etc/forgeflow/gateway.json` | `trusted_hosts` |
| `/etc/forgeflow/reviewers.json` | `trusted_hosts` |
| `/etc/forgeflow/runtime.env` | `FORGEFLOW_TRUSTED_HOSTS` for the legacy runtime |
| Every retained `/etc/forgeflow/instances/*.env` | `FORGEFLOW_TRUSTED_HOSTS`, including inactive QA instances before their next start |

Apply these as narrow, atomic field edits, retaining the files' existing owner,
group and mode. Preserve gateway `routing_secret`, tenant IDs, ports, invitation
hashes and `public_tenant`, and preserve all invitations, session/model secrets,
runtime paths, active bundles and generations. Runtime JSON files do not need a
new trusted-host field: the supervisor passes this setting from the environment.

Do **not** rerun `vps_reviewers.py prepare` to perform this update. It is an
instance installer, does not update existing instance environments for this
purpose, and is not a domain migration tool. The gateway installer also guards
against changing an existing trusted-host binding; edit the existing bounded
configuration instead of regenerating credentials or routes.

The running gateway and APIs load these settings at startup. Drain active work,
then restart the existing gateway and active runtime services in a controlled
window so they all accept the same additional hostname. Leave inactive QA
services inactive. Do not initialize, rotate, reseed or clear any environment
lease. Compare generation, case/receipt and business-state fingerprints before
and after the restart, using the same preservation scope as the existing release
procedure.

## Login and verification

The existing invitation still selects the same backend and principal. Browser
cookies belong to their original host, and the gateway's signed route binds the
host as well as the session. A reviewer must **sign in again on the domain** with
the same invitation. Existing cases, approvals and ERP data stay in the same
workspace; a new hostname must not create a new environment or release its lease.

Before reporting the domain ready, record these checks:

1. DNS resolves to the intended server; the HTTP challenge probe succeeds.
2. TLS validates `forgeflowagent.com`, and `/api/health` and `/app/` succeed on both
   the domain and the retained IP entry. Compare the served entry assets.
3. Authenticate existing invitations through both hosts with
   [vps_gateway_acceptance.py](../../scripts/vps_gateway_acceptance.py), selecting
   the host with `--base-url`. Omit `--fresh` for preservation checks: users may
   already have cases. Verify the same backend generations and existing data.
4. Allow the gateway's login-rate window between runs: checking five invitations
   consumes ten login attempts. Keep cookies and invitations out of published
   output. No case creation or business approval is needed for a hostname change.
5. The domain renewal dry run and deploy hook pass, and both renewal timers have
   future triggers. If a model stream is checked, read an existing case and record
   that narrow result separately from business execution.

Record the origin and transport of external probes. A server-local request, a
proxy-assisted external request and a rendered browser session establish
different things; only claim the checks actually completed. Publish sanitized
DNS/TLS/status/hash and preservation results, never configuration files, cookies,
private keys or invitation values.

If a check fails, retain the working IP entry and its renewal service. Restore
the saved narrow configuration changes or disable only the new named virtual
host, validate Nginx, and reload. If restoring `gateway.json` or runtime `.env`
files, also restart the gateway and affected active runtime services so the
restored values take effect; leave stopped QA services stopped. Keep the new certificate lineage and failed
check evidence for diagnosis; do not reset business state to repair routing.
