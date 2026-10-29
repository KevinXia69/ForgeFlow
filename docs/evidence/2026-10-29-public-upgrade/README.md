# Public deployment upgrade — October 29, 2026

The hosted workbench was upgraded to source
`a02a197319545f6c4859e12978c7953371aa6e67`. The [deployment report](deployment-report.json)
records the installed source, served assets, retained environments and final
service state. These are operator captures over synthetic ERP-Bench data,
with real Odoo execution and original task acceptance.

## Approval and verification chain

One new, isolated **qa3** environment exercised Scenario A through the public
HTTPS gateway. Existing judge workspaces and historical qa1/qa2 were preserved.

| Capture | What happened |
| --- | --- |
| [Initial execution check](qa3-execute/report.json), source `d69cab5` | Preparation succeeded, but the acceptance script incorrectly required a public candidate-objective field when candidates were disabled. It stopped before approval. The prepared case and pre-prepare table digests are retained. |
| [Continuation](qa3-resume/report.json), source `a02a197` | Continued that same unapproved case after fixing the two acceptance scripts. Business source, bundle and built assets stayed identical. The old baseline matched two newly captured pre-approval table digests; there was no reset or second case creation. |
| [Installed-production check](qa3-production/report.json) | After the production-directory restart, the same case, plan, generation, receipt and business state remained. Repeating approval was idempotent. This was a preservation/replay check, not another fresh execution. |
| [Production model check](qa3-production-model/report.json) | A real SSE answer returned `mode=harness` for the same case. Only the answer hash is published. Approval, execution and the eight measured business tables did not change. |

The original approval was issued by an explicitly authorized **HTTP acceptance
driver**, not by a human browser or an autonomous model. The completed case passed
**51/51 original applicable rules, score 100**, with **two business actions and
seven verified execution steps**. Those counts are not database-write counts.

The independently calculated plan cost was **USD 4,794.94**, including retained
purchases and manufacturing. **43** purchased/manufactured finished units covered
**USD 20,623.23** of sales revenue; **7** finished-stock units were excluded from
the denominator. The new-spend margin was **76.74981077%**, displayed as **76.75%**.
Candidates were disabled for this hosted acceptance; it does not demonstrate a
multi-candidate comparison or claim that optional `decision_metrics` fields were
returned by this single-plan API response.

The [receipt binding](qa3-resume/receipt-binding.json) compares HTTP hashes with
the persisted verification payload, plan, policy, snapshot, journal and actual
bundle provenance. The [original reward bytes](qa3-resume/original-reward.raw.json)
match the receipt's reward hash. Execution/replay/restart measurements cover
eight named business tables, not every Odoo table.

The first [production precheck](qa3-production-initial-precheck/precheck-failure.json)
failed authentication before business requests. A later login returned 201 and
the production check passed. Exhaustion of the login window by the preceding
five-judge check is recorded as a possible cause, not a confirmed diagnosis.

## Preserved deployment state

The [cutover event record](cutover-events.jsonl) and deployment report record:

- **9 encrypted backups verified**, retained on the server. No database restore
  drill was performed.
- **443 protected files across 8 retained instances** unchanged, plus **48 table
  comparisons across the 6 running original instances**. Three older historical
  sandbox roots retained **176 files** unchanged.
- All five judge environments remained fresh with their original generations;
  compare [before](judges-before.json) and [after](judges-after.json). Original
  invitations, secrets and gateway routes were retained.
- The production frontend's **134 built assets** matched the QA assets. Source
  and frontend archive identities are recorded in the [release package](release-package.json).
  This is the final `a02a197` package generated and hash-checked on the server,
  after the acceptance-script fixes; it is not the initial `d69cab5` upload.
- qa3 was stopped after verification. Its database, case and receipt were kept,
  and its temporary gateway route was removed. Existing judge routes remain.

These preservation and backup totals are operator-reported checks; private
archives, credentials, ownership material and filesystem snapshots are not
published here. The earlier [local A/B acceptance](../2026-10-28-next-phase/README.md)
remains a separate record on source `bf880f2`.

## Access and test boundaries

Server-side public TLS, health and served-entry-asset checks passed. An
[external Mac probe through an existing HTTP CONNECT proxy](external-probe.json)
also returned health/page HTTP 200 with TLS verification; its HTML hash matched
the production gateway capture. A direct connection from that network timed out.
The in-app browser did not complete its page load, so this release does not claim
a browser rendering or visual acceptance test, or direct reachability from all
networks.

The full backend regression run preceded the final acceptance-script corrections.
Focused Linux upgrade, final acceptance and private continuation regressions ran
afterward. Their different scopes do not replace the real HTTPS approval and ERP
evidence above.

## Evidence integrity and operator drivers

[manifest.json](manifest.json) hashes every published file except itself. Nested
manifests retain their original scope, and the failed run's files remain intact.
The continuation records the exact hashes of the earlier failed report and
prepared case. Source records match their Git commits and distinguish copied
source from the builder's three deliberately empty package markers.

The captured [continuation driver](operators/resume_qa3.py) and
[production preservation driver](operators/verify_production_qa3.py) are retained
for inspection, including the standard server paths used during execution.
They contain no credentials. The continuation driver's hash is bound in its
report; the production driver's bytes are covered by the publication manifest.
They are historical operator scripts, not general installation commands. Hashes
identify captured bytes and source relationships; they are not independent
attestation.
