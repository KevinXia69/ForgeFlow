# VPS acceptance record — 2026-10-25

This is a redacted operator capture from the deployed Linux VPS, separate from
the older fixture replayed by the frontend tests. It binds the deployment to
source commit `67bcb95e3253c245dab49478458aab652af8d05a`, immutable Odoo image
`sha256:df04859629c5fade4094698b5547984d7dcd674eabce5348365fd597fcc891fa`,
and case `a9d6a8a859cfc24d888ed540a732fece`.

The data is synthetic ERP-Bench Scenario A
(`2280_medium_repair_plan_medium`) with the recorded `planning-metadata-v1`
derived visibility profile. The execution used a real Odoo sandbox and the
original verifier. This is not customer production data, the original
unmodified data-visibility condition, or a fake-executor result.

## What the retained records establish

| Stage | Recorded observation | Published evidence |
| --- | --- | --- |
| Before approval | The case was `awaiting_approval`; approval and execution were `null`. The snapshot and policy digests identify the prepared plan. | [Live acceptance](live-acceptance.json), `pre_approval` |
| Approval and write | The prepared and approved plan IDs match. The case reached `succeeded`; the two recorded changes cancel P00006 and create a replacement purchase order. | [Live acceptance](live-acceptance.json), `approval`, `audit`, `execution` |
| ERP readback | Two document readbacks, seven verified execution steps, with receipt and readback SHA256 values. | [Live acceptance](live-acceptance.json), `execution.readback` |
| Original acceptance | Valid original verifier result: 51/51 rules passed, zero failed, score 100, `strict_success=true`. | [Unmodified original reward bytes](original-reward.json), linked from the live receipt by SHA256 |
| Duplicate approval | The operator driver recorded successful assertions for HTTP 200, an identical execution receipt, and equal ERP counts before/after the repeated request. | [Live acceptance](live-acceptance.json), `duplicate_approval` |
| Isolated restore | Logical PostgreSQL restore, generation binding, Odoo API-key authentication, and one filestore attachment passed. A fresh verifier attempt also passed 51 rules with score 100. | [Recovery acceptance](recovery-acceptance.json) |
| Reviewer rotation | The actual offline rotation committed a new generation while retaining the case root and disabling ordinary-start initialization. | [Rotation acceptance](rotation-acceptance.json) |
| Complete OS reboot | Boot ID changed; generation, four state-file hashes, ERP counts, and the historical execution remained equal. The API could log in and read that case; the fresh generation remained unclaimed. | [Reboot acceptance](reboot-acceptance.json) |

The case approval occurred at 10:21 and its successful execution receipt was
recorded at 10:26 on October 25, 2026, America/Los_Angeles. JSON timestamps retain
their original timezone information. Recovery and reboot occurred later the same
day. They are separate tests, not one continuous new approval after a restore.

## Boundaries that remain explicit

- The preparation capture contains no execution record, but no independent ERP
  before/after snapshot was retained for the preparation-only interval. It does
  **not independently prove zero database writes before approval**.
- The duplicate-approval driver retained its comparison result and counts before
  the duplicate request (`7/7/4/12` for purchase orders, purchase-order lines,
  manufacturing orders, and stock pickings). A second standalone after-count
  snapshot was not retained. Equal counts also do not alone prove that every
  possible field stayed unchanged.
- Isolated recovery started PostgreSQL and Odoo, **not the ForgeFlow public API
  or approval service**. Its receipt explicitly records
  `forgeflow_api_and_approval_service_restarted=false` and `public_ports=false`.
  This is not proof of an entire-site failover onto another VPS.
- The OS reboot test exercised the existing installation's normal startup,
  login, capability checks, historical case read, and preserved state. It did **not**
  create and approve a new case after reboot. That end-to-end claim remains
  untested by this record.
- A new reviewer generation was deliberately left empty; the successful earlier
  case belongs to the retained previous generation. A reboot of an empty new
  generation must not be described as a new successful business execution.
- These observations validate this source/image/profile combination. A later
  commit does not inherit a new live acceptance merely because it includes these
  files. Multi-reviewer load and Scenario B are outside this record.

## Integrity and redaction

[manifest.json](manifest.json) lists SHA256 and byte sizes for each published
artifact. The JSON captures also record hashes of the original private capture
and operator driver where applicable. Hashes identify bytes; they are **not an
independent attestation or third-party audit**.

`original-reward.json` preserves the original raw bytes. The live and isolated
restore verifier outputs have the same digest
`23cf0e9703a482859312b078223077700ca1c83f365aed4ecf3673993b868d3c`;
their different attempt IDs are in the respective receipts. Identical
deterministic reward bytes alone do not establish that two executions happened;
that provenance comes from the separately retained operator captures.

The other JSON files are explicit allowlisted exports. Cloud account identifiers,
addresses, local client paths, credentials, invite/session values, approval
comments, actor labels, and private model prompts/answers are omitted. Only
synthetic benchmark business identifiers remain in the raw reward. The encrypted
archives and decryption key are not committed to the repository.

To verify the published bytes without contacting the live service:

```sh
python -c 'import hashlib,json,pathlib; p=pathlib.Path("docs/evidence/2026-10-25-vps"); m=json.loads((p/"manifest.json").read_text()); assert all((p/x["name"]).stat().st_size == x["bytes"] and hashlib.sha256((p/x["name"]).read_bytes()).hexdigest() == x["sha256"] for x in m["files"]); print("Published evidence digests match")'
```

The command verifies file integrity, not the truth of operator observations.
For controlled maintenance and future recovery tests, see
[the VPS runbook](../../../deploy/vps/README.md),
[rotation](../../../deploy/vps/ROTATION.md), and
[backup/recovery instructions](../../../deploy/vps/odoo-runtime.md).
