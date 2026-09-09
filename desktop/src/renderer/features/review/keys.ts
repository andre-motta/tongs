import { useEffect, useRef } from "react";

/**
 * The review workflow's keyboard map (design section 2.8).
 *
 * The review surfaces are pages, not focusable controls, so a keystroke typed
 * with nothing focused is delivered to the document body and never bubbles
 * through the React tree. PR #178 settled the shape for the pipeline log: one
 * listener on the document, guarded by containment so it only claims keys
 * aimed at its own region, and by an `aria-modal` check so a dialog keeps the
 * keyboard even for a keystroke that landed on the body. Every review key
 * registers the same way, which is also what keeps the map off the
 * non-focusable containers the design forbids binding to.
 */

/** True when a keystroke belongs to the focused control rather than the view. */
export function isTextEntry(element: Element | null): boolean {
  if (element === null) return false;
  const tag = element.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    (element as HTMLElement).isContentEditable === true
  );
}

export interface ReviewKeyBinding {
  /** The `KeyboardEvent.key` this binding answers, matched exactly. */
  readonly key: string;
  /** Whether Shift must be held. A binding never fires with the wrong state. */
  readonly shift?: boolean;
  /** Whether Ctrl (or Cmd on macOS) must be held. */
  readonly primary?: boolean;
  /**
   * Carries the key out. Returning false means the surface had nothing to do
   * with it, so the keystroke is left alone rather than being swallowed: a
   * `n` with no threads on screen still reaches whatever else wants it.
   */
  readonly run: () => boolean;
}

interface KeyModifiers {
  readonly key: string;
  readonly shiftKey: boolean;
  readonly ctrlKey: boolean;
  readonly metaKey: boolean;
  readonly altKey: boolean;
}

/**
 * Exact modifier matching, in both directions: a binding with no modifier is
 * refused when one is held, and a binding that names one is refused without
 * it. Alt is never part of the map, so it always refuses.
 */
export function reviewKeyMatches(
  event: KeyModifiers,
  binding: ReviewKeyBinding,
): boolean {
  if (event.key !== binding.key) return false;
  if (event.altKey) return false;
  if (event.shiftKey !== (binding.shift ?? false)) return false;
  return binding.primary === true
    ? event.ctrlKey || event.metaKey
    : !event.ctrlKey && !event.metaKey;
}

/**
 * Registers `bindings` on the document that owns `region`.
 *
 * A key is claimed only when the keystroke landed on the document body or
 * inside `region`, no `aria-modal` dialog is on screen, the focused element is
 * not a text entry, the event has not already been answered by a React handler
 * closer to the target, and `standDown` does not hand the keyboard elsewhere.
 * The bindings are read through a ref, so a surface can rebuild them on every
 * render without the listener being torn down and re-added each time.
 */
export function useReviewKeyMap(
  region: Element | null,
  bindings: readonly ReviewKeyBinding[],
  standDown?: (owner: Document) => boolean,
): void {
  const latest = useRef<readonly ReviewKeyBinding[]>(bindings);
  const held = useRef<((owner: Document) => boolean) | undefined>(standDown);
  useEffect(() => {
    latest.current = bindings;
    held.current = standDown;
  });
  useEffect(() => {
    const owner = region?.ownerDocument;
    if (!region || !owner) return;
    const onKeyDown = (event: globalThis.KeyboardEvent): void => {
      // A React handler on the row, the composer or the drawer answers first
      // and calls `preventDefault`; the map never answers the same key twice.
      if (event.defaultPrevented) return;
      const target = event.target as Node | null;
      if (target === null) return;
      if (target !== owner.body && !region.contains(target)) return;
      // A modal dialog owns the keyboard even for a keystroke that landed on
      // the document body, so never act from behind one.
      if (owner.querySelector('[aria-modal="true"]') !== null) return;
      const element = target.nodeType === 1 ? (target as Element) : null;
      if (isTextEntry(owner.activeElement) || isTextEntry(element)) return;
      if (held.current?.(owner) === true) return;
      for (const binding of latest.current) {
        if (!reviewKeyMatches(event, binding)) continue;
        if (!binding.run()) continue;
        event.preventDefault();
        return;
      }
    };
    owner.addEventListener("keydown", onKeyDown);
    return () => owner.removeEventListener("keydown", onKeyDown);
  }, [region]);
}

/**
 * The composer's primary write, as the mouse reaches it: the one button in the
 * composer's write row that is not a secondary action and not the destructive
 * one behind the overflow.
 *
 * Ctrl/Cmd+Enter typed inside the composer is answered by the composer's own
 * handler, which owns the text being collected. Typed from the diff behind it,
 * the key has to reach the same write without that state, so it presses the
 * button rather than reaching into the composer: the button already carries
 * every refusal the write has, so a press the composer would refuse is refused
 * here too, and no second copy of the rules is kept.
 */
export const COMPOSER_PRIMARY_SELECTOR =
  ".inline-composer .inline-composer-writes button.button:not(.button-secondary):not(.button-danger)";

export function pressComposerPrimary(region: Element | null): boolean {
  const button =
    region?.querySelector<HTMLButtonElement>(COMPOSER_PRIMARY_SELECTOR) ?? null;
  if (button === null || button.disabled) return false;
  button.click();
  return true;
}

