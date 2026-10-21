import { memo, useMemo, useState, type ReactNode } from "react";
import { DisclosureRow } from "./DisclosureRow";
import { TextShimmer } from "./TextShimmer";
import css from "./ReasoningRow.module.css";

function ThinkIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true" strokeWidth="1">
      <path d="M10.2854 5.71481C12.9673 8.39663 14.1182 11.5938 12.8562 12.8559C11.5942 14.1179 8.39706 12.9669 5.71518 10.2851C3.03333 7.60323 1.88236 4.40608 3.14441 3.14403C4.40644 1.882 7.6036 3.03297 10.2854 5.71481Z" stroke="currentColor" />
      <path d="M10.2854 10.2851C7.6036 12.9669 4.40644 14.1179 3.14441 12.8559C1.88236 11.5938 3.03333 8.39663 5.71518 5.71481C8.39706 3.03297 11.5942 1.882 12.8562 3.14403C14.1182 4.40608 12.9673 7.60323 10.2854 10.2851Z" stroke="currentColor" />
      <path d="M8.86291 8.0002C8.86291 8.47549 8.47762 8.86087 8.00224 8.86087C7.52694 8.86087 7.1416 8.47549 7.1416 8.0002C7.1416 7.52485 7.52694 7.13953 8.00224 7.13953C8.47762 7.13953 8.86291 7.52485 8.86291 8.0002Z" fill="currentColor" />
    </svg>
  );
}

function firstLine(text: string): string {
  const newline = text.indexOf("\n");
  return newline === -1 ? text : text.slice(0, newline);
}

function latestCompletedParagraphFirstLine(text: string): string {
  let summary = "";
  let paragraphStart = 0;
  const separator = /\r?\n(?:[\t ]*\r?\n)+/g;
  while (true) {
    const next = separator.exec(text);
    const paragraphEnd = next === null ? text.length : next.index + next[0].indexOf("\n");
    const newline = text.indexOf("\n", paragraphStart);
    if (newline !== -1 && newline <= paragraphEnd) {
      const candidate = text.slice(paragraphStart, newline).trim();
      if (candidate !== "") summary = candidate;
    }
    if (next === null) return summary;
    paragraphStart = next.index + next[0].length;
  }
}

/** Ported from DSH's ReasoningRow; ForgeFlow supplies text/running and body. */
export const DshReasoningRow = memo(function DshReasoningRow({
  text, running = false, title, body,
}: {
  text: string;
  running?: boolean;
  title: string;
  body: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const summaryText = running ? latestCompletedParagraphFirstLine(text) : firstLine(text);
  const summary = useMemo(() => summaryText.replaceAll("**", ""), [summaryText]);
  const preview = !expanded && summary !== "";

  return (
    <div className={css.root} data-variant="think" data-state={running ? "running" : "ok"} data-expanded={expanded || undefined} data-preview={preview || undefined}>
      <DisclosureRow
        rowClassName={css.row}
        leadingClassName={css.leading}
        titleClassName={css.title}
        icon={<ThinkIcon />}
        title={title}
        running={running}
        open={expanded}
        expandable
        onToggle={() => setExpanded((value) => !value)}
        collapsedContent={(
          <>
            <span className={css.separator} data-shimmer-decoration aria-hidden="true" />
            <span className={css.summary} data-streaming={running || undefined}>
              <span className={css.summaryText}><TextShimmer>{summary}</TextShimmer></span>
            </span>
          </>
        )}
      >
        <div className={css.thinkBody}>{body}</div>
      </DisclosureRow>
    </div>
  );
});
