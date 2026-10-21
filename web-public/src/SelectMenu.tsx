import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import {
  MenuSurface,
  useAnchoredPosition,
  useDismissOnOutsidePointer,
} from "@forgeflow/dsh-primitives";
import css from "./SelectMenu.module.css";

export type SelectOption = {
  value: string;
  label: string;
  description?: string;
  /** Optional option-level disablement; disabled rows keep their label but cannot be chosen. */
  disabled?: boolean;
};

type Props = {
  id?: string;
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  ariaLabel?: string;
  disabled?: boolean;
  className?: string;
};

type FocusTarget = "selected" | "first" | "last";

/** Indexes of the options the keyboard walk may land on. */
function enabledIndexes(options: readonly SelectOption[]): number[] {
  const indexes: number[] = [];
  options.forEach((option, index) => {
    if (!option.disabled) indexes.push(index);
  });
  return indexes;
}

/**
 * Compact select used instead of native browser popups.
 *
 * The installed DSH `Menu` primitive is a command menu: its public API hardcodes
 * `role="menu"`/`role="menuitem"` and cannot emit the `role="listbox"` /
 * `role="option"` / `aria-selected` contract this component's consumers and
 * Playwright smoke test rely on. This is therefore the minimal select adapter:
 * DSH `MenuSurface` supplies the menu material and macOS backing, DSH
 * `useAnchoredPosition` owns fixed anchoring/clamping/scroll tracking, and DSH
 * `useDismissOnOutsidePointer` owns outside-pointer dismissal. The listbox
 * semantics and roving keyboard focus stay local.
 *
 * The surface is pinned with `position: fixed` but stays inside the consumer's
 * DOM subtree (no React portal): that escapes ancestor overflow clipping while
 * keeping dialog-scoped selectors such as `dialog.getByRole("option")` valid.
 */
