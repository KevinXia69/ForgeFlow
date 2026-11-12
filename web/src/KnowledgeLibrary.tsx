import { useEffect, useState } from "react";
import { api } from "./api";
import { useI18n } from "./i18n";
import { KnowledgeDocumentDialog } from "./KnowledgeDocumentDialog";
import type { DocumentTarget } from "./knowledgeCitation";

type Document = {
  source_path: string; filename: string; version: number; classification?: string;
  lifecycle?: { searchable?: boolean; withdrawn?: boolean };
};

export function KnowledgeLibrary() {
  const { t } = useI18n();
  const [documents, setDocuments] = useState<Document[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [versionPath, setVersionPath] = useState("");
  const [versions, setVersions] = useState<{ version: number }[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsFailed, setVersionsFailed] = useState(false);
  const [target, setTarget] = useState<DocumentTarget | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setFailed(false);
    void api<Document[]>("/api/knowledge/documents", { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setDocuments(value); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [attempt]);
  useEffect(() => {
    const controller = new AbortController();
    setVersions([]); setVersionsFailed(false);
    if (!versionPath) return;
    setVersionsLoading(true);
    void api<{ version: number }[]>(`/api/knowledge/documents/${encodeURIComponent(versionPath)}/versions`, { signal: controller.signal })
      .then((value) => { if (!controller.signal.aborted) setVersions(value); })
      .catch(() => { if (!controller.signal.aborted) setVersionsFailed(true); })
      .finally(() => { if (!controller.signal.aborted) setVersionsLoading(false); });
    return () => controller.abort();
  }, [versionPath, attempt]);
  return <section className="settings-section" aria-labelledby="knowledge-library-heading">
    <h3 id="knowledge-library-heading">{t("settings.documents")}</h3>
    {loading ? <p role="status">{t("settings.documentsLoading")}</p>
      : failed ? <p role="alert">{t("settings.documentsLoadFailed")}</p>
        : documents.length === 0 ? <p>{t("settings.documentsEmpty")}</p> : <ul className="settings-documents">
          {documents.map((document) => <li key={document.source_path}>
            <span>{document.filename} · v{document.version}
              {document.source_path.startsWith("reference/") ? ` · ${t("settings.referenceDocument")}` : ""}
              {` · ${document.lifecycle?.withdrawn ? t("settings.documentWithdrawn")
                : document.lifecycle?.searchable ? t("settings.indexed") : t("settings.notIndexed")}`}</span>
            <button type="button" className="secondary" onClick={() => setTarget({ sourcePath: document.source_path, version: document.version })}>{t("settings.documentOpen")}</button>
            <button type="button" className="secondary" onClick={() => setVersionPath(document.source_path)}>{t("settings.versions")}</button>
          </li>)}
        </ul>}
    {versionPath && <div className="settings-section">
      <h4>{t("settings.versions")} · {versionPath.split("/").pop()}</h4>
      {versionsLoading && <p role="status">{t("settings.documentsLoading")}</p>}
      {versionsFailed && <p role="alert">{t("settings.documentsLoadFailed")}</p>}
      {versions.map((item) => <button type="button" className="secondary" key={item.version}
        onClick={() => setTarget({ sourcePath: versionPath, version: item.version })}>v{item.version} · {t("settings.documentOpen")}</button>)}
    </div>}
    <button type="button" className="secondary" disabled={loading} onClick={() => setAttempt((value) => value + 1)}>{t("setupOps.retryLoad")}</button>
    {target && <KnowledgeDocumentDialog target={target} onClose={() => setTarget(null)} />}
  </section>;
}
