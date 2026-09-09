import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import type { ReviewRevisionDto } from "../../../shared/bridge.js";
import type {
  DraftCommentInputDto,
  DraftContentInputDto,
  DraftInlineAnchorDto,
  DraftInlineAnchorInputDto,
  DraftSnapshotDto,
  InlineCommentParams,
  MutationDiffAnchorDto,
  ReviewDesktopBridge,
  ReviewMutationCapabilitiesDto,
} from "../../../shared/review.js";
import { reviewMutationMessage } from "../../../shared/review.js";
import type {
  InlineAnchorSelection,
  InlineSelectedLine,
} from "../../core/navigation.js";
import { safeError } from "../../core/presentation.js";
import { SafeMarkdown } from "../../core/safe-markdown.js";
import {
  pendingAnchorLabel,
  type PendingDraftEntry,
} from "./pending-card.js";
import {
  containsSuggestionBlock,
  prepareSuggestionTarget,
  suggestionDisabledReason,
  suggestionPrefill,
  type SuggestionForge,
} from "./suggestion.js";
import {
  adoptDraft,
  beginDraftSave,
  beginQuickIntent,
  canCaptureDraftInline,
  captureDraftAnchor,
  conflictDraftSave,
  createReviewWorkflowState,
  editDraft,
  failDraftSave,
  finishDraftSave,
  markQuickIntentUncertain,
  rejectQuickIntent,
  settleQuickIntent,
  type DraftAnchorSelection,
  type ReviewWorkflowState,
} from "./state.js";

export const ACTIVE_DRAFT_STATES = [
  "editable",
  "submitting",
  "partial",
  "unknown",
] as const;

/**
 * Unsent composer text, kept per review and per anchor identity. The in-diff
 * composer and the review workflow panel share this store so that closing a
 * composer returns the same text to the same review and the same anchor, and
 * never to another one.
 */
export interface ComposerBuffers {
  general: string;
  readonly inline: Map<string, string>;
  readonly replies: Map<string, string>;
  /**
   * Unsent text of an edit in progress, kept per pending entry rather than per
   * anchor: an entry and a new comment can share one anchor, and the text of
   * one must never reach the other.
   */
  readonly edits: Map<string, string>;
  readonly suggestions: Map<
    string,
    { readonly comment: string; readonly replacement: string }
  >;
}

const composerCache = new Map<string, ComposerBuffers>();
const workflowCache = new Map<string, ReviewWorkflowState>();

export function buffersFor(review: string): ComposerBuffers {
  let buffers = composerCache.get(review);
  if (!buffers) {
    buffers = {
      general: "",
      inline: new Map(),
      replies: new Map(),
      edits: new Map(),
      suggestions: new Map(),
    };
    composerCache.set(review, buffers);
  }
  return buffers;
}

export function anchorIdentity(
  anchor: InlineAnchorSelection | null,
): string | null {
  if (!anchor) return null;
  return JSON.stringify({
    review: anchor.review,
    revision: anchor.revision,
    oldPath: anchor.oldPath,
    newPath: anchor.newPath,
    side: anchor.side,
    oldLine: anchor.oldLine,
    newLine: anchor.newLine,
    contextLines: anchor.contextLines,
    contextComplete: anchor.contextComplete,
    rangeOriginOldLine: anchor.rangeOriginOldLine,
    rangeOriginNewLine: anchor.rangeOriginNewLine,
    selectedLines: anchor.selectedLines,
  });
}

export function inlineBufferLabel(key: string): string {
  try {
    const value = JSON.parse(key) as {
      readonly newPath?: unknown;
      readonly side?: unknown;
      readonly oldLine?: unknown;
      readonly newLine?: unknown;
      readonly revision?: { readonly head_sha?: unknown };
    };
    const line = value.side === "old" ? value.oldLine : value.newLine;
    return `${String(value.newPath)} · ${String(value.side)} line ${String(line)} · revision ${String(value.revision?.head_sha)}`;
  } catch {
    return "Earlier inline selection";
  }
}

export function readInlineBuffer(
  review: string,
  anchor: InlineAnchorSelection | null,
): string {
  const key = anchorIdentity(anchor);
  return key === null ? "" : (buffersFor(review).inline.get(key) ?? "");
}

export function writeInlineBuffer(
  review: string,
  anchor: InlineAnchorSelection | null,
  body: string,
): void {
  const key = anchorIdentity(anchor);
  if (key !== null) buffersFor(review).inline.set(key, body);
}

export function clearInlineBuffer(
  review: string,
  anchor: InlineAnchorSelection | null,
): void {
  const key = anchorIdentity(anchor);
  if (key !== null) buffersFor(review).inline.delete(key);
}

/** The retained edit text for a pending entry, or null when none is kept. */
export function readEditBuffer(review: string, entryId: string): string | null {
  return buffersFor(review).edits.get(entryId) ?? null;
}

export function writeEditBuffer(
  review: string,
  entryId: string,
  body: string,
): void {
  buffersFor(review).edits.set(entryId, body);
}

export function clearEditBuffer(review: string, entryId: string): void {
  buffersFor(review).edits.delete(entryId);
}

