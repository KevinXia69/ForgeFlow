export type RunSummary = {
  run_id: string;
  started_at?: string | null;
  agent?: { name?: string; category?: string };
  experiment?: RunDetail["experiment"];
  task?: string | null;
  task_label?: string | null;
  score?: number | null;
  strict_success?: boolean | null;
  evaluation_valid?: boolean | null;
  security_profile?: string | null;
  state?: string | null;
};

export type RunDetail = {
  run_id: string;
  task?: { id?: string | null; label?: string | null } | null;
  state?: string | null;
  evaluation?: {
    score?: number | null;
    evaluation_valid?: boolean | null;
    strict_success?: boolean | null;
    passed_rule_count?: number | null;
    applicable_rule_count?: number | null;
    failed_rule_count?: number | null;
    rule_results?: { dimension: string | null; rule: string | null; status: "PASS" | "FAIL" | "NA"; applicable: boolean }[] | null;
  } | null;
  attestation?: { status?: string | null } | null;
  experiment?: {
    model?: string | null;
    budget_seconds?: number | null;
    thinking_mode?: string | null;
    context_window_tokens?: number | null;
    sampling_params?: Record<string, unknown> | null;
    output_policy?: Record<string, unknown> | null;
    execution_guidance?: Record<string, unknown> | null;
    contract_guidance?: { profile: string; sha256: string | null; recorded: boolean } | null;
    data_profile?: { name?: string | null; label?: string | null } | null;
    planner?: { profile?: string | null; label?: string | null } | null;
    security_profile?: { name?: string | null } | null;
  } | null;
  timing?: { started_at?: string | null; elapsed_seconds?: number | null } | null;
};

/** One curated turn of prior conversation sent as model context. */
export type AssistantHistoryTurn = {
  role: "user" | "assistant";
  content: string;
};

export type Plan = {
  plan_id: string;
  state: string;
  policy_sha256?: string | null;
  snapshot_sha256?: string | null;
  prepared?: {
    operation_counts?: Record<string, number> | null;
    step_count?: number | null;
    expected_new_spend?: number | null;
  } | null;
  success?: Record<string, unknown> | null;
  journal_present?: boolean;
  execution?: {
    status: "not_recorded" | "succeeded" | "stopped" | "unknown";
    journal_status?: "missing" | "empty" | "plan_only" | "recorded" | "invalid" | "unreadable";
    dispatch_count?: number | null;
    verified_count?: number | null;
    pending_dispatch?: boolean | null;
    reason?: string | null;
  } | null;
};

export type Approval = {
  status: string;
  expires_at_utc?: string;
  approver_label?: string;
  run_id?: string;
  plan_id?: string;
  policy_sha256?: string;
  snapshot_sha256?: string;
};

export type Config = { deployment: string; runner_enabled: boolean; scope_id?: string; display_name?: string };

/** Authenticated runtime capabilities; product_approval gates live case writes. */
export type Capabilities = {
  schema_version?: number;
  deployment?: string;
  project_qa: boolean;
  historical_review: boolean;
  live_prepare: boolean;
  live_execute: boolean;
  product_approval: boolean;
  worker_available: boolean;
  runner_enabled?: boolean;
  reason?: string | null;
  case_read?: boolean;
  case_qa?: boolean;
  case_create?: boolean;
  case_prepare?: boolean;
  case_approve?: boolean;
  product_prepare?: boolean;
  product_execute?: boolean;
  product_scenarios?: { id: "A" | "B"; task: string; label: string; can_create: boolean }[];
  environment?: { scenario?: "A" | "B"; task?: string; deployment: string; data_source: string; execution: string; evidence_time: string | null; connection?: string; ready?: boolean | null; checked_at?: string | null; lease?: { enforced?: boolean; available?: boolean; owned_by_me?: boolean; requires_admin_reset?: boolean; reason?: string | null } };
};

export type ProductOrder = {
  ref: string;
  product: string;
  units: number;
  due_days: number;
  due_days_max?: number;
  customer: string;
};

export type ProductDisruption = {
  kind: string;
  vendor?: string;
  workcenter?: string;
  workcenter_code?: string;
};

export type ProductAction = {
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
  kind: string;
  document: string;
  summary: string;
  quantity: number;
};

/** Policy checks are optional until the product API ships them. */
export type ProductCheckStatus =
  | "pass"
  | "warn"
  | "block"
  | "unknown"
  | "planned";

