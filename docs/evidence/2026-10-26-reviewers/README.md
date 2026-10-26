# Independent reviewer deployment acceptance

Operator captures from October 25, 2026 Pacific time / October 26 UTC. These
records identify observed results and executed source bytes; they are not an
independent audit or a claim of continuous uptime.

## Two real disposable workspaces

[acceptance.json](acceptance.json) records two new, isolated Odoo/PostgreSQL
environments, `qa1` and `qa2`. Each has separate Unix users, API, assistant,
session secret, executor socket, case store and ERP generation. Each stage
prepared both cases concurrently, called the real Nebius/Nemotron assistant
concurrently, and approved both cases concurrently. No official judge invite
was consumed by these business tests.

| Result | QA1 | QA2 |
| --- | --- | --- |
| Case | `079279148c138f11d40704ac6a086208` | `247a684a65c7eb994990bbdd3ab2394e` |
| Preparation | 23.66 s | 23.68 s |
| Real assistant, `mode=harness` | 13.33 s | 13.18 s |
| Approval through execution and verification | 327.20 s | 327.05 s |
| Original verifier | 51/51, 100/100 | 51/51, 100/100 |
| Readback and strict success | Passed | Passed |

The exact original rewards are [QA1](qa1-original-reward.json) and
[QA2](qa2-original-reward.json); both hash to
`23cf0e9703a482859312b078223077700ca1c83f365aed4ecf3673993b868d3c`.
The data are synthetic Scenario A / task 2280 under `planning-metadata-v1`.
Writes were real Odoo sandbox operations, not customer production transactions.

Preparation left the twelve named business tables unchanged, measured by exact
row counts and ordered row-content MD5 hashes. Before approval, both cases had
no approval or execution receipt. Repeated approval returned the stored execution
and again left those twelve tables unchanged. This measurement does not cover
every Odoo table and does not assert that session/log infrastructure never writes.

Cross-case access returned 404; another instance's session returned 401. Unix
file access isolation passed. Service restarts preserved cases, execution
results, generations, ownership and the measured business rows. The QA services
and containers were then stopped with their evidence retained.

## Resource observations

The host has four vCPUs and 8 GiB RAM. Each Odoo container has a one-CPU / 1 GiB
cap; each API/executor/assistant service has a one-CPU / 512 MiB cap. These are
limits, not reserved allocations. During the two-workspace test, the lowest
sampled host available memory was **5,231.5 MiB**. The largest recorded service
peak was **372.9 MiB**, and Odoo cgroup peak **578.1 MiB**. Cgroup peaks include
earlier initialization. The sampled services had no restarts and there were no
sampling errors. Memory fields in `acceptance.json` are bytes.

This validates two disposable workflows in overlap. Five official environments
were initialized and checked separately; the record does not claim five heavy
operations at once or a prolonged load test. The intended use is five assigned
judges working mainly at different times. There is no measured reason here to
upgrade the existing VPS for that use.

## Public access and release checks

[public-access.json](public-access.json) checks all five official invites through
the same verified HTTPS address. Every invite selects a distinct generation,
an available unclaimed workspace, an empty case list and a live executor.
The check covers secure cookies, failed login preservation, Origin enforcement,
missing/forged route rejection, mixed-instance cookie rejection and logout.
[gateway-loopback.json](gateway-loopback.json) records the preceding gateway check.
Neither check creates a case, approves work or calls a model.

The gateway exposes only `legacy` and `judge1`–`judge5`; QA environments are not
publicly routed. Each formal environment has initialization disabled and is
enabled to start at boot. Historical evidence remains reachable only through
its separate internal invitation.

The [public model stream](public-model-stream.json) used the historical
acceptance case through verified HTTPS and the new gateway. It returned a
nonempty real harness answer in 15.33 seconds without changing that execution
or consuming a formal workspace. The receipt records one final SSE event;
it does not establish incremental token delivery or a first-token latency.

[host-reboot.json](host-reboot.json) confirms a different OS boot ID, automatic
startup of all six runtime services, the gateway, Nginx and renewal timer, and
unchanged generations, ownership and persistent case fingerprints. The
fingerprint scope is included in the record; time-dependent decision flags
are excluded. [post-reboot-access.json](post-reboot-access.json) repeats all five
public HTTPS checks after reboot. No new business approval was requested after
that reboot.

[runtime-identities.json](runtime-identities.json) records ten distinct Unix
identities, private configuration permissions, sockets, image IDs and five
distinct ready generations, with initialization disabled. Its read-only driver
is [included](runtime-identities-driver.py). The [release manifest](release-manifest.json)
binds these additional captures and the gateway/release drivers to their SHA-256
hashes. The QA service-restart result and whole-host reboot remain separate tests.

## Integrity and boundaries

[manifest.json](manifest.json) binds the original QA evidence and execution
drivers. Invitations, cookies, actor identifiers, API credentials, model prompts
and answers are excluded. Source hashes identify the bytes used on Linux;
they are not a claim that a later documentation commit was already executed.

The initial [single-instance acceptance](../2026-10-25-vps/README.md) and
[local bootstrap validation](../2026-10-25-local-bootstrap/README.md) remain
separate. Full recovery onto another VPS, new approval after an OS reboot,
Scenario B runtime coverage, five-way heavy concurrency and long-term uptime
are not established by this record.
