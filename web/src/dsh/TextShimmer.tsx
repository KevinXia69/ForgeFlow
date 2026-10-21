import { createContext, memo, useContext, type ReactNode } from "react";
import css from "./TextShimmer.module.css";

const DecorativeCopy = createContext<boolean | undefined>(undefined);

function TextContent({ children, className }: { children: ReactNode; className?: string }) {
  const decorative = useContext(DecorativeCopy);
  const generated = decorative === true && typeof children === "string";
  return (
    <span className={`${css.text}${className ? ` ${className}` : ""}`} data-shimmer-text={generated ? children : undefined}>
      {generated ? null : children}
    </span>
  );
}

export const TextShimmer = memo(function TextShimmer({
  children, active = false, className, contentClassName,
}: {
  children: ReactNode;
  active?: boolean;
  className?: string;
  contentClassName?: string;
}) {
  const decorative = useContext(DecorativeCopy);
  if (decorative !== undefined) return <TextContent className={className}>{children}</TextContent>;
  const content = typeof children === "string" ? <TextContent>{children}</TextContent> : children;
  return (
    <span className={`${css.root}${className ? ` ${className}` : ""}`} data-shimmer={active || undefined}>
      <DecorativeCopy.Provider value={false}>
        <span className={`${css.content}${contentClassName ? ` ${contentClassName}` : ""}`}>{content}</span>
      </DecorativeCopy.Provider>
      {active ? (
        <span className={css.decoration} aria-hidden="true" inert>
          <span className={css.sweep}>
            <DecorativeCopy.Provider value>
              <span className={`${css.content} ${css.highlight}${contentClassName ? ` ${contentClassName}` : ""}`}>{content}</span>
            </DecorativeCopy.Provider>
          </span>
        </span>
      ) : null}
    </span>
  );
});
