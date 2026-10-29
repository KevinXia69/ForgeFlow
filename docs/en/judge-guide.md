# Judge walkthrough

Open [ForgeFlow](https://168.144.242.231/app/) in a current desktop browser.
Enter the private invite supplied in the competition's testing instructions and
select **Enter workbench**. Judges do not need a model account, API key, payment,
software installation, or access to the operator's computer.

## Your workspace

Each of the five invitations selects a separate persistent workspace. Use one
invitation per judge and retain the same invitation on return. Cases, approvals,
ERP records and receipts stay with that workspace across sign-out and restart.
Another judge can use a different invitation without waiting for yours to be
released. Invitations are credentials: do not publish them in repository issues.

The hosted workflow supports Scenario A, a supplier cancellation in an Odoo
sandbox. Its business records are synthetic ERP-Bench benchmark data. Approved
operations make real writes to that sandbox; they do not order goods or alter a
customer's production ERP. The assistant uses NVIDIA Nemotron through Nebius
Token Factory. Constraint planning uses CP-SAT, and execution requires your
explicit approval of the displayed plan.

## Read the quantities and margin correctly

The A case covers 5 orders and 50 units due in 8–10 days. One failed 3-unit
finished-goods purchase is directly linked to Alpine Theater's 13-unit order,
due in 10 days. The case total is not the failed supply quantity.

Builds using `new_spend_coverage_v2` show new-spend margin on purchased/manufactured
coverage revenue, excluding finished stock. For the unchanged A plan this is
76.7498%, using $20,623.23 revenue and $4,794.94 planned cost. A legacy margin
label means the old value has not been corrected or newly verified. Historical
receipts remain original; new source changes need separate deployment acceptance.

Cost/capacity alternatives change the original preservation priority. The
selected objective appears with the approval scope; original verification still
scores the repair task. A feasible alternative need not obtain full marks.
Document actions and verified execution steps are shown separately, and neither
is a count of database writes. See [current capabilities](current-capabilities.md).

## Try a complete case

1. Open **New case** and select the supported supplier-cancellation scenario.
   Wait for preparation to finish. Inspect the affected orders, proposed
   changes, costs, delivery dates and policy checks.
2. Ask the assistant: **Which records does this plan change, and has it been
   executed yet?** Its response should refer to this case's evidence. A proposed
   plan or an assistant answer is not an execution receipt.
3. Review the exact plan and the approval controls. Acknowledge the displayed
   scope and risks, enter a short decision note, and select **Approve & execute**
   when ready. If a check blocks approval, read its reason; do not bypass it.
4. Wait for the result. Inspect the actual writes, ERP readback and original
   verifier outcome. Download the receipt to retain the evidence.
5. Sign out and sign back in with the same invitation. Reopen the case and check
   that its result and history remain available.

The previously recorded Scenario A acceptance passed 51 of 51 rules and scored
100/100. That is a result for the documented case and data profile, not a promise
that every future decision will pass. A failed or unknown result must remain
visible as such. See the [acceptance record and its limits](../evidence/2026-10-25-vps/README.md).

## Returning, retries and fresh scenarios

A recorded two-workspace check took about 24 seconds to prepare each case,
13 seconds for each model answer, and 5 minutes 27 seconds for approval through
ERP execution and original verification. These are measured examples, not
latency guarantees. Keep the case open and allow its
status to settle; refreshing the page does not grant a new approval. A repeated
approval for an already completed execution should return the stored result.
Do not treat a disconnected browser as proof that a business operation failed.

Your ERP intentionally remains changed after a successful execution. Signing
out does not restore its original seed. Revisiting history and asking about the
result remain available. A completely fresh baseline for the same invitation
currently requires an operator rotation; that operation preserves old evidence
and does not interrupt the other four workspaces. A different judge's initial
access never depends on this reset.

The intended capacity is five assigned reviewers using the application at
different times. Two disposable workspaces completed concurrent preparation,
model and approval stages; the five official workspaces were checked separately
without consuming their fresh scenarios. See the [reviewer acceptance record](../evidence/2026-10-26-reviewers/README.md).
This is a single-host demonstration,
not a claim of unlimited parallel heavy workloads or high availability. The
operator must maintain the application, HTTPS renewal and model access through
the end of the judging period. Availability checks and measured capacity belong in
the deployment evidence, not in an unsupported uptime guarantee.

For self-hosting, use the [repository setup instructions](../../README.md#run-the-product-locally)
and [VPS guide](../../deploy/vps/README.md). Runtime secrets and private invitations
are deliberately excluded from the public source.

## Current local validation materials

For the corrected source revision, see the [local UI screenshots](../media/next-phase/README.md) and [release evidence](../NEXT_PHASE_RELEASE.md). Local sandbox results have their own source commit and plan identities. The hosted workflow above needs its own deployment verification before those results can describe the hosted version.
