import { useMemo, useState } from "react";
import { MarkdownText, type MarkdownLabels } from "@forgeflow/dsh-primitives";
import { useI18n } from "./i18n";
import { privateMarkdownSource } from "./markdownPrivacy";
import css from "./PrivateMarkdown.module.css";

/** User-uploaded chat pictures are rendered separately by Assistant. This
 * wrapper governs destinations supplied by model answers and document text. */
export function PrivateMarkdown({ text, labels }: { text: string; labels: MarkdownLabels }) {
  const { t } = useI18n();
  const parsed = useMemo(() => privateMarkdownSource(text, window.location.origin), [text]);
  const [selection, setSelection] = useState<{ text: string; keys: string[] }>({ text, keys: [] });
  const approved = selection.text === text ? selection.keys : [];
  const pathImages = useMemo(() => ({ resolve: (key: string) => {
    const image = parsed.images.find((item) => item.key === key);
    return image && (image.trusted || (selection.text === text && selection.keys.includes(key))) ? image.source : undefined;
  } }), [text, parsed, selection]);
  return <div data-private-markdown="true">
    <MarkdownText text={parsed.text} streaming={false} labels={labels} pathImages={pathImages} />
    {parsed.images.some((image) => !image.trusted && !approved.includes(image.key)) && <div className={css.images} aria-label={t("privacy.externalImages")}>
      <p className={css.notice}>{t("privacy.externalImageNotice")}</p>
      {parsed.images.filter((image) => !image.trusted && !approved.includes(image.key)).map((image) => <div className={css.image} key={image.key}>
        <span className={css.label}>{image.alt || t("privacy.externalImages")} · {image.host}</span>
        <button type="button" className="secondary" onClick={() => setSelection((current) => ({ text, keys: [...(current.text === text ? current.keys : []), image.key] }))}>
          {t("privacy.loadImage", { host: image.host })}
        </button>
      </div>)}
    </div>}
  </div>;
}
