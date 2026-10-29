import { hasNewSpendBasis } from "./decisionSummary";
import type { Translate } from "./i18n";
import type { CaseFile } from "./types";
import { marginFloor, preparationPending } from "./caseUi";

export function canContinuePreparation(caseFile: CaseFile): boolean {
  const decision = caseFile.decision;
  if (decision?.revision_pending) return decision.can_resume_revision === true;
  if (decision?.repreparation_pending) return decision.can_resume_repreparation === true;
  return caseFile.state === "preparing" && decision?.can_resume_preparation !== false;
}

export function preparationFailure(reason: string | null | undefined, t: Translate): string {
  if (reason === "product_selection_conflict") return t("candidate.conflictBody");
  if (reason === "product_candidates_unavailable") return t("candidate.noneAvailable");
  if (reason === "planning_model_infeasible") return t("v10.infeasible");
  if (reason === "product_constraint_not_satisfied") return t("v10.constraintNotSatisfied");
  if (reason === "product_preparation_interrupted") return t("v10.preparationInterrupted");
  if (reason === "product_constraints_invalid" || reason === "product_constraints_must_tighten"
      || reason === "product_constraints_relaxed") return t("v10.invalidMargin");
  if (reason === "product_revision_conflict" || reason === "product_binding_changed"
      || reason === "product_constraints_binding_changed") return t("v10.versionConflict");
  if (reason === "revision_not_started") return t("v10.revisionNotStarted");
  if (reason === "product_history_limit") return t("v10.versionLimit");
  return t("v10.preparationFailed");
}

export function preparationMessage(caseFile: CaseFile, t: Translate): string {
  if (caseFile.decision?.selection_pending) return t("candidate.pendingBody");
  if (caseFile.comparison?.status === "unavailable") return t("candidate.unavailable");
  if (caseFile.state === "awaiting_selection") return t("candidate.chooseFirst");
  const pending = preparationPending(caseFile);
  if (pending) return t(pending.body);
  if (caseFile.reason) return preparationFailure(caseFile.reason, t);
  return t(caseFile.state === "preparing" ? "case.planPreparing" : "case.noPlanBody");
}

export function revisionFloor(caseFile: CaseFile): number | null {
  if (!hasNewSpendBasis(caseFile.plan)) return null;
  const value = caseFile.revision_limits?.effective_margin_pct ?? marginFloor(caseFile.plan);
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value < 100 ? value : null;
}

export function validRevisionMargin(value: number, floor: number | null): boolean {
  return floor !== null && Number.isFinite(value) && value > floor && value < 100
    && Number(value.toFixed(2)) === value;
}

export function suggestedRevisionMargin(caseFile: CaseFile): number {
  const floor = revisionFloor(caseFile);
  return floor === null ? 0 : Math.min(99.99, (Math.floor(floor * 100) + 1) / 100);
}