/** The attribute the diff stamps on every row `n` and `p` step through. */
export const REVIEW_ROW_ATTRIBUTE = "data-review-row";

/** The row key for a published discussion, and for a pending draft entry. */
export function threadRowKey(discussionId: string): string {
  return `thread:${discussionId}`;
}

export function pendingRowKey(entryId: string): string {
  return `pending:${entryId}`;
}

/** The discussion a row key names, or null when the row is a pending card. */
export function threadRowDiscussion(key: string | null): string | null {
  return key !== null && key.startsWith("thread:") ? key.slice(7) : null;
}

export interface ReviewRowStep {
  /** The row the step landed on, as its `data-review-row` key. */
  readonly key: string | null;
  readonly moved: boolean;
}

/**
 * Steps `n` and `p` through the threads and pending cards a container renders,
 * in DOM order, wrapping at both ends. The cursor is taken from the focused
 * element when the focus is already on a row, and from `remembered` otherwise,
 * so a step still continues from where the last one left off after the reader
 * has clicked away.
 */
export function stepReviewRow(
  container: Element | null,
  direction: 1 | -1,
  remembered: string | null,
): ReviewRowStep {
  const rows =
    container === null
      ? []
      : [
          ...container.querySelectorAll<HTMLElement>(
            `[${REVIEW_ROW_ATTRIBUTE}]`,
          ),
        ];
  if (rows.length === 0) return { key: remembered, moved: false };
  const active = container?.ownerDocument.activeElement ?? null;
  const focused =
    active === null ? -1 : rows.findIndex((row) => row.contains(active));
  const at =
    focused >= 0
      ? focused
      : rows.findIndex(
          (row) => row.getAttribute(REVIEW_ROW_ATTRIBUTE) === remembered,
        );
  const index =
    at < 0
      ? direction === 1
        ? 0
        : rows.length - 1
      : (at + direction + rows.length) % rows.length;
  const next = rows[index];
  if (!next) return { key: remembered, moved: false };
  const focusable =
    next.querySelector<HTMLElement>("button:not([disabled]), [tabindex]") ??
    next;
  focusable.focus();
  // jsdom has no layout, so the scroll is asked for only where it exists.
  if (typeof next.scrollIntoView === "function")
    next.scrollIntoView({ block: "nearest" });
  return { key: next.getAttribute(REVIEW_ROW_ATTRIBUTE), moved: true };
}

/** The row the focus sits in, as its key, or null when the focus is elsewhere. */
export function focusedReviewRow(container: Element | null): string | null {
  const active = container?.ownerDocument.activeElement ?? null;
  if (container === null || active === null) return null;
  const rows = [
    ...container.querySelectorAll<HTMLElement>(`[${REVIEW_ROW_ATTRIBUTE}]`),
  ];
  const row = rows.find((candidate) => candidate.contains(active)) ?? null;
  return row === null ? null : row.getAttribute(REVIEW_ROW_ATTRIBUTE);
}

export interface ReviewKeyRow {
  /** The desktop key, written the way the documentation tables write it. */
  readonly key: string;
  readonly desktop: string;
  /** The terminal interface's key for the same intent, or the divergence. */
  readonly tui: string;
}

/**
 * The parity table both documentation pages publish, kept here so the two
 * pages cannot drift from each other. The terminal column names the binding
 * actually declared in `src/tongs`, including where the desktop deliberately
 * differs.
 */
export const REVIEW_KEY_PARITY: readonly ReviewKeyRow[] = Object.freeze([
  {
    key: "`c`",
    desktop: "Comment on the focused row or the current selection",
    tui: "`c` (diff viewer, MR detail)",
  },
  {
    key: "`Shift+C`",
    desktop: "Open **Your review**",
    tui: "`Ctrl+G` (MR detail)",
  },
  {
    key: "`]` / `[`",
    desktop: "Next / previous changed file, wrapping",
    tui: "`n` / `Shift+N` (diff viewer)",
  },
  {
    key: "`n` / `p`",
    desktop: "Next / previous thread or pending comment, wrapping",
    tui: "`]` / `[` (diff viewer, next / previous comment)",
  },
  {
    key: "`r`",
    desktop: "Reply to the focused thread",
    tui: "`r` (diff viewer, discussion tab)",
  },
  {
    key: "`Ctrl+Enter` / `Cmd+Enter`",
    desktop: "Primary composer action",
    tui: "`Ctrl+S` (comment editor)",
  },
  {
    key: "`Esc`",
    desktop: "Close the composer and keep the text",
    tui: "`Esc` (comment editor, cancel)",
  },
  {
    key: "`v`",
    desktop: "Cycle the verdict in **Your review**",
    tui: "`v` (review draft)",
  },
]);

/** The parity table as the Markdown both documentation pages carry. */
export function reviewKeyParityTable(): string {
  const rows = REVIEW_KEY_PARITY.map(
    (row) => `| ${row.key} | ${row.desktop} | ${row.tui} |`,
  );
  return [
    "| Key | Desktop | Terminal interface |",
    "|-----|---------|--------------------|",
    ...rows,
  ].join("\n");
}