export function SelectMenu({
  id,
  value,
  options,
  onChange,
  ariaLabel,
  disabled = false,
  className = "",
}: Props) {
  const [open, setOpen] = useState(false);
  const [menuWidth, setMenuWidth] = useState<number | null>(null);
  const [side, setSide] = useState<"bottom" | "top">("bottom");
  const root = useRef<HTMLDivElement | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const panel = useRef<HTMLDivElement | null>(null);
  const optionButtons = useRef<Array<HTMLButtonElement | null>>([]);
  const pendingFocus = useRef<FocusTarget | null>(null);
  const listboxId = useId();
  const selected = options.find((option) => option.value === value) ?? options[0];

  const position = useAnchoredPosition({
    open,
    anchorRef: trigger,
    panelRef: panel,
    side,
    align: "start",
    gap: 6,
    margin: 12,
  });
  useDismissOnOutsidePointer(root, open, setOpen);

  const openMenu = useCallback(
    (focusTarget: FocusTarget) => {
      if (disabled) return;
      const rect = trigger.current?.getBoundingClientRect();
      if (rect !== undefined) {
        if (rect.width > 0) setMenuWidth(rect.width);
        const below = window.innerHeight - rect.bottom;
        setSide(below < 240 && rect.top > below ? "top" : "bottom");
      }
      pendingFocus.current = focusTarget;
      setOpen(true);
    },
    [disabled],
  );

  const closeMenu = useCallback((returnFocus: boolean) => {
    pendingFocus.current = null;
    setOpen(false);
    if (returnFocus) trigger.current?.focus();
  }, []);

  // Move focus onto the requested row after the listbox mounts. Doing this in a
  // layout effect keeps the open state and the keyboard target in one commit.
  useLayoutEffect(() => {
    // The first commit renders the fixed panel hidden so its real size can be
    // measured; focusing a row inside a visibility:hidden subtree is a no-op,
    // so wait for the anchored position before moving the keyboard focus.
    if (!open || pendingFocus.current === null || position === null) return;
    const focusTarget = pendingFocus.current;
    pendingFocus.current = null;
    const indexes = enabledIndexes(options);
    if (indexes.length === 0) return;
    let next = indexes[0];
    if (focusTarget === "last") {
      next = indexes[indexes.length - 1];
    } else if (focusTarget === "selected") {
      const selectedIndex = options.findIndex(
        (option) => option.value === value && !option.disabled,
      );
      next = selectedIndex >= 0 ? selectedIndex : indexes[0];
    }
    optionButtons.current[next]?.focus();
  }, [open, options, value, position]);

  // A disabled trigger must never leave a listbox floating on the page.
  useEffect(() => {
    if (disabled) closeMenu(false);
  }, [disabled, closeMenu]);

  // Escape closes the listbox before the surrounding dialog sees the key. The
  // capture listener is deliberate: SettingsDialog also listens for Escape on
  // document, and the select is the topmost layer while it is open.
  useEffect(() => {
    if (!open) return;
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      event.stopPropagation();
      closeMenu(true);
    };
    document.addEventListener("keydown", onEscape, true);
    return () => document.removeEventListener("keydown", onEscape, true);
  }, [open, closeMenu]);

  // Roving-focus keyboard walk. Enter/Space are left to the native button
  // activation (the browser turns them into a click), matching how every row
  // already behaves for pointer users.
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        event.ctrlKey ||
        event.altKey ||
        event.metaKey
      ) {
        return;
      }
      const active = document.activeElement;
      const inside =
        root.current?.contains(active) === true ||
        panel.current?.contains(active) === true;
      if (!inside) return;

      if (event.key === "Tab") {
        // Let the browser move focus first; closing synchronously would unmount
        // the focused row before the default Tab action can run.
        pendingFocus.current = null;
        window.setTimeout(() => setOpen(false), 0);
        return;
      }

      if (
        event.key !== "ArrowDown" &&
        event.key !== "ArrowUp" &&
        event.key !== "Home" &&
        event.key !== "End"
      ) {
        return;
      }

      const indexes = enabledIndexes(options);
      if (indexes.length === 0) return;
      event.preventDefault();
      const current = indexes.findIndex(
        (index) => optionButtons.current[index] === active,
      );
      let next: number;
      if (event.key === "Home") {
        next = 0;
      } else if (event.key === "End") {
        next = indexes.length - 1;
      } else if (current < 0) {
        next = event.key === "ArrowDown" ? 0 : indexes.length - 1;
      } else {
        next =
          (current + (event.key === "ArrowDown" ? 1 : -1) + indexes.length) %
          indexes.length;
      }
      optionButtons.current[indexes[next]]?.focus();
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, options]);

  const onTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (disabled || open) return;
    if (
      event.key !== "ArrowDown" &&
      event.key !== "ArrowUp" &&
      event.key !== "Home" &&
      event.key !== "End"
    ) {
      return;
    }
    event.preventDefault();
    // The same keydown must not also reach the document-level walk that is
    // (re)armed for the listbox this handler is about to open.
    event.stopPropagation();
    if (event.key === "ArrowDown") openMenu("selected");
    else if (event.key === "ArrowUp" || event.key === "End") openMenu("last");
    else openMenu("first");
  };

  const choose = (option: SelectOption) => {
    if (option.disabled) return;
    onChange(option.value);
    closeMenu(true);
  };

  const surfaceStyle: CSSProperties = {
    ...(position ?? { left: 0, top: 0 }),
    ...(menuWidth === null ? {} : { width: menuWidth }),
    visibility: position === null ? "hidden" : undefined,
  };

  return (
    <div
      ref={root}
      className={`${css.root}${className ? ` ${className}` : ""}`}
    >
      <button
        ref={trigger}
        id={id}
        type="button"
        className={css.trigger}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => {
          if (open) closeMenu(false);
          else openMenu("selected");
        }}
        onKeyDown={onTriggerKeyDown}
      >
        <span className={css.triggerLabel}>{selected?.label ?? value}</span>
        <span
          className={`${css.chevron}${open ? ` ${css.chevronOpen}` : ""}`}
          aria-hidden="true"
        >
          <svg viewBox="0 0 16 16" width="14" height="14" fill="none">
            <path
              d="m4 6 4 4 4-4"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </span>
      </button>

      {open ? (
        <MenuSurface ref={panel} className={css.surface} style={surfaceStyle}>
          <div
            id={listboxId}
            role="listbox"
            aria-label={ariaLabel}
            className={css.viewport}
          >
            {options.map((option, index) => {
              const isSelected = option.value === value;
              return (
                <button
                  key={option.value}
                  ref={(node) => {
                    optionButtons.current[index] = node;
                  }}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  disabled={option.disabled}
                  tabIndex={-1}
                  className={`${css.option}${isSelected ? ` ${css.optionSelected}` : ""}`}
                  onClick={() => choose(option)}
                >
                  <span className={css.optionCopy}>
                    <span>{option.label}</span>
                    {option.description ? (
                      <small className={css.optionDescription}>
                        {option.description}
                      </small>
                    ) : null}
                  </span>
                  {isSelected ? (
                    <span className={css.check} aria-hidden="true">
                      ✓
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>
        </MenuSurface>
      ) : null}
    </div>
  );
}
