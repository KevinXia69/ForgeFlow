import type { Translate } from "./i18n";
import type { CaseFile, ProductDisruption, ProductOrder } from "./types";

/**
 * The scenario-A order snapshot used for the labelled demo case when product
 * execution is unavailable. It mirrors the live scenario-A facts (50 units
 * across five sales orders, Stonewall Components, 8-day due date) so the
 * decision panel is a realistic preview rather than placeholder copy.
 *
 * Nothing in this module talks to the API, and the fixture is never submitted
 * for approval: the demo case is read-only by construction.
 */
export const PREVIEW_ORDER: ProductOrder = {
  ref: "SO-1..5",
  product: "Clean Room Sealed Fixture",
  units: 50,
  due_days: 8,
  customer: "5 confirmed sales orders",
};

export const PREVIEW_DISRUPTION: ProductDisruption = {
  kind: "supplier_cancellation",
  vendor: "Stonewall Components",
};

// Fixed identifiers and hashes keep the fixture byte-stable between renders.
const FIXTURE_CASE_ID = "274c4b9089cc74cc1c01401732d91202";
const FIXTURE_PLAN_ID =
  "d31ec8f2bfcec2c85029e51d85f76ef2d5a68db942da63bb1af37741c908d13d";
const FIXTURE_POLICY_SHA256 =
  "cc9f46348e7265b191647fc672ec8b62ae0ce8e9023923e756fab6aade4d56de";
const FIXTURE_SNAPSHOT_SHA256 =
  "779ea56a2690a65c7c3f84865a33fa908635a8474ebb6efa1ac69c2689b6618e";
const FIXTURE_STAMP = "2026-10-21T23:00:00+00:00";

/**
 * Build the explicit, labelled synthetic case shown when
 * ``product_approval`` is false (or the cases API is unavailable).
 *
 * The fixture carries ``new_spend`` and ``checks`` exactly like the intended
 * product contract, so the decision UI can be reviewed without a backend.
 * The legacy new_spend key is planned purchase/manufacturing cost, not an increment.
 */
export function buildFixtureCase(t: Translate): CaseFile {
  return {
    case_id: FIXTURE_CASE_ID,
    scenario: "A",
    state: "awaiting_approval",
    synthetic: true,
    order: { ...PREVIEW_ORDER },
    disruption: { ...PREVIEW_DISRUPTION },
    plan: {
      plan_id: FIXTURE_PLAN_ID,
      actions: [
        {
          kind: "cancel_po",
          document: "P00006",
          summary: t("case.fixtureActionCancel", {
            vendor: PREVIEW_DISRUPTION.vendor || "—",
          }),
          quantity: 3,
        },
        {
          kind: "create_po",
          document: "new PO",
          summary: t("case.fixtureActionCreate"),
          quantity: 3,
        },
      ],
      margin: 80.0,
      on_time: true,
      new_spend: 4794.94,
      checks: [
        {
          code: "demand",
          label: t("decision.checkDemand"),
          status: "pass",
          actual: `${PREVIEW_ORDER.units} ${t("cases.units")} · 5`,
          threshold: `${PREVIEW_ORDER.units} ${t("cases.units")}`,
          source: t("case.checkSourceCase"),
        },
        {
          code: "margin",
          label: t("decision.checkMargin"),
          status: "pass",
          actual: "80.0%",
          threshold: "27.5%",
          source: t("case.checkSourcePolicy"),
        },
        {
          code: "budget",
          label: t("decision.checkBudget"),
          status: "pass",
          actual: "$4,794.94",
          threshold: "$6,828.00",
          source: t("case.checkSourceContract"),
        },
        {
          code: "date",
          label: t("decision.checkDate"),
          status: "planned",
          actual: t("case.days", { count: PREVIEW_ORDER.due_days }),
          threshold: t("case.days", { count: PREVIEW_ORDER.due_days }),
          source: t("case.checkSourcePlan"),
        },
      ],
      cost_delta: 4794.94,
      policy_sha256: FIXTURE_POLICY_SHA256,
      snapshot_sha256: FIXTURE_SNAPSHOT_SHA256,
      prepared_at: FIXTURE_STAMP,
    },
    approval: null,
    execution: null,
    created_at: FIXTURE_STAMP,
    updated_at: FIXTURE_STAMP,
    reason: "synthetic_demo",
  };
}
