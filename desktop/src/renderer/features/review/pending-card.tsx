import type { ReactNode } from "react";

import type { DraftInlineAnchorDto } from "../../../shared/review.js";
import { SafeMarkdown } from "../../core/safe-markdown.js";

/**
 * One pending entry of the durable review draft, in the shape the diff renders
 * it. The anchor carries the stored `stale` flag, which the local editable
 * content drops, so the card is built from the entry rather than from the
 * content the composer saves.
 */
export interface PendingDraftEntry {
  readonly id: string;
  readonly kind: "general" | "inline" | "reply";
  readonly body: string;
  readonly anchor: DraftInlineAnchorDto | null;
  readonly threadId: string | null;
}

/** The line an inline anchor names on its own side, or null for neither. */
export function pendingAnchorLine(anchor: DraftInlineAnchorDto): number | null {
  return anchor.side === "old" ? anchor.old_line : anchor.new_line;
}

/**
 * Where the entry sits, worded exactly as the composer words the same anchor:
 * a range claims "Lines A to B (side)", a single line keeps "side line N".
 */
export function pendingAnchorLabel(anchor: DraftInlineAnchorDto): string {
  const line = pendingAnchorLine(anchor);
  const start = anchor.start_line;
  return start !== null && start !== line
    ? `Lines ${start} to ${line} (${anchor.side})`
    : `${anchor.side} line ${line}`;
}

/** How the card names an entry that has no diff anchor of its own. */
export function pendingEntryLabel(entry: PendingDraftEntry): string {
  if (entry.anchor) return pendingAnchorLabel(entry.anchor);
  return entry.kind === "reply" ? "reply" : "general comment";
}

/**
 * The S46 ribbon. The revision is the one the anchor was captured at, short
 * form, because that is the identity the reader compares against the diff
 * toolbar's own revision.
 */
export function staleRibbonText(anchor: DraftInlineAnchorDto): string {
  return `Stale, was line ${pendingAnchorLine(anchor)} at revision ${anchor.revision.head_sha.slice(0, 7)}`;
}

/**
 * A pending draft entry rendered where its anchor is, with the Pending badge
 * the design borrows from GitLab. Edit is withheld from a stale entry: the
 * anchor it holds must never be recaptured against the current revision, so
 * the only actions left on it are keeping it or deleting it.
 */
export function PendingCard({
  entry,
  reason,
  explain,
  busy,
  openExternal,
  edit,
  remove,
}: {
  readonly entry: PendingDraftEntry;
  /** Why this entry cannot be changed right now, or null when it can. */
  readonly reason: string | null;
  /**
   * Whether this card is the one that spells the refusal out. Every card's
   * controls carry it as a title, but one sentence repeated under every card
   * of a file says nothing the first one did not.
   */
  readonly explain: boolean;
  readonly busy: boolean;
  readonly openExternal: (url: string) => Promise<boolean>;
  readonly edit: () => void;
  readonly remove: () => void;
}): ReactNode {
  const stale = entry.anchor?.stale === true;
  const label = pendingEntryLabel(entry);
  return (
    <article
      className={`pending-card${stale ? " pending-card-stale" : ""}`}
      aria-label={`Pending review comment on ${label}`}
    >
      <header className="pending-card-heading">
        <span className="badge pending-badge">Pending</span>
        <strong className="pending-card-author">You</strong>
        <span className="pending-card-anchor">{label}</span>
        <div className="pending-card-actions">
          {!stale && (
            <button
              className="button button-secondary"
              aria-label={`Edit pending comment on ${label}`}
              disabled={busy || reason !== null}
              title={reason ?? undefined}
              onClick={edit}
            >
              Edit
            </button>
          )}
          <button
            className="button button-secondary"
            aria-label={`Delete pending comment on ${label}`}
            disabled={busy || reason !== null}
            title={reason ?? undefined}
            onClick={remove}
          >
            Delete
          </button>
        </div>
      </header>
      {stale && entry.anchor && (
        <p className="pending-card-ribbon" role="status">
          {staleRibbonText(entry.anchor)}
        </p>
      )}
      <div className="pending-card-body">
        <SafeMarkdown source={entry.body} openExternal={openExternal} />
      </div>
      {reason !== null && explain && <small>{reason}</small>}
    </article>
  );
}

/**
 * The spacer the other split pane renders in place of a card, so the two
 * independent pane grids stay on the same rows. It matches the card slot by
 * construction: both panes iterate the same entry list for the same row.
 */
export function PendingCardMirror(): ReactNode {
  return (
    <div className="pending-card-mirror" role="presentation" aria-hidden="true" />
  );
}
