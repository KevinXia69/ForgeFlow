import { useI18n } from "./i18n";
import type { RunDetail, ProductExecution } from "./types";
export type RuleEvaluation = RunDetail["evaluation"];

/** Only safe rule names and outcomes enter the presentation; no expressions or arguments. */
export function productRuleEvaluation(verifier: ProductExecution["verifier"]): RuleEvaluation {
  const groups = verifier?.rules?.by_dimension;
  let rule_results: NonNullable<RuleEvaluation>["rule_results"] = null;
  if (groups && typeof groups === "object" && !Array.isArray(groups)) {
    const rows = Object.entries(groups);
    if (rows.every(([, values]) => Array.isArray(values))) {
      const flat = rows.flatMap(([dimension, values]) => (values as unknown[]).map(row => ({ dimension, row })));
      if (flat.length && flat.every(({ row }) => row && typeof row === "object" && ["PASS", "FAIL", "NA"].includes(String((row as Record<string, unknown>).status)))) {
        const name = (value: unknown) => typeof value === "string" && /^[A-Za-z][A-Za-z0-9_.-]{0,119}$/.test(value) ? value : null;
        rule_results = flat.map(({ dimension, row }) => {
          const value = row as Record<string, unknown>;
          const status = value.status as "PASS" | "FAIL" | "NA";
          return { dimension: name(dimension), rule: name(value.rule), status, applicable: typeof value.applicable === "boolean" ? value.applicable : status !== "NA" };
        });
      }
    }
  }
  return { passed_rule_count: verifier?.passed_rule_count, applicable_rule_count: verifier?.applicable_rule_count, failed_rule_count: verifier?.failed_rule_count, rule_results };
}

export function RecordedRules({ evaluation }: { evaluation: RuleEvaluation }) {
  const { t } = useI18n();
  const rules = evaluation?.rule_results;
  const failures = rules?.filter(rule => rule.status === "FAIL") ?? [];
  const count = (value: number | null | undefined) => value == null ? "—" : value;
  return <section className="panel" data-rule-results="true">
    <h2>{t("runs.ruleResults")}</h2>
    <p data-rule-counts="true">{t("runs.ruleCounts", { passed: count(evaluation?.passed_rule_count), applicable: count(evaluation?.applicable_rule_count), failed: count(evaluation?.failed_rule_count) })}</p>
    {failures.length > 0 && <div className="rule-failures"><h3>{t("runs.failedRules")}</h3><ul>{failures.slice(0, 5).map((rule, index) => <li key={`${rule.dimension}:${rule.rule}:${index}`}><strong>{rule.rule || t("runs.unnamedRule")}</strong>{rule.dimension ? ` · ${rule.dimension}` : ""}{!rule.applicable ? ` · ${t("runs.ruleNotApplicable")}` : ""}</li>)}</ul></div>}
    {rules == null ? <p className="muted">{t("runs.rulesMissing")}</p> : <details>
      <summary>{t("runs.allRules", { count: rules.length })}</summary>
      <div className="table-wrap"><table><thead><tr><th>{t("runs.ruleName")}</th><th>{t("runs.ruleDimension")}</th><th>{t("v9.resultFilter")}</th><th>{t("runs.ruleApplicable")}</th></tr></thead>
        <tbody>{[...rules].sort((a, b) => Number(b.status === "FAIL") - Number(a.status === "FAIL")).map((rule, index) => <tr key={`${rule.dimension}:${rule.rule}:${index}`}>
          <td>{rule.rule || t("runs.unnamedRule")}</td><td>{rule.dimension || "—"}</td><td>{t(rule.status === "PASS" ? "runs.rulePass" : rule.status === "FAIL" ? "runs.ruleFail" : "runs.ruleNotApplicable")}</td><td>{t(rule.applicable ? "runs.yes" : "runs.no")}</td>
        </tr>)}</tbody>
      </table></div>
    </details>}
  </section>;
}
