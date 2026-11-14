import { useI18n } from "./i18n";
import { citationReferences, type CitationReference, type DocumentTarget } from "./knowledgeCitation";
import type { Passage } from "./types";
import "./CitationSources.css";

export function CitationSources({ citations, passages, onOpen, reviewedComparison = false }: {
  citations: string[]; passages: Passage[]; onOpen: (target: DocumentTarget) => void; reviewedComparison?: boolean;
}) {
  const { t } = useI18n();
  const heading = t(reviewedComparison ? "assistant.comparisonSources" : "assistant.sources");
  const notice = t(reviewedComparison ? "assistant.comparisonSourcesNotice" : "assistant.sourcesNotice");
  const refs = citationReferences(citations, passages);
  if (!refs.length) return null;
  const renderSource = ({ citation, target, url }: CitationReference) => {
      const source = target?.sourceMetadata;
      return <div className="answer-source-row" key={citation}>
        {target ? <button type="button" className="citation answer-source-title" data-source-path={target.sourcePath} onClick={() => onOpen(target)}>
          {source?.title || target.sourcePath.split("/").pop()}
          {target.version ? ` · v${target.version}` : ""}
          {!source?.original_locator && target.locator ? ` · ${target.locator}` : ""}
        </button> : <a className="citation answer-source-title" href={url} target="_blank" rel="noreferrer noopener">{url}</a>}
        {source && <div className="answer-source-detail">
          <div>
            {source.original_url ? <a href={source.original_url} target="_blank" rel="noreferrer noopener">
              {source.publisher || new URL(source.original_url).hostname} ↗
            </a> : source.publisher}
            {source.material_class === "industry_reference" && <span className="answer-source-badge">{t("assistant.sourceIndustryReference")}</span>}
          </div>
          {source.original_locator && <div>{t("assistant.sourceOriginalLocator")}: {source.original_locator}</div>}
        </div>}
      </div>;
  };
  return <section className="answer-citations answer-source-list" data-answer-citations="true"
    aria-label={heading}>
    <div className="answer-source-heading">{heading}</div>
    <p className="answer-source-note">{notice}</p>
    <div className="answer-source-items">{refs.slice(0, 3).map(renderSource)}</div>
    {refs.length > 3 && <details className="answer-source-more">
      <summary>{t("assistant.sourcesMore", { count: refs.length - 3 })}</summary>
      <div className="answer-source-items">{refs.slice(3).map(renderSource)}</div>
    </details>}
    {refs.some(ref => ref.target?.sourceMetadata?.material_class === "industry_reference") &&
      <p className="answer-source-note">{t("assistant.sourceIndustryLimit")}</p>}
  </section>;
}
