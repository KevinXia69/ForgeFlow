import type { MessageKey, Translate } from "./i18n";

// Human labels supplied by the read-only run API. Unknown values (including
// task ids and scenario letters) are rendered unchanged so technical data is
// never lost.
const TASK_LABEL_KEYS: Record<string, MessageKey> = {
  "2280_medium_repair_plan_medium": "runs.taskSupplierRepair",
  "2299_hard_repair_plan_hard": "runs.taskLineRepair",
  "\u4f9b\u5e94\u5546\u65ad\u4f9b": "runs.taskSupplierDisruption",
  "\u4ea7\u7ebf\u505c\u673a": "runs.taskLineDowntime",
  "\u5176\u4ed6\u4efb\u52a1": "runs.taskOther",
  "\u4f9b\u5e94\u5546\u65ad\u4f9b\u540e\u7684\u8ba1\u5212\u4fee\u590d": "runs.taskSupplierRepair",
  "\u4ea7\u7ebf\u505c\u673a\u540e\u7684\u8ba1\u5212\u4fee\u590d": "runs.taskLineRepair",
};

export function translateTaskLabel(
  label: string | null | undefined,
  t: Translate,
): string | null {
  if (!label) return null;
  const key = TASK_LABEL_KEYS[label];
  return key ? t(key) : label;
}

/** Generic server labels must not hide a task identity. */
export function runTaskLabel(task: string | null | undefined, label: string | null | undefined, t: Translate): string {
  if (task && TASK_LABEL_KEYS[task]) return t(TASK_LABEL_KEYS[task]);
  if (label && TASK_LABEL_KEYS[label] && TASK_LABEL_KEYS[label] !== "runs.taskOther") return t(TASK_LABEL_KEYS[label]);
  if (task) return t("runs.taskNumber", { task: task.match(/^\d+/)?.[0] || task });
  return label && !TASK_LABEL_KEYS[label] ? label : t("runs.taskOther");
}

export function profileLabel(kind: "planner" | "data", code: string | null | undefined, t: Translate): string {
  if (!code) return t("runs.notRecorded");
  const keys: Record<string, MessageKey> = kind === "planner" ? {
    none: "runs.plannerNone", "cp-sat-live-v1": "runs.plannerLive", "cp-sat-recovery-v1": "runs.plannerRecovery",
  } : { original: "runs.dataOriginal", "planning-metadata-v1": "runs.dataPlanning" };
  return keys[code] ? `${t(keys[code])} (${code})` : code;
}
