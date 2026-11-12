import { useEffect, useState } from "react";
import { MarkdownText, Modal } from "@forgeflow/dsh-primitives";
import { api } from "./api";
import { useI18n } from "./i18n";
import type { DocumentTarget } from "./knowledgeCitation";

type DocumentBlocks = {
  source_path: string;
  version: number;
  blocks: { ord: number; text: string; locator?: string; version: number }[];
};

export function KnowledgeDocumentDialog({ target, onClose }: { target: DocumentTarget; onClose: () => void }) {
  const { t } = useI18n();
  const [content, setContent] = useState<DocumentBlocks | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setContent(null); setFailed(false);
    if (!target.version) { setLoading(false); return; }
    setLoading(true);
    void api<DocumentBlocks>(`/api/knowledge/documents/${encodeURIComponent(target.sourcePath)}/blocks?version=${target.version}`,
      { signal: controller.signal }).then((value) => {
      if (controller.signal.aborted) return;
      if (value.source_path !== target.sourcePath || value.version !== target.version
        || value.blocks.some((block) => block.version !== target.version)) throw new Error("document_version_mismatch");
      setContent(value);
    }).catch(() => { if (!controller.signal.aborted) setFailed(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [target.sourcePath, target.version, attempt]);
  const title = `${target.sourcePath.split("/").pop()}${target.version ? ` · v${target.version}` : ""}`;
  const labels = { code: { copyLabel: t("assistant.copyCode"), copiedLabel: t("assistant.copiedCode") }, footnotes: t("assistant.footnotes") };
  return <Modal open onClose={onClose} title={title} headless className="settings-dialog settings-dialog-wide">
    <div className="settings-head"><h2>{title}</h2>
      <button type="button" className="settings-close" aria-label={t("settings.close")} onClick={onClose} data-modal-autofocus="true">×</button>
    </div>
    <div className="settings-content" data-knowledge-document="true">
      {target.excerpt && <section className="settings-section">
        <h3>{t("settings.documentExcerpt")}{target.locator ? ` · ${target.locator}` : ""}</h3>
        <div className="assistant-markdown"><MarkdownText text={target.excerpt} streaming={false} labels={labels} /></div>
      </section>}
      {loading && <p role="status">{t("settings.documentsLoading")}</p>}
      {(failed || (!target.version && !target.excerpt)) && <p role="alert">{t("settings.documentVersionUnavailable")}</p>}
      {failed && <button className="secondary" type="button" onClick={() => setAttempt((value) => value + 1)}>{t("setupOps.retryLoad")}</button>}
      {content && <section className="settings-section">
        {target.excerpt ? <details><summary>{t("settings.documentOpen")} · v{content.version}</summary>
          <div className="assistant-markdown"><MarkdownText text={content.blocks.map((block) => block.text).join("\n\n")} streaming={false} labels={labels} /></div>
        </details> : <div className="assistant-markdown"><MarkdownText text={content.blocks.map((block) => block.text).join("\n\n")} streaming={false} labels={labels} /></div>}
      </section>}
    </div>
  </Modal>;
}
