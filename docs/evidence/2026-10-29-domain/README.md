# Domain migration — October 29, 2026

The public entry is [forgeflowagent.com/app/](https://forgeflowagent.com/app/).
The original [IP entry](https://168.144.242.231/app/) remains available. Both
serve the existing invitation gateway and the same persistent environments.

This change adds DNS, a separate domain certificate and Nginx virtual host,
an independent renewal timer, and the hostname in eleven existing configuration
files. It also fixes the operator acceptance script to test the actual HTTPS
Host/Origin instead of the first configured trusted host. Application business
logic, invitations, session/routing secrets and environment bindings are retained.

| Check | Result | Evidence |
| --- | --- | --- |
| DNS | Both authoritative nameservers and two public resolvers return `168.144.242.231`; no AAAA record | [Deployment report](deployment-report.json) |
| Public HTTPS | Domain and IP health, root and workbench succeed; entry HTML, JavaScript and CSS match; HTTP redirects to the corresponding HTTPS host | [External probes](external-probes.json) |
| Browser | Chrome rendered the new domain's invitation screen | Observed through the operator's Chrome window; no browser login or business approval was performed |
| Five invitations on the domain | All five remain fresh, distinct and authenticated; cookie flags, origin rejection and routing isolation pass | [Domain authentication](domain-auth.json) |
| Five invitations on the IP | Same five generations, same isolation checks; run after the login-rate window | [IP authentication](ip-auth.json) |
| State preservation | Nine retained environments, 520 protected files and 48 business-table comparisons unchanged; all non-host configuration values preserved | [Preservation summary](preservation.json) |
| Certificate renewal | HTTP-01 issuance and renewal simulation pass, including the Nginx validation/reload hook; domain and IP timers both active | [Deployment report](deployment-report.json) |
| Acceptance-script regression | 20 targeted tests pass, including real authority selection, explicit ports and loopback compatibility | `tests/test_vps_gateway_acceptance.py` |

The domain certificate observed externally expires on January 27, 2027. Both
timers check every six hours with a randomized delay. The successful renewal
simulation exercises issuance and deployment, but does not claim a future
scheduled renewal has already occurred. No Alibaba Cloud API credential is used.

Authenticated gateway checks ran from the VPS through its public HTTPS URLs with
certificate verification enabled. Separate external HTTP checks ran on the Mac
through its existing HTTP CONNECT proxy, also with certificate verification.
The external checks cover anonymous delivery; authenticated browser interaction
and a new model/approval run are outside this hostname-change check.

The database comparisons cover eight business tables in each of the six running
environments. Three QA environments stayed stopped; their databases were not
queried, while their retained case/evidence files were included in the file
comparison. The comparison allows additional files and excludes volatile
PID/lock files, planner bytecode and activation records. Private snapshots and
configuration backups remain on the server; no credentials or ERP rows are
published here.

No business case, approval, ERP write or model call was made by these checks.
The previous [business execution and original acceptance results](../2026-10-29-public-upgrade/README.md)
retain their own source revision and scope. See [DOMAIN.md](../../../deploy/vps/DOMAIN.md)
for operation and rollback instructions.