export type ProductPlanCheck = {
  code: string;
  label: string;
  status: ProductCheckStatus | string;
  actual?: string | number | boolean | null;
  threshold?: string | number | boolean | null;
  source?: string | null;
  /** Only this same-plan provenance identifies a per-order sales-value budget check. */
  basis?: { kind: "sales_order_value_per_customer_cap_v1"; plan_id: string; snapshot_sha256: string } | null;
};

export type CostValue = {
  quantity: string; unit_price: string | null; amount: string | null;
  uom_id: number; uom_label: string | null;
  price_source: "purchase_unit_price" | "public_workcenter_standard";
  price_reference: string | null; missing: "unit_price_missing" | null;
};
export type ProductCosts = {
  schema_version: 1;
  basis: {
    snapshot_sha256: string; pricing_sha256: string; as_of: string | null;
    anchor_date: string; currency: "USD"; company_id: number;
    sales_order_ids: number[]; target_product_code: string;
    tax_basis: string; manufacturing_basis: string; unit_basis: string;
  };
  lines: {
    document: string; kind: "purchase_order" | "manufacturing_order";
    action: "keep" | "update" | "cancel" | "create"; product_code: string;
    before: CostValue | null; after: CostValue | null; known_delta: string | null;
    cancellation_fee: null;
  }[];
  baseline: { purchase: string | null; manufacturing: string | null; total: string | null; known_subtotal: string };
  planned: { purchase: string | null; manufacturing: string | null; total: string | null; known_subtotal: string };
  comparable: boolean; known_delta: string | null; new_purchase_value: string | null;
  purchase_increase: string | null; new_manufacturing_standard: string | null;
  full_delta: null; actual_cost: null; missing: string[];
};

export type DecisionMetrics = {
  schema_version: 1 | 2;
  basis: { plan_id: string; snapshot_sha256: string; currency: string; pricing_sha256: string;
    margin_basis?: "new_spend_coverage_v2";
    cost_basis: "recorded_unit_price_and_manufacturing_standard"; capacity_basis: "planning_horizon_standard_minutes" };
  margin: { sales_value: string; new_spend_revenue?: string; new_spend_units?: number; stock_units?: number; planned_cost: string | null; original_floor_pct: string; effective_floor_pct: string;
    planned_margin_pct: string | null; headroom_pp: string | null; max_planning_cost: string; cost_headroom: string | null };
  order_budgets: { order_ref: string; sales_value: string; customer_cap: string; headroom: string }[];
  workcenters: { code: string; used_standard_minutes: string | null; horizon_limit_minutes: string | null; headroom_minutes: string | null }[];
  readiness: { order_count: number; zero_buffer_orders: number; late_orders: number; uncovered_orders: number;
    unknown_ready_orders: number; minimum_buffer_days: number | null };
};

export type OrderReadinessData = { schema_version: 1;
    basis: { plan_id: string; snapshot_sha256: string; anchor_date: string; time_basis: "planned_supply_availability_days"; quantity_scope: "target_product" };
    orders: { order_ref: string; demand_units: number; covered_units: number; customer_due_day: number; customer_due_date: string;
      planned_ready_day: number | null; planned_ready_date: string | null; buffer_days: number | null;
      stock_units: number; manufactured_units: number; purchased_units: number; status: "on_time" | "late" | "incomplete" | "unknown" }[] };

export type ImpactSourceRef = {
  projection: "source_facts";
  artifact: "request" | "snapshot" | "blueprint" | "plan" | "change_set" | "order_readiness";
  record: string; fields: string[];
};
export type ImpactFact = string | number | boolean | null | ImpactFact[] | { [key: string]: ImpactFact };
export type ImpactNode = {
  key: string;
  kind: "disruption" | "purchase_order" | "manufacturing_order" | "sales_order" | "stock" | "unallocated_supply";
  phase: "baseline" | "planned" | "shared";
  label: string; product_code: string | null; quantity: number | null; unit: "units" | null;
  action: "keep" | "create" | "update" | "cancel" | null;
  facts: Record<string, ImpactFact>; source_refs: ImpactSourceRef[];
};
export type ImpactEdge = {
  key: string; from: string; to: string; phase: "baseline" | "planned";
  relation: "disruption_scope" | "origin_scope" | "component_allocation" | "sales_allocation" | "unused_supply";
  product_code: string; quantity: number | null;
  quantity_basis: "recorded_scope_quantity_unknown" | "exact_plan_allocation" | "unallocated_supply";
  available_day: number | null; required_day: number | null; source_refs: ImpactSourceRef[];
};
export type ImpactOrder = {
  order_ref: string; node_key: string; baseline_node_keys: string[]; planned_node_keys: string[];
  baseline_edge_keys: string[]; planned_edge_keys: string[];
  readiness: OrderReadinessData["orders"][number]; missing: string[];
};
export type ProductImpactChain = {
  schema_version: 1;
  basis: { plan_id: string; snapshot_sha256: string; anchor_date: string; target_product_code: string;
    source_sha256: Record<"request" | "snapshot" | "blueprint" | "plan" | "change_set" | "order_readiness", string>; quantity_basis: "product_units"; time_basis: "planned_supply_availability_days" };
  nodes: ImpactNode[]; edges: ImpactEdge[]; orders: ImpactOrder[]; missing: string[];
  source_facts: Record<string, ImpactFact>;
};

