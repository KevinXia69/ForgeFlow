/** Invented candidate records for browser interaction checks; no ERP/model. */
import { initialCase } from "./revision-fixture.mjs";
export const IDS = ["a".repeat(64), "b".repeat(64), "c".repeat(64)];
export function candidateCase({ single = false, selected = null, capacity = false } = {}) {
  const value = initialCase();
  const plans = [62, 50].map((amount, index) => ({ ...structuredClone(value.plan),
    plan_id: IDS[index], objective: index ? "min_planned_cost" : "preserve_quantities",
    new_spend: amount, actions: [{ kind: "update_po", document: index ? "PO-COST" : "PO-QUANTITY",
      quantity: index ? 8 : 10, summary: "Invented candidate for browser checks", before: null, after: null }],
    costs: { schema_version: 1, basis: { snapshot_sha256: "e".repeat(64), pricing_sha256: "f".repeat(64),
      as_of: "2026-10-23T12:00:00Z", anchor_date: "2026-10-23", currency: "USD", company_id: 1,
      sales_order_ids: [1], target_product_code: "FIXTURE", tax_basis: "unknown", manufacturing_basis: "public_standard", unit_basis: "unchanged" },
      lines: [], baseline: { purchase: "40", manufacturing: "0", total: "40", known_subtotal: "40" },
      planned: { purchase: String(amount), manufacturing: "0", total: String(amount), known_subtotal: String(amount) },
      comparable: true, known_delta: String(amount - 40), new_purchase_value: "0", purchase_increase: String(amount - 40),
      new_manufacturing_standard: "0", full_delta: null, actual_cost: null, missing: ["actual_financial_cost"] } }));
  if (capacity) {
    const third = structuredClone(plans[1]);
    third.plan_id = IDS[2]; third.objective = "preserve_capacity";
    third.new_spend = 90; third.costs.planned.total = "90";
    third.costs.planned.purchase = "90"; third.costs.planned.known_subtotal = "90";
    third.costs.known_delta = "50"; third.costs.purchase_increase = "50";
    third.actions[0].document = "PO-CAPACITY";
    plans.push(third);
    plans.forEach((plan, index) => { plan.capacity = {
      schema_version: 1, basis: { plan_id: plan.plan_id, snapshot_sha256: plan.snapshot_sha256,
        quantity_scope: "target_product", time_basis: "standard_unit_minutes" },
      manufactured_units: 10, finished_purchase_units: 0, standard_minutes: index === 2 ? 10 : 30,
      workcenters: [{ code: index === 2 ? "WC-D" : "WC-B", standard_minutes: index === 2 ? 10 : 30 }], missing: [],
    }; });
  }
  if (single) selected = 0;
  value.plan = selected === null ? null : plans[selected];
  value.state = selected === null ? "awaiting_selection" : "awaiting_approval";
  value.comparison = { status: "ready", comparison_id: "d".repeat(32), version: 1, selection_revision: 1,
    selected_plan_id: value.plan?.plan_id ?? null, plans: single ? plans.slice(0, 1) : plans,
    attempts: plans.map((plan, index) => ({ objective: plan.objective, status: single && index ? "duplicate" : "prepared" })) };
  value.decision = { ...value.decision, can_approve: selected !== null, can_request_changes: selected !== null,
    can_select_candidate: !single, can_resume_selection: false, selection_pending: false };
  value.result.outcome = value.state;
  return value;
}

export function wireCandidateFixture(fixture) {
  fixture.selectionMode = "normal";
  fixture.onSelection = body => {
    const current = fixture.current;
    const comparison = current.comparison;
    const pending = current.decision.selection_pending;
    const request = pending ? current.selection_request : {
      comparison_id: comparison.comparison_id, selection_revision: comparison.selection_revision, plan_id: body.plan_id,
    };
    if (Object.keys(body).sort().join() !== "comparison_id,plan_id,selection_revision"
        || Object.keys(body).some(key => body[key] !== request[key])
        || !comparison.plans.some(plan => plan.plan_id === body.plan_id)) return { status: 409, body: { detail: "selection_conflict" } };
    if (fixture.selectionMode === "conflict") {
      current.state = "failed"; current.reason = "product_selection_conflict";
      current.result.outcome = "failed";
      for (const key of Object.keys(current.decision)) if (key.startsWith("can_")) current.decision[key] = false;
      return { body: structuredClone(current) };
    }
    if (!pending && ["pending", "refresh_lost"].includes(fixture.selectionMode)) {
      current.selection_request = { ...body, requested_at: current.updated_at, error: "unavailable" };
      current.decision = { ...current.decision, selection_pending: true, can_resume_selection: true,
        can_select_candidate: false, can_approve: false, can_request_changes: false, can_reject: false };
      if (fixture.selectionMode === "refresh_lost") fixture.detailOffline = true;
      return { status: 503, body: { detail: "controlled_reply_loss" } };
    }
    current.plan = comparison.plans.find(plan => plan.plan_id === body.plan_id);
    comparison.selected_plan_id = body.plan_id; comparison.selection_revision++;
    current.state = "awaiting_approval"; current.result.outcome = current.state;
    current.selection_request = null;
    current.decision = { ...current.decision, selection_pending: false, can_resume_selection: false,
      can_select_candidate: true, can_approve: true, can_request_changes: true, can_reject: true };
    return fixture.selectionMode === "complete_then_lost"
      ? { status: 503, body: { detail: "controlled_reply_loss" } } : { body: structuredClone(current) };
  };
  fixture.onReject = body => {
    const current = fixture.current;
    if (!body.reason || body.plan_id !== (current.plan?.plan_id ?? current.comparison.plans[0].plan_id))
      return { status: 409, body: { detail: "rejection_scope_mismatch" } };
    current.state = "rejected"; current.result.outcome = "rejected";
    current.audit.push({ action: "rejected", comment: body.reason, actor_label: "Test operator", at: current.updated_at });
    for (const key of Object.keys(current.decision)) if (key.startsWith("can_")) current.decision[key] = false;
    return { body: structuredClone(current) };
  };
  fixture.onApproval = body => {
    if (body.plan_id !== fixture.current.plan?.plan_id) throw new Error("wrong selected plan");
    const current = fixture.current;
    current.state = "succeeded";
    current.approval = { plan_id: body.plan_id, comment: body.comment, phase: "confirmed", issued_at: current.updated_at, approver_label: "Test operator" };
    current.execution = { status: "applied", writes: [{ kind: "update_po", document: current.plan.actions[0].document }],
      readback: { execution_validated: true, records: [] },
      verifier: { source: "synthetic_executor", strict_success: true, evaluation_valid: true, score: 100, checks: [] } };
    current.result = { execution_status: "applied", readback_status: "passed", verifier_status: "passed",
      strict_success: true, evaluation_valid: true, verified: false, simulation_passed: true,
      verification_source: "synthetic_executor", outcome: "simulation_passed" };
    for (const key of Object.keys(current.decision)) if (key.startsWith("can_")) current.decision[key] = false;
    return structuredClone(current);
  };
}
