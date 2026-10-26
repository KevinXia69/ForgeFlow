import type { Translate } from "./i18n";
import type { Capabilities, CaseFile } from "./types";

export type EnvironmentNotice = {
  kind: "environment";
  cause: "busy" | "historical" | "reset";
  reason: string;
  recovery: string;
};

/** Read-only access explanations contain no owner identifiers. */
export function environmentAccessNotice(
  t: Translate,
  capabilities?: Pick<Capabilities, "reason" | "environment"> | null,
  caseFile?: Pick<CaseFile, "decision"> | null,
): EnvironmentNotice | null {
  if (caseFile?.decision?.reason === "environment_generation_mismatch") {
    return { kind: "environment", cause: "historical", reason: t("environment.historical"),
      recovery: t("environment.historicalRecovery") };
  }
  const reasons = [caseFile?.decision?.reason, capabilities?.reason, capabilities?.environment?.lease?.reason];
  if (reasons.includes("environment_busy")) {
    return { kind: "environment", cause: "busy", reason: t("environment.busy"),
      recovery: t("environment.busyRecovery") };
  }
  if (reasons.includes("environment_generation_mismatch")) {
    return { kind: "environment", cause: "reset", reason: t("environment.reset"),
      recovery: t("environment.resetRecovery") };
  }
  return null;
}