export function cachedWorkflow(review: string): ReviewWorkflowState | null {
  return workflowCache.get(review) ?? null;
}

export function cacheWorkflow(
  review: string,
  state: ReviewWorkflowState,
): void {
  workflowCache.set(review, state);
}

/**
 * The two ends of a multi-line selection, ordered the way every layer below
 * the composer reads a range: `first` is the line the range opens on and is
 * carried as `start_line`, `last` is the line the anchor itself names. A
 * single-line selection has no range.
 */
function rangeEndpoints(selection: InlineAnchorSelection): {
  readonly first: InlineSelectedLine;
  readonly last: InlineSelectedLine;
} | null {
  const lines = selection.selectedLines ?? [];
  const opening = lines[0];
  const closing = lines.at(-1);
  if (lines.length < 2 || !opening || !closing) return null;
  const open = sideLine(selection.side, opening);
  const close = sideLine(selection.side, closing);
  if (open === null || close === null || open === close) return null;
  return open < close
    ? Object.freeze({ first: opening, last: closing })
    : Object.freeze({ first: closing, last: opening });
}

function sideLine(
  side: "old" | "new",
  line: InlineSelectedLine,
): number | null {
  return side === "old" ? line.oldLine : line.newLine;
}

/** The span a selection covers on its own side, or null for a single line. */
export function anchorRange(selection: InlineAnchorSelection): {
  readonly startLine: number;
  readonly endLine: number;
} | null {
  const range = rangeEndpoints(selection);
  if (!range) return null;
  const startLine = sideLine(selection.side, range.first);
  const endLine = sideLine(selection.side, range.last);
  return startLine === null || endLine === null
    ? null
    : Object.freeze({ startLine, endLine });
}

export function mutationAnchor(
  selection: InlineAnchorSelection,
): MutationDiffAnchorDto {
  const range = rangeEndpoints(selection);
  const side = selection.side === "old" ? "LEFT" : "RIGHT";
  const line = range
    ? sideLine(selection.side, range.last)
    : anchorLine(selection);
  const start = range ? sideLine(selection.side, range.first) : null;
  if (line === null)
    throw new Error("The selected diff side has no source line");
  return Object.freeze({
    old_path: selection.oldPath,
    new_path: selection.newPath,
    line,
    side,
    ...(start === null ? {} : { start_line: start, start_side: side }),
  });
}

export function draftSelection(
  selection: InlineAnchorSelection,
): DraftAnchorSelection {
  const range = rangeEndpoints(selection);
  const start = range ? sideLine(selection.side, range.first) : null;
  return Object.freeze({
    review: selection.review,
    revision: selection.revision,
    oldPath: selection.oldPath,
    newPath: selection.newPath,
    side: selection.side,
    oldLine: range ? range.last.oldLine : selection.oldLine,
    newLine: range ? range.last.newLine : selection.newLine,
    startLine: start,
    startSide: start === null ? null : selection.side,
    contextLines: selection.contextLines,
    contextComplete: selection.contextComplete,
  });
}

/**
 * A body carrying a suggestion block is anchored by the suggestion rules,
 * which differ per forge: GitHub anchors the range and replaces it, GitLab
 * anchors the first line and encodes the span in the fence. Anything else is
 * anchored by the selection, range included.
 */
function suggestionAware(
  anchor: InlineAnchorSelection,
  forge: SuggestionForge | null,
  body: string,
): boolean {
  return (
    forge !== null &&
    containsSuggestionBlock(body) &&
    suggestionDisabledReason(anchor, forge) === null
  );
}

function anchorSelectionFor(
  anchor: InlineAnchorSelection,
  forge: SuggestionForge | null,
  body: string,
): DraftAnchorSelection {
  return suggestionAware(anchor, forge, body)
    ? prepareSuggestionTarget(anchor, forge!).draftSelection
    : draftSelection(anchor);
}

function mutationAnchorFor(
  anchor: InlineAnchorSelection,
  forge: SuggestionForge | null,
  body: string,
): MutationDiffAnchorDto {
  return suggestionAware(anchor, forge, body)
    ? prepareSuggestionTarget(anchor, forge!).mutationAnchor
    : mutationAnchor(anchor);
}

export function anchorLine(selection: InlineAnchorSelection): number | null {
  return selection.side === "old" ? selection.oldLine : selection.newLine;
}

/**
 * Header text of the composer. A range claims the span the design words as
 * "Lines A to B (new)"; a single line keeps the wording card #188 shipped.
 */
export function anchorLabel(selection: InlineAnchorSelection): string {
  const range = anchorRange(selection);
  const span = range
    ? `Lines ${range.startLine} to ${range.endLine} (${selection.side})`
    : `${selection.side} line ${anchorLine(selection)}`;
  return `${selection.newPath}, ${span}`;
}

/**
 * The one sentence a forge without `multiline_comment` gets. Both writes carry
 * the same range anchor, so both refuse for the same reason.
 */
export const MULTILINE_REFUSAL =
  "Multi-line comments are unsupported for this review. Select a single line.";

/**
 * A refusal the composer itself authored. Its text is written for the person
 * using the app, so it is shown verbatim instead of being folded into the
 * generic read-failure sentence that `safeError` reports for an untyped error.
 */
