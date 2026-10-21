import { Component, type ReactNode } from "react";
import { useI18n } from "./i18n";
function RecoveryNotice() {
  const { t } = useI18n();
  return <main className="view" role="alert"><h1>{t("v9.displayError")}</h1><p>{t("v9.displayErrorHint")}</p><button className="primary" onClick={() => { location.hash = "#cases"; location.reload(); }}>{t("case.backToCases")}</button></main>;
}
/** Rendering failures must not erase the recovery path or reveal internal stacks. */
export class WorkbenchErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? <RecoveryNotice /> : this.props.children; }
}
