import { Component, useEffect, useRef, useState, type ComponentType, type ComponentProps, type ReactNode } from "react";
import { useI18n } from "./i18n";
import { focusPageEntry } from "./workspace";

function LoadFailure({ retry }: { retry: () => void }) {
  const { t } = useI18n();
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { focusPageEntry(heading.current); }, []);
  return <section className="view" role="alert" data-lazy-error="true">
    <h2 ref={heading} tabIndex={-1}>{t("navigation.loadError")}</h2>
    <p>{t("navigation.loadErrorHint")}</p>
    <button type="button" className="primary" onClick={retry}>{t("navigation.retry")}</button>{" "}
    <button type="button" className="secondary" onClick={() => location.reload()}>{t("navigation.reload")}</button>
  </section>;
}

class LoadBoundary extends Component<{ children: ReactNode; retry: () => void }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? <LoadFailure retry={this.props.retry} /> : this.props.children; }
}

/** Load on use, share successful imports, and retire failed import promises.
 * Rendering the loading state explicitly avoids Suspense's reveal delay on
 * these independently navigated panels. A reload also handles cached obsolete
 * module URLs after a deployment. Other mounted views keep their state. */
export function lazyView<ViewType extends ComponentType<any>>(load: () => Promise<{ default: ViewType }>) {
  let loaded: ViewType | null = null;
  let pending: Promise<{ default: ViewType }> | null = null;
  return function DeferredView(props: ComponentProps<ViewType>) {
    const { t } = useI18n();
    const [attempt, setAttempt] = useState(0);
    const [View, setView] = useState<ViewType | null>(() => loaded);
    const [failed, setFailed] = useState(false);
    useEffect(() => {
      let active = true;
      if (!loaded) {
        pending ??= load().then(module => { loaded = module.default; return module; })
          .catch(error => { pending = null; throw error; });
        void pending.then(module => { if (active) setView(() => module.default); })
          .catch(() => { if (active) setFailed(true); });
      } else setView(() => loaded);
      return () => { active = false; };
    }, [attempt]);
    const retry = () => { setFailed(false); setAttempt(value => value + 1); };
    return <LoadBoundary key={attempt} retry={retry}>
      {failed ? <LoadFailure retry={retry} /> : View ? <View {...props} />
        : <p className="loading" role="status" data-lazy-loading="true">{t("app.loading")}</p>}
    </LoadBoundary>;
  };
}