export class ComposerRefusal extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ComposerRefusal";
  }
}

/** The sentence to show for a failed composer action. */
export function composerFailureMessage(value: unknown): string {
  return value instanceof ComposerRefusal
    ? value.message
    : reviewMutationError(value);
}

export function reviewMutationError(value: unknown): string {
  if (
    value !== null &&
    typeof value === "object" &&
    "code" in value &&
    typeof value.code === "string"
  ) {
    return reviewMutationMessage(value.code);
  }
  return safeError(value);
}

export function isUncertainError(value: unknown): boolean {
  if (value === null || typeof value !== "object") return false;
  const code =
    "code" in value && typeof value.code === "string" ? value.code : "";
  return (
    code === "invalid_response" ||
    code === "mutation_timeout" ||
    code === "request_cancelled" ||
    code === "unexpected_eof" ||
    code === "write_failed"
  );
}

export function isConflictError(value: unknown): boolean {
  if (value === null || typeof value !== "object") return false;
  return "code" in value && value.code === "conflict";
}

export function newOperationId(kind: string): string {
  return `desktop:${kind}:${crypto.randomUUID()}`;
}

export function sameComposerRevision(
  left: ReviewRevisionDto,
  right: ReviewRevisionDto,
): boolean {
  return (
    left.head_sha === right.head_sha &&
    left.base_sha === right.base_sha &&
    left.start_sha === right.start_sha
  );
}

export function Composer({
  label,
  body,
  setBody,
  disabled,
  disabledReason,
  submit,
}: {
  readonly label: string;
  readonly body: string;
  readonly setBody: (value: string) => void;
  readonly disabled: boolean;
  readonly disabledReason: string | null;
  readonly submit: () => void;
}): ReactNode {
  return (
    <section className="review-workflow-composer">
      <label>
        <strong>{label}</strong>
        <textarea
          value={body}
          disabled={disabled}
          title={disabledReason ?? undefined}
          onChange={(event) => setBody(event.target.value)}
          onKeyDown={(event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === "Enter")
              submit();
          }}
        />
      </label>
      {disabledReason && <small>{disabledReason}</small>}
      <button className="button" disabled={disabled || !body} onClick={submit}>
        {label}
      </button>
    </section>
  );
}

export function BufferedInlineNotes({
  entries,
  currentKey,
}: {
  readonly entries: ReadonlyMap<string, string>;
  readonly currentKey: string | null;
}): ReactNode {
  const retained = [...entries.entries()].filter(
    ([key, body]) => key !== currentKey && body.length > 0,
  );
  if (retained.length === 0) return null;
  return (
    <section className="review-workflow-buffered-inline">
      <strong>Unsent inline text kept on earlier selections</strong>
      {retained.map(([key, body]) => (
        <article key={key}>
          <small>{inlineBufferLabel(key)}</small>
          <p>{body}</p>
        </article>
      ))}
    </section>
  );
}

/**
 * The two writes the in-diff composer offers, over the paths that already
 * exist: a durable review draft entry through `save_draft`, and the immediate
 * inline mutation. Both surfaces share one workflow state per review, so a
 * review started on the diff is the same review the workflow panel edits.
 */
export interface InlineComposerBridge extends ReviewDesktopBridge {
  cancelRead(requestToken: string): Promise<boolean>;
  openExternal(url: string): Promise<boolean>;
}

export interface InlineComposerController {
  readonly review: string;
  readonly forge: SuggestionForge | null;
  readonly pendingReview: boolean;
  readonly pendingCount: number;
  /**
   * Every entry of the pending review, in stored order, with the anchor the
   * store holds including its `stale` flag.
   */
  readonly pending: readonly PendingDraftEntry[];
  /** Why a pending entry cannot be edited or deleted now, or null when it can. */
  readonly entryReason: string | null;
  readonly busy: boolean;
  readonly message: string | null;
  readonly quickReason: (anchor: InlineAnchorSelection) => string | null;
  readonly draftReason: (anchor: InlineAnchorSelection) => string | null;
  /** Why Insert suggestion cannot act on this anchor and this body. */
  readonly suggestionReason: (
    anchor: InlineAnchorSelection,
    body: string,
  ) => string | null;
  /** The pre-filled suggestion body, or null when the rules refuse it. */
  readonly insertSuggestion: (
    anchor: InlineAnchorSelection,
    body: string,
  ) => string | null;
  readonly addToReview: (
    anchor: InlineAnchorSelection,
    body: string,
  ) => Promise<boolean>;
  readonly commentNow: (
    anchor: InlineAnchorSelection,
    body: string,
  ) => Promise<boolean>;
  /** Replaces one pending entry's body in place, keeping its id and anchor. */
  readonly updateEntry: (entryId: string, body: string) => Promise<boolean>;
  /** Removes one pending entry from the draft and saves what is left. */
  readonly removeEntry: (entryId: string) => Promise<boolean>;
  readonly openExternal: (url: string) => Promise<boolean>;
  readonly clearMessage: () => void;
}