export type ProductPlan = {
  retention_refinement?: { profile: "fixed-totals-retention-v1"; status: "improved" | "unchanged" | "fallback" | "skipped";
    retained_before: number; retained_after: number; reason: string } | null;
  margin_basis?: "new_spend_coverage_v2" | null;
  decision_metrics?: DecisionMetrics | null;
  plan_id: string;
  order_readiness?: OrderReadinessData | null;
  impact_chain?: ProductImpactChain | null;
  objective?: "preserve_quantities" | "min_planned_cost" | "preserve_capacity";
  capacity?: { schema_version: 1; basis: { plan_id: string; snapshot_sha256: string; quantity_scope: "target_product"; time_basis: "standard_unit_minutes" };
    manufactured_units: number; finished_purchase_units: number; standard_minutes: number | null;
    workcenters: { code: string; standard_minutes: number | null }[]; missing: string[] } | null;
  actions: ProductAction[];
  margin: number | null;
  on_time: boolean | null;
  /** Legacy key for planned purchases and manufacturing, including retained orders; not a cost delta. Missing measurements remain null. */
  new_spend?: number | null;
  checks?: ProductPlanCheck[] | null;
  /** Change relative to a measured baseline; unknown without a baseline. */
  cost_delta: number | null;
  costs?: ProductCosts | null;
  policy_sha256: string;
  snapshot_sha256: string;
  prepared_at: string;
};

export type ProductSelection = { plan_id: string; comparison_id: string; selection_revision: number };
export type ProductComparison = {
  status: "ready"; comparison_id: string; version: number; selection_revision: number;
  selected_plan_id: string | null; plans: ProductPlan[];
  attempts: { objective: NonNullable<ProductPlan["objective"]>; status: "prepared" | "duplicate" | "infeasible" | "rejected";
    plan_id?: string; equivalent_plan_id?: string; reason?: string }[];
} | { status: "unavailable"; version: number; reason: string };

export type ProductApproval = {
  phase?: string;
  expires_at?: string;
  approver_label?: string;
  plan_id?: string;
  issued_at?: string;
  comment?: string;
};

export type ProductWrite = {
  kind?: string;
  document?: string;
  quantity?: number;
};

export type ProductReadbackRecord = {
  kind?: string;
  document?: string;
  state?: string;
  quantity?: number;
};

export type ProductVerifierCheck = {
  kind?: string;
  document?: string;
  passed?: boolean;
  applicable?: number;
  total?: number;
};

/** Executor readback retained on the case after an approval is applied. */
export type ProductExecution = {
  status: string;
  readback:
    | {
        records?: ProductReadbackRecord[];
        execution_validated?: boolean;
        verified_steps?: number;
      }
    | null;
  verifier: {
    source?: string | null;
    evaluation_scope?: string;
    original_passed?: boolean | null;
    rules?: { by_dimension?: Record<string, unknown> } | null;
    failed_rule_count?: number | null;
    passed_rule_count?: number | null;
    applicable_rule_count?: number | null;
    status?: string;
    score?: number | null;
    checks?: ProductVerifierCheck[];
    strict_success?: boolean | null;
    evaluation_valid?: boolean | null;
  } | null;
  writes: ProductWrite[];
};

/** Optional case Q&A contract; the UI falls back to case fields when absent. */
export type ProductAskCitation = {
  field?: string | null;
  label?: string | null;
  value?: string | number | boolean | null;
};

