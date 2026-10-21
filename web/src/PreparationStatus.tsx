import { useI18n } from "./i18n";
import type { CaseFile } from "./types";
import { formatDateTime } from "./caseUi";
import { preparationFailure, preparationMessage } from "./casePreparation";

export function PreparationStatus({ caseFile, disabled, canContinue, onContinue, onReprepare }: {
  caseFile: CaseFile; disabled: boolean; canContinue: boolean;
  onContinue: () => void; onReprepare: () => void;
}) {
  const { t, lang } = useI18n();
  const revisionPending = caseFile.decision?.revision_pending === true;
  const repreparePending = caseFile.decision?.repreparation_pending === true;
  const request = revisionPending ? caseFile.revision_request : caseFile.repreparation_request;
  const failed = caseFile.state === "failed" && !caseFile.plan && !caseFile.execution;
  const notStarted = caseFile.reason === "revision_not_started";
  const selectionConflict = caseFile.reason === "product_selection_conflict";
  const conflict = caseFile.reason === "product_revision_conflict";
  const history = (caseFile.history ?? []).filter((row): row is Record<string, unknown> =>
    !!row && typeof row === "object" && "version" in row);
  const failures = (caseFile.audit ?? []).filter((row): row is Record<string, unknown> =>
    !!row && typeof row === "object" && "action" in row
    && ["revision_failed", "repreparation_failed"].includes(String(row.action))
    && "reason" in row && row.reason !== "product_revision_conflict");
  const versions = [...history.map(row => ({ row, retired: true })), ...failures.map(row => ({ row, retired: false }))]
    .sort((left, right) => Number(left.row.version) - Number(right.row.version));
  return <>
    {(revisionPending || repreparePending || failed || notStarted || conflict || selectionConflict) && <section className="case-section" data-version-status="true" role="status">
      <h3>{t(selectionConflict ? "candidate.conflictTitle" : revisionPending ? "v10.revisionPendingTitle" : repreparePending ? "v10.repreparationPendingTitle" : notStarted ? "v10.revisionNotStartedTitle" : "v10.failedPreparationTitle")}</h3>
      {request && <p>{t("v10.requestedVersion", { version: request.version })}</p>}
      {typeof request?.changes.minimum_margin_pct === "number" && <p>{t("v10.requestedMargin", { floor: request.changes.minimum_margin_pct })}</p>}
      <p>{preparationMessage(caseFile, t)}</p>
      {(revisionPending || repreparePending) && <button type="button" className="secondary"
        data-continue-version="true" disabled={disabled || !canContinue} onClick={onContinue}>
        {t(revisionPending ? "v10.continueRevision" : "v10.continueRepreparation")}
      </button>}
      {failed && caseFile.decision?.can_reprepare === true && <button type="button" className="secondary"
        data-reprepare-case="true" disabled={disabled} onClick={onReprepare}>{t("v10.reprepare")}</button>}
    </section>}
    {caseFile.revision_limits && <details className="case-section" data-margin-constraints="true">
      <summary>{t("v10.marginConstraints")}</summary>
      <p>{t("v10.originalMargin", { floor: caseFile.revision_limits.original_margin_pct })}</p>
      <p>{t("v10.effectiveMargin", { floor: caseFile.revision_limits.effective_margin_pct })}</p>
      <p className="muted">{t("v10.constraintScope")}</p>
    </details>}
    {(history.length > 0 || failures.length > 0) && <details className="case-section" data-version-history="true">
      <summary>{t("v10.versionHistory")}</summary>
      <ul>
        {versions.map(({ row, retired }, index) => <li key={index}>
          <strong>{t(retired ? "v10.retiredVersion" : "v10.failedVersion", { version: String(row.version) })}</strong>
          <p>{retired ? typeof row.reason === "string" ? row.reason : t("runs.notRecorded")
            : preparationFailure(typeof row.reason === "string" ? row.reason : null, t)}</p>
          {retired && <p className="muted">{formatDateTime(typeof row.retired_at === "string" ? row.retired_at : null, lang)}</p>}
        </li>)}
      </ul>
      <p className="muted">{t("v10.historyExplanation")}</p>
    </details>}
  </>;
}