export function useInlineReviewComposer(
  bridge: InlineComposerBridge,
  review: string,
  revision: ReviewRevisionDto,
  forge: SuggestionForge | null = null,
): InlineComposerController {
  const [workflow, setWorkflow] = useState<ReviewWorkflowState>(
    () => cachedWorkflow(review) ?? createReviewWorkflowState(review, revision),
  );
  const held = useRef(workflow);
  const [capabilities, setCapabilities] =
    useState<ReviewMutationCapabilitiesDto | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const apply = useCallback(
    (
      change: (current: ReviewWorkflowState) => ReviewWorkflowState,
    ): ReviewWorkflowState => {
      const next = change(held.current);
      held.current = next;
      cacheWorkflow(review, next);
      setWorkflow(next);
      return next;
    },
    [review],
  );
  useEffect(() => {
    const current = held.current;
    const durable =
      current.draft.remote !== null ||
      current.quick !== null ||
      current.submission.progress !== null;
    if (
      current.displayed.review === review &&
      (durable ||
        sameComposerRevision(current.displayed.latestObservedRevision, revision))
    ) {
      cacheWorkflow(review, current);
      return;
    }
    const next =
      (current.displayed.review === review ? null : cachedWorkflow(review)) ??
      createReviewWorkflowState(review, revision);
    held.current = next;
    cacheWorkflow(review, next);
    setWorkflow(next);
  }, [review, revision.base_sha, revision.head_sha, revision.start_sha]);
  useEffect(() => {
    let live = true;
    let token: string | null = null;
    try {
      const read = bridge.getReviewMutationCapabilities(review);
      token = read.requestToken;
      void read.result.then(
        (result) => {
          if (live) setCapabilities(result.capabilities);
        },
        () => {
          if (live) setCapabilities(null);
        },
      );
    } catch {
      setCapabilities(null);
    }
    return () => {
      live = false;
      cancelQuietly(bridge, token);
    };
  }, [bridge, review]);

  // A durable draft written by the TUI, by another window or by an earlier run
  // is the same pending review this composer writes to. Reading it once on
  // mount is what makes the chip, the primary label and the pending cards
  // describe the state before the first press instead of after it. Exactly one
  // active draft is adopted: several are a choice the review workflow owns, and
  // `bindDraft` still refuses them in its own words on the first write.
  useEffect(() => {
    let live = true;
    let token: string | null = null;
    const current = held.current;
    if (current.draft.remote !== null || current.draft.dirty) return;
    try {
      const read = bridge.listReviewDrafts({
        review,
        states: ACTIVE_DRAFT_STATES,
        max_items: 100,
      });
      token = read.requestToken;
      void read.result.then(
        (result) => {
          const draft = result.drafts[0];
          if (!live || result.drafts.length !== 1 || !draft) return;
          const latest = held.current;
          if (
            latest.draft.remote !== null ||
            latest.draft.dirty ||
            latest.displayed.review !== draft.review
          )
            return;
          apply((state) => adoptDraft(state, draft));
        },
        () => undefined,
      );
    } catch {
      // A read that cannot even be dispatched leaves the composer in the
      // no-draft state it already holds; the first write recovers it.
    }
    return () => {
      live = false;
      cancelQuietly(bridge, token);
    };
  }, [apply, bridge, review]);

  const clearMessage = useCallback(() => setMessage(null), []);
  const quickBlocked =
    workflow.quick?.status === "sending" || workflow.quick?.status === "unknown";
  const quickReason = useCallback(
    (anchor: InlineAnchorSelection): string | null => {
      if (capabilities === null)
        return "Inline comment support for this review is still loading.";
      if (!capabilities.inline_comment)
        return "Inline comments are unsupported for this review.";
      if (anchorRange(anchor) !== null && !capabilities.multiline_comment)
        return MULTILINE_REFUSAL;
      if (anchor.review !== review)
        return "Select a source line in the current review diff.";
      if (quickBlocked)
        return "Resolve or acknowledge the previous action in the review workflow before another mutation.";
      if (busy) return "A review write is already in flight.";
      if (anchorLine(anchor) === null)
        return "The selected diff side has no source line.";
      return null;
    },
    [busy, capabilities, quickBlocked, review],
  );
  const draftReason = useCallback(
    (anchor: InlineAnchorSelection): string | null => {
      const shared = quickReason(anchor);
      if (shared !== null) return shared;
      if (!anchor.contextComplete)
        return "The selected context is partial. Refresh the complete diff before drafting.";
      if (
        !sameComposerRevision(
          anchor.revision,
          workflow.displayed.latestObservedRevision,
        )
      )
        return "The selected code belongs to an earlier revision. Refresh the diff.";
      if (workflow.draft.conflict)
        return "This pending review was changed elsewhere. Resolve the conflict in the review workflow before adding inline feedback.";
      if (workflow.draft.remote && !canCaptureDraftInline(workflow))
        return boundElsewhereRefusal(workflow);
      return null;
    },
    [quickReason, workflow],
  );

  const suggestionReason = useCallback(
    (anchor: InlineAnchorSelection, body: string): string | null => {
      if (capabilities === null)
        return "Inline comment support for this review is still loading.";
      if (!capabilities.inline_comment)
        return "Inline comments are unsupported for this review.";
      if (anchorRange(anchor) !== null && !capabilities.multiline_comment)
        return MULTILINE_REFUSAL;
      const rules = suggestionDisabledReason(anchor, forge);
      if (rules !== null) return rules;
      if (containsSuggestionBlock(body))
        return "This comment already carries a suggestion block.";
      return null;
    },
    [capabilities, forge],
  );

  const insertSuggestion = useCallback(
    (anchor: InlineAnchorSelection, body: string): string | null => {
      const refusal = suggestionReason(anchor, body);
      if (refusal !== null || forge === null) {
        setMessage(refusal ?? "The selected review repository is unavailable.");
        return null;
      }
      try {
        return suggestionPrefill(anchor, forge, body);
      } catch {
        // The rules just passed, so a throw here means the selection moved
        // under the press. Its own text is an invariant, not a sentence for
        // the reader.
        setMessage(
          "The selection changed while the suggestion was prepared. Reselect the lines.",
        );
        return null;
      }
    },
    [forge, suggestionReason],
  );

  const bindDraft = useCallback(
    async (anchor: InlineAnchorSelection): Promise<ReviewWorkflowState> => {
      const current = held.current;
      if (current.draft.remote) return current;
      const result = await bridge.listReviewDrafts({
        review,
        states: ACTIVE_DRAFT_STATES,
        max_items: 100,
      }).result;
      if (result.drafts.length > 1)
        throw new ComposerRefusal(
          "Several pending reviews were recovered. Resume one in the review workflow before commenting.",
        );
      const recovered = result.drafts[0];
      const draft =
        recovered ??
        (await bridge.createReviewDraft({
          review,
          revision: anchor.revision,
        }));
      const bound = apply((state) => adoptDraft(state, draft));
      if (!canCaptureDraftInline(bound))
        throw new ComposerRefusal(boundElsewhereRefusal(bound));
      return bound;
    },
    [apply, bridge, review],
  );

  const saveDraft = useCallback(async (): Promise<void> => {
    const current = apply(beginDraftSave);
    const remote = current.draft.remote;
    const pending = current.draft.pendingSave;
    if (!remote || !pending)
      throw new ComposerRefusal(
        "The pending review is not ready to save. Settle it in the review workflow before adding inline feedback.",
      );
    try {
      const saved = await bridge.saveReviewDraft({
        review,
        draft_id: remote.id,
        expected_version: pending.expectedVersion,
        content: pending.content,
      });
      apply((latest) => finishDraftSave(latest, saved));
    } catch (failure) {
      const conflicting = isConflictError(failure)
        ? await recoverDraft(bridge, review, remote.id)
        : null;
      if (conflicting)
        apply((latest) => conflictDraftSave(latest, conflicting));
      else apply(failDraftSave);
      throw failure;
    }
  }, [apply, bridge, review]);

  /**
   * Undoes the local change a failed save was carrying. When the draft was
   * clean before it, the restored content is the remote snapshot itself, so
   * `adoptDraft` restores it without leaving `dirty` set on content nothing
   * changed; a draft that was already dirty keeps its own edits and its flag.
   * A refusal is reported rather than swallowed: the entry the save left
   * behind is what an obvious retry would write a second time.
   */
  const rollBack = useCallback(
    (
      remote: DraftSnapshotDto | null,
      wasDirty: boolean,
      content: DraftContentInputDto,
    ): boolean => {
      try {
        apply((current) => {
          const bound = current.draft.remote;
          // A recorded conflict has already replaced the bound snapshot, and
          // the local text is what "Keep my text" would re-save, so that state
          // is restored by content and left standing.
          return remote &&
            !wasDirty &&
            !current.draft.conflict &&
            bound?.id === remote.id &&
            bound.version === remote.version
            ? adoptDraft(current, remote)
            : editDraft(current, content);
        });
        return true;
      } catch {
        return false;
      }
    },
    [apply],
  );

  /**
   * The sentence that stands in for a pending entry's Edit and Delete. It
   * repeats the wording the draft path already uses for the same states, so a
   * card and the composer above it never explain the same refusal twice over.
   */
  const entryReason = useMemo((): string | null => {
    if (workflow.draft.remote === null)
      return "No pending review holds this comment.";
    if (workflow.draft.conflict)
      return "This pending review was changed elsewhere. Resolve the conflict in the review workflow before changing pending comments.";
    if (!canCaptureDraftInline(workflow))
      return boundElsewhereEntryRefusal(workflow);
    return null;
  }, [workflow]);

  const pending = useMemo((): readonly PendingDraftEntry[] => {
    const remote = workflow.draft.remote;
    if (!remote) return Object.freeze([]);
    // Staleness is stored, and the editable local content drops it, so the
    // flag is read back from the snapshot by comment ID. An entry this session
    // has just added is not in the snapshot yet and is never stale.
    const stored = new Map(remote.comments.map((entry) => [entry.id, entry]));
    return Object.freeze(
      workflow.draft.local.comments.map((comment) => {
        const saved = stored.get(comment.id);
        return Object.freeze({
          id: comment.id,
          kind: comment.kind,
          body: comment.body,
          anchor:
            comment.kind === "inline"
              ? pendingAnchor(
                  comment.anchor,
                  saved?.kind === "inline" && saved.anchor.stale,
                )
              : null,
          threadId: comment.kind === "reply" ? comment.thread_id : null,
        });
      }),
    );
  }, [workflow.draft.local, workflow.draft.remote]);

  /**
   * Every change to an existing entry takes the same route as adding one: the
   * shared draft is edited, saved, and put back exactly as it was when the
   * save fails, so a failed edit or delete never leaves the diff describing a
   * review the store does not hold.
   */
  const mutateEntries = useCallback(
    async (
      change: (
        comments: readonly DraftCommentInputDto[],
      ) => readonly DraftCommentInputDto[],
    ): Promise<boolean> => {
      if (entryReason !== null) {
        setMessage(entryReason);
        return false;
      }
      if (busy) {
        setMessage("A review write is already in flight.");
        return false;
      }
      setBusy(true);
      setMessage(null);
      const bound = held.current;
      const restored = bound.draft.local;
      const wasDirty = bound.draft.dirty;
      try {
        apply((current) =>
          editDraft(current, {
            ...current.draft.local,
            comments: change(current.draft.local.comments),
          }),
        );
        try {
          await saveDraft();
        } catch (failure) {
          if (!rollBack(bound.draft.remote, wasDirty, restored))
            throw new ComposerRefusal(
              `${composerFailureMessage(failure)} ${ENTRY_ROLLBACK_REFUSAL}`,
            );
          throw failure;
        }
        return true;
      } catch (failure) {
        setMessage(composerFailureMessage(failure));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [apply, busy, entryReason, rollBack, saveDraft],
  );

  const updateEntry = useCallback(
    async (entryId: string, body: string): Promise<boolean> => {
      if (body.length === 0) return false;
      const existing = held.current.draft.local.comments.find(
        (comment) => comment.id === entryId,
      );
      if (!existing) {
        setMessage(ENTRY_GONE_REFUSAL);
        return false;
      }
      // An edit that changed nothing is not a version of the review.
      if (existing.body === body) return true;
      return mutateEntries((comments) =>
        comments.map((comment) =>
          comment.id === entryId ? { ...comment, body } : comment,
        ),
      );
    },
    [mutateEntries],
  );

  const removeEntry = useCallback(
    async (entryId: string): Promise<boolean> => {
      const existing = held.current.draft.local.comments.some(
        (comment) => comment.id === entryId,
      );
      if (!existing) {
        setMessage(ENTRY_GONE_REFUSAL);
        return false;
      }
      return mutateEntries((comments) =>
        comments.filter((comment) => comment.id !== entryId),
      );
    },
    [mutateEntries],
  );

  const addToReview = useCallback(
    async (anchor: InlineAnchorSelection, body: string): Promise<boolean> => {
      if (body.length === 0) return false;
      const refusal = draftReason(anchor);
      if (refusal !== null) {
        setMessage(refusal);
        return false;
      }
      setBusy(true);
      setMessage(null);
      try {
        const captured = await captureAnchor(anchor, forge, body);
        const bound = await bindDraft(anchor);
        const comment: DraftCommentInputDto = Object.freeze({
          id: crypto.randomUUID(),
          kind: "inline",
          body,
          anchor: captured,
        });
        // The entry is added to the shared draft only for the duration of the
        // save. A failed save keeps the local content dirty, so leaving the
        // entry behind would let the obvious retry write the same comment
        // twice at the same anchor.
        const restored = bound.draft.local;
        const wasDirty = bound.draft.dirty;
        apply((current) =>
          editDraft(current, {
            ...current.draft.local,
            comments: [...current.draft.local.comments, comment],
          }),
        );
        try {
          await saveDraft();
        } catch (failure) {
          if (!rollBack(bound.draft.remote, wasDirty, restored))
            throw new ComposerRefusal(
              `${composerFailureMessage(failure)} ${ENTRY_ROLLBACK_REFUSAL}`,
            );
          throw failure;
        }
        clearInlineBuffer(review, anchor);
        return true;
      } catch (failure) {
        setMessage(composerFailureMessage(failure));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [apply, bindDraft, draftReason, forge, review, rollBack, saveDraft],
  );

  const commentNow = useCallback(
    async (anchor: InlineAnchorSelection, body: string): Promise<boolean> => {
      if (body.length === 0) return false;
      const refusal = quickReason(anchor);
      if (refusal !== null) {
        setMessage(refusal);
        return false;
      }
      setBusy(true);
      setMessage(null);
      const operationId = newOperationId("comment");
      try {
        const command: InlineCommentParams = {
          operation_id: operationId,
          review,
          revision: anchor.revision,
          anchor: mutationAnchorFor(anchor, forge, body),
          body,
        };
        apply((current) => beginQuickIntent(current, operationId, command));
        try {
          const outcome = await bridge.postInlineReviewComment(command);
          const settled = apply((current) =>
            settleQuickIntent(current, operationId, outcome),
          );
          if (outcome.outcome === "known") {
            clearInlineBuffer(review, anchor);
            return true;
          }
          setMessage(settled.quick?.message ?? "The remote result is unknown.");
          return false;
        } catch (failure) {
          const settled = isUncertainError(failure)
            ? apply((current) =>
                markQuickIntentUncertain(current, operationId),
              )
            : apply((current) =>
                rejectQuickIntent(
                  current,
                  operationId,
                  reviewMutationError(failure),
                ),
              );
          setMessage(settled.quick?.message ?? reviewMutationError(failure));
          return false;
        }
      } catch (failure) {
        setMessage(composerFailureMessage(failure));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [apply, bridge, forge, quickReason, review],
  );

  const openExternal = useCallback(
    (url: string): Promise<boolean> => bridge.openExternal(url),
    [bridge],
  );

  return {
    review,
    forge,
    pendingReview: workflow.draft.remote !== null,
    pendingCount: pending.length,
    pending,
    entryReason,
    busy,
    message,
    quickReason,
    draftReason,
    suggestionReason,
    insertSuggestion,
    addToReview,
    commentNow,
    updateEntry,
    removeEntry,
    openExternal,
    clearMessage,
  };
}

/**
 * The stored shape of an inline anchor, filled in from the editable input
 * shape the composer keeps locally plus the staleness the snapshot holds.
 */
function pendingAnchor(
  anchor: DraftInlineAnchorInputDto,
  stale: boolean,
): DraftInlineAnchorDto {
  return Object.freeze({
    revision: anchor.revision,
    old_path: anchor.old_path,
    new_path: anchor.new_path,
    old_line: anchor.old_line,
    new_line: anchor.new_line,
    side: anchor.side,
    context_fingerprint: anchor.context_fingerprint,
    start_line: anchor.start_line ?? null,
    start_side: anchor.start_side ?? null,
    stale,
  });
}

/** Said when a rollback itself is refused, appended to the failure that caused it. */
const ENTRY_ROLLBACK_REFUSAL =
  "The pending review stopped accepting edits, so the unsaved change is still listed here. Settle it in the review workflow before retrying.";

const ENTRY_GONE_REFUSAL =
  "That pending comment is no longer part of this review. Refresh the review workflow.";

/** Names the actual reason a bound draft cannot take another inline entry. */
function boundElsewhereRefusal(state: ReviewWorkflowState): string {
  return boundElsewhereSentence(state, "adding inline feedback");
}

/**
 * The same three states, worded for the controls a pending card carries. The
 * state is identical and so is the remedy; only the action the reader was
 * refused differs, and naming the wrong one is what sends them looking for a
 * composer they never opened.
 */
function boundElsewhereEntryRefusal(state: ReviewWorkflowState): string {
  return boundElsewhereSentence(state, "changing pending comments");
}

function boundElsewhereSentence(
  state: ReviewWorkflowState,
  action: string,
): string {
  const remote = state.draft.remote;
  if (remote && remote.state !== "editable")
    return `The pending review is locked by its submission attempt. Settle it in the review workflow before ${action}.`;
  if (
    remote &&
    !sameComposerRevision(
      remote.revision,
      state.displayed.latestObservedRevision,
    )
  )
    return `The pending review is bound to an earlier revision. Migrate it in the review workflow before ${action}.`;
  return `The pending review is bound to a durable submission attempt. Settle it in the review workflow before ${action}.`;
}

/**
 * The two sentences `captureDraftAnchor` raises for the reader. Only these are
 * shown verbatim: anything else it can throw is an invariant, and this card
 * adds range and suggestion anchoring to the same path, so a new invariant
 * must not become a notice.
 */
const CAPTURE_REFUSALS: readonly string[] = Object.freeze([
  "The selected diff context is partial. Refresh before drafting inline feedback.",
  "The inline selection changed while its context was captured",
]);

async function captureAnchor(
  anchor: InlineAnchorSelection,
  forge: SuggestionForge | null,
  body: string,
): Promise<DraftInlineAnchorInputDto> {
  try {
    return await captureDraftAnchor(anchorSelectionFor(anchor, forge, body), () =>
      anchorSelectionFor(anchor, forge, body),
    );
  } catch (failure) {
    throw failure instanceof Error &&
      CAPTURE_REFUSALS.includes(failure.message)
      ? new ComposerRefusal(failure.message)
      : failure;
  }
}

/**
 * The in-diff composer: two primary actions that never relabel in place, and a
 * Cancel that keeps the typed text on this anchor. Rows below it are pushed
 * down by its own height rather than covered by an overlay.
 */
export function InlineComposer({
  anchor,
  controller,
  entry,
  close,
}: {
  readonly anchor: InlineAnchorSelection;
  readonly controller: InlineComposerController;
  /**
   * The pending entry being edited, or null for a new comment. An edit keeps
   * the entry's stored anchor: it replaces a body and never recaptures a
   * target, which is why Insert suggestion, whose rules choose the anchor, is
   * not offered here.
   */
  readonly entry?: PendingDraftEntry | null;
  readonly close: () => void;
}): ReactNode {
  const editing = entry ?? null;
  const [body, setBodyState] = useState(() =>
    editing
      ? (readEditBuffer(controller.review, editing.id) ?? editing.body)
      : readInlineBuffer(controller.review, anchor),
  );
  const [preview, setPreview] = useState(false);
  const editor = useRef<HTMLTextAreaElement | null>(null);
  // The composer is opened from the gutter affordance or from the keyboard, and
  // both leave focus on the row. Taking focus here is what makes Esc and
  // Ctrl/Cmd+Enter reach the composer that just opened, and leaving the preview
  // has to hand focus back to the editor it replaced.
  useEffect(() => {
    if (!preview) editor.current?.focus();
  }, [preview]);
  const setBody = (value: string): void => {
    if (editing) writeEditBuffer(controller.review, editing.id, value);
    else writeInlineBuffer(controller.review, anchor, value);
    setBodyState(value);
  };
  const quickReason = controller.quickReason(anchor);
  const draftReason = editing
    ? controller.entryReason
    : controller.draftReason(anchor);
  const suggestionReason = controller.suggestionReason(anchor, body);
  const insertSuggestion = (): void => {
    const filled = controller.insertSuggestion(anchor, body);
    if (filled === null) return;
    setPreview(false);
    setBody(filled);
  };
  // A refusal that is already reported as the notice is not repeated as a
  // standing reason underneath it.
  const reasons = [
    ...new Set(
      (editing ? [draftReason] : [draftReason, quickReason]).filter(
        (reason): reason is string =>
          reason !== null && reason !== controller.message,
      ),
    ),
  ];
  const settle = (done: boolean): void => {
    if (!done) return;
    if (editing) clearEditBuffer(controller.review, editing.id);
    setBodyState("");
    close();
  };
  const runPrimary = (): void => {
    void (editing
      ? controller.updateEntry(editing.id, body)
      : controller.addToReview(anchor, body)
    ).then(settle);
  };
  const runQuick = (): void => {
    void controller.commentNow(anchor, body).then(settle);
  };
  return (
    <section
      className="inline-composer"
      aria-label={
        editing ? "Edit pending comment composer" : "Inline comment composer"
      }
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          close();
          return;
        }
        // The primary action belongs to the composer, not to the editor: the
        // preview replaces the textarea, and the key has to keep working from
        // the toolbar and the preview too.
        if (!(event.ctrlKey || event.metaKey) || event.key !== "Enter") return;
        event.preventDefault();
        runPrimary();
      }}
    >
      <div className="inline-composer-heading">
        <strong>
          {editing && editing.anchor
            ? `${editing.anchor.new_path}, ${pendingAnchorLabel(editing.anchor)}`
            : anchorLabel(anchor)}
        </strong>
        {editing ? (
          <span className="inline-composer-chip">Editing pending comment</span>
        ) : (
          controller.pendingReview && (
            <span className="inline-composer-chip">
              Review in progress, {controller.pendingCount} pending
            </span>
          )
        )}
      </div>
      {preview ? (
        <div className="inline-composer-preview" aria-label="Comment preview">
          {body.length === 0 ? (
            <small>Nothing to preview yet.</small>
          ) : (
            <SafeMarkdown source={body} openExternal={controller.openExternal} />
          )}
        </div>
      ) : (
        <textarea
          ref={editor}
          className="inline-composer-text"
          aria-label={
            editing ? "Pending review comment" : "Inline review comment"
          }
          value={body}
          onChange={(event) => setBody(event.target.value)}
        />
      )}
      {controller.message !== null && (
        <div className="notice notice-error" role="alert">
          {controller.message}
        </div>
      )}
      {reasons.map((reason) => (
        <small key={reason}>{reason}</small>
      ))}
      <div className="inline-composer-actions">
        <div className="inline-composer-toolbar">
          {!editing && (
            <button
              className="button button-secondary"
              disabled={suggestionReason !== null}
              title={suggestionReason ?? undefined}
              onClick={insertSuggestion}
            >
              Insert suggestion
            </button>
          )}
          <button
            className="button button-secondary"
            aria-pressed={preview}
            onClick={() => setPreview(!preview)}
          >
            Preview
          </button>
        </div>
        <div className="inline-composer-writes">
          <button className="button button-secondary" onClick={close}>
            Cancel
          </button>
          {!editing && (
            <button
              className="button button-secondary"
              disabled={body.length === 0 || quickReason !== null}
              title={quickReason ?? undefined}
              onClick={runQuick}
            >
              Add comment now
            </button>
          )}
          <button
            className="button"
            disabled={
              body.length === 0 ||
              draftReason !== null ||
              (editing !== null && controller.busy)
            }
            title={draftReason ?? undefined}
            onClick={runPrimary}
          >
            {editing
              ? "Save changes"
              : controller.pendingReview
                ? "Add to review"
                : "Start a review"}
          </button>
        </div>
      </div>
    </section>
  );
}

function cancelQuietly(bridge: InlineComposerBridge, token: string | null): void {
  if (token === null) return;
  try {
    void bridge.cancelRead(token).catch(() => undefined);
  } catch {
    // A cancellation that cannot be delivered must not fail the unmount.
  }
}

/**
 * The stored snapshot of a draft, or null when it cannot be read. Shared with
 * the review workflow panel so both surfaces recover a conflicting or frozen
 * draft the same way.
 */
export async function recoverDraft(
  bridge: ReviewDesktopBridge,
  review: string,
  draftId: string,
): Promise<DraftSnapshotDto | null> {
  try {
    return await bridge.getReviewDraft({ review, draft_id: draftId }).result;
  } catch {
    return null;
  }
}