export type CaseAnswerContext = {
  schema_version: 1;
  case_id: string;
  state: string;
  version: number | null;
  plan_id: string | null;
  comparison_id: string | null;
  selection_revision: number | null;
  selection_pending: boolean;
  updated_at: string | null;
  outcome: string | null;
};

export type ProductAskAnswer = {
  case_context?: CaseAnswerContext | null;
  answer?: string | null;
  mode?: "case" | string | null;
  citations?: (ProductAskCitation | string)[] | null;
  found?: boolean | null;
  source?: string | null;
  evidence_refs?: { kind: string; id: string; href: string; label: string }[];
};

/** The server's public CaseFile projection; it never includes an owner uid. */
export type CaseFile = {
  case_id: string;
  scenario: string;
  state: string;
  synthetic: boolean;
  order: ProductOrder;
  disruption: ProductDisruption;
  plan: ProductPlan | null;
  comparison?: ProductComparison;
  selection_request?: ProductSelection & { requested_at: string; error: string | null };
  approval: ProductApproval | null;
  execution: ProductExecution | null;
  created_at: string;
  updated_at: string;
  reason: string | null;
  decision?: { can_approve: boolean; reason: string | null; blocked_checks: string[]; unknown_checks: string[]; warnings: string[]; can_reject: boolean; can_request_changes: boolean; revision_reason: string | null; can_reconcile?: boolean; can_resume_preparation?: boolean; preparation_pending?: boolean; can_cancel_unstarted?: boolean; authorization_pending?: boolean; can_resume_revision?: boolean; revision_pending?: boolean; can_reprepare?: boolean; repreparation_pending?: boolean; can_resume_repreparation?: boolean; can_select_candidate?: boolean; selection_pending?: boolean; can_resume_selection?: boolean };
  result?: {
    simulation_passed?: boolean;
    verification_source?: string; execution_status: string | null; readback_status: string; verifier_status: string; strict_success: boolean | null; evaluation_valid: boolean | null; verified: boolean; outcome: string };
  revision?: { version: number; parent_plan_id: string | null; reason: string | null; changes: Record<string, unknown> };
  revision_limits?: { original_margin_pct: number; effective_margin_pct: number; max_exclusive_pct: number; decimal_places: number } | null;
  revision_request?: { version: number; changes: Record<string, unknown>; reason: string; requested_at: string; error: string | null };
  repreparation_request?: { version: number; failed_version: number; changes: Record<string, unknown>; reason: string; requested_at: string; error: string | null };
  history?: unknown[];
  audit?: unknown[];
};

export type Passage = {
  path: string;
  start_line: number;
  end_line: number;
  text: string;
  score: number;
};

export type AnswerMode = "model" | "retrieval" | "harness";

export type Answer = {
  found: boolean;
  mode?: AnswerMode;
  answer?: string;
  warning?: string;
  passages: Passage[];
  citations: string[];
  /** Optional summarized harness activity and project/general scope. */
  activity?: ActivityStep[];
  scope?: "general" | "project" | string;
  session_id?: string | null;
  elapsed_ms?: number;
  evidence_refs?: { kind: string; id: string; href: string; label: string }[];
};

export type ModelTestResult =
  | { ok: true; latency_ms: number }
  | { ok: false; error?: string };

/** Client-safe view of the server model settings; the key is never returned. */
export type ModelSettingsInfo = {
  configured: boolean;
  source?: string;
  profile?: string | null;
  base_url?: string;
  model?: string;
  has_key?: boolean;
  key_hidden?: boolean;
  can_override?: boolean;
  read_only?: boolean;
  updated_at?: string | null;
};


/** A summarized, user-visible activity trace. Raw chain-of-thought is never exposed. */
export type ActivityStepStatus = "pending" | "active" | "done" | "failed";

export type ActivityStep = {
  id: string;
  kind?: string;
  label: string;
  detail?: string;
  status: ActivityStepStatus;
  /** MCP search evidence rows, rendered by DSH SearchBlock. */
  evidence?: { kind?: string; id?: string; excerpt?: string }[];
  /** Bounded case/run payload, rendered by DSH JsonTree. */
  payload?: unknown;
};

export type ActivityState = {
  active: boolean;
  startedAt: number;
  hideDuration?: boolean;
  durationLabel?: string;
  finishedAt?: number;
  steps: ActivityStep[];
  title?: string;
  tone?: "work" | "verify";
  /** Harness runs can retry a failed tool without failing the answer. */
  outcome?: "success" | "failed";
  openByDefault?: boolean;
};
