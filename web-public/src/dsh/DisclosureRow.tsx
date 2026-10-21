import { memo, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { TextShimmer } from "./TextShimmer";
import css from "./DisclosureRow.module.css";

function Chevron({ up = false }: { up?: boolean }) {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d={up ? "m4 10 4-4 4 4" : "m4 6 4 4 4-4"} stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export const DisclosureRow = memo(function DisclosureRow({
  icon, title, open, expandable, onToggle, running = false, collapsedContent, children,
  rowClassName, leadingClassName, titleClassName,
}: {
  icon: ReactNode;
  title: string;
  open: boolean;
  expandable: boolean;
  onToggle: () => void;
  running?: boolean;
  collapsedContent?: ReactNode;
  children?: ReactNode;
  rowClassName?: string;
  leadingClassName?: string;
  titleClassName?: string;
}) {
  const toggleFromLeading = (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    onToggle();
  };
  const toggleFromKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    onToggle();
  };
  const leading = open ? <Chevron up /> : (
    <>
      <span className={css.iconIdle}>{icon}</span>
      <span className={css.chevronHover}><Chevron /></span>
    </>
  );

  return (
    <div className={css.root} data-open={open || undefined}>
      <div className={css.header} data-disclosure-header>
        <div
          className={`${css.row}${rowClassName ? ` ${rowClassName}` : ""}`}
          data-disclosure-row
          data-expandable={expandable || undefined}
          role={expandable ? "button" : undefined}
          tabIndex={expandable ? 0 : undefined}
          aria-expanded={expandable ? open : undefined}
          onClick={expandable ? onToggle : undefined}
          onKeyDown={expandable ? toggleFromKeyboard : undefined}
        >
          {expandable ? (
            <button type="button" className={`${css.leading}${leadingClassName ? ` ${leadingClassName}` : ""}`} aria-label={title} aria-expanded={open} onClick={toggleFromLeading}>
              {leading}
            </button>
          ) : <span className={`${css.leading}${leadingClassName ? ` ${leadingClassName}` : ""}`}>{leading}</span>}
          <TextShimmer active={running}>
            <TextShimmer className={`${css.title}${titleClassName ? ` ${titleClassName}` : ""}`}>{title}</TextShimmer>
            {!open ? collapsedContent : null}
          </TextShimmer>
        </div>
      </div>
      {open ? children : null}
    </div>
  );
});
