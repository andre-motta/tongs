import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import type { ReviewRevisionDto } from "../../../shared/bridge.js";
import type {
  DraftCommentInputDto,
  DraftSnapshotDto,
  InlineCommentParams,
  ReviewDesktopBridge,
  ReviewMutationCapabilitiesDto,
} from "../../../shared/review.js";
import { reviewMutationMessage } from "../../../shared/review.js";
import type { InlineAnchorSelection } from "../../core/navigation.js";
import { safeError } from "../../core/presentation.js";
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

export function cachedWorkflow(review: string): ReviewWorkflowState | null {
  return workflowCache.get(review) ?? null;
}

export function cacheWorkflow(
  review: string,
  state: ReviewWorkflowState,
): void {
  workflowCache.set(review, state);
}

export function mutationAnchor(selection: InlineAnchorSelection): {
  readonly old_path: string;
  readonly new_path: string;
  readonly line: number;
  readonly side: "LEFT" | "RIGHT";
} {
  const line = anchorLine(selection);
  if (line === null)
    throw new Error("The selected diff side has no source line");
  return Object.freeze({
    old_path: selection.oldPath,
    new_path: selection.newPath,
    line,
    side: selection.side === "old" ? "LEFT" : "RIGHT",
  });
}

export function draftSelection(
  selection: InlineAnchorSelection,
): DraftAnchorSelection {
  return Object.freeze({
    review: selection.review,
    revision: selection.revision,
    oldPath: selection.oldPath,
    newPath: selection.newPath,
    side: selection.side,
    oldLine: selection.oldLine,
    newLine: selection.newLine,
    startLine: null,
    startSide: null,
    contextLines: selection.contextLines,
    contextComplete: selection.contextComplete,
  });
}

export function anchorLine(selection: InlineAnchorSelection): number | null {
  return selection.side === "old" ? selection.oldLine : selection.newLine;
}

/** Header text of the composer, and the label the gutter affordance announces. */
export function anchorLabel(selection: InlineAnchorSelection): string {
  const line = anchorLine(selection);
  const lines = selection.selectedLines ?? [];
  const first = lines[0];
  const start =
    first === undefined
      ? line
      : selection.side === "old"
        ? first.oldLine
        : first.newLine;
  const span =
    lines.length > 1 && start !== null && start !== line
      ? `${selection.side} lines ${start} to ${line}`
      : `${selection.side} line ${line}`;
  return `${selection.newPath}, ${span}`;
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
}

export interface InlineComposerController {
  readonly review: string;
  readonly pendingReview: boolean;
  readonly pendingCount: number;
  readonly busy: boolean;
  readonly message: string | null;
  readonly quickReason: (anchor: InlineAnchorSelection) => string | null;
  readonly draftReason: (anchor: InlineAnchorSelection) => string | null;
  readonly addToReview: (
    anchor: InlineAnchorSelection,
    body: string,
  ) => Promise<boolean>;
  readonly commentNow: (
    anchor: InlineAnchorSelection,
    body: string,
  ) => Promise<boolean>;
}

export function useInlineReviewComposer(
  bridge: InlineComposerBridge,
  review: string,
  revision: ReviewRevisionDto,
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

  const quickBlocked =
    workflow.quick?.status === "sending" || workflow.quick?.status === "unknown";
  const quickReason = useCallback(
    (anchor: InlineAnchorSelection): string | null => {
      if (capabilities === null)
        return "Inline comment support for this review is still loading.";
      if (!capabilities.inline_comment)
        return "Inline comments are unsupported for this review.";
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
      if (workflow.draft.remote && !canCaptureDraftInline(workflow))
        return "The pending review is bound to an earlier revision or to a durable submission attempt. Settle it in the review workflow first.";
      return null;
    },
    [quickReason, workflow],
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
        throw new Error(
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
        throw new Error(
          "The recovered pending review is bound to an earlier revision. Migrate it in the review workflow before adding inline feedback.",
        );
      return bound;
    },
    [apply, bridge, review],
  );

  const saveDraft = useCallback(async (): Promise<void> => {
    const current = apply(beginDraftSave);
    const remote = current.draft.remote;
    const pending = current.draft.pendingSave;
    if (!remote || !pending)
      throw new Error("The pending review is not ready to save");
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
        const captured = await captureDraftAnchor(draftSelection(anchor), () =>
          draftSelection(anchor),
        );
        await bindDraft(anchor);
        const comment: DraftCommentInputDto = Object.freeze({
          id: crypto.randomUUID(),
          kind: "inline",
          body,
          anchor: captured,
        });
        apply((current) =>
          editDraft(current, {
            ...current.draft.local,
            comments: [...current.draft.local.comments, comment],
          }),
        );
        await saveDraft();
        clearInlineBuffer(review, anchor);
        return true;
      } catch (failure) {
        setMessage(reviewMutationError(failure));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [apply, bindDraft, draftReason, review, saveDraft],
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
          anchor: mutationAnchor(anchor),
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
        setMessage(reviewMutationError(failure));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [apply, bridge, quickReason, review],
  );

  return {
    review,
    pendingReview: workflow.draft.remote !== null,
    pendingCount: workflow.draft.remote
      ? workflow.draft.local.comments.length
      : 0,
    busy,
    message,
    quickReason,
    draftReason,
    addToReview,
    commentNow,
  };
}

/**
 * The in-diff composer: two primary actions that never relabel in place, and a
 * Cancel that keeps the typed text on this anchor. Rows below it are pushed
 * down by its own height rather than covered by an overlay.
 */
export function InlineComposer({
  anchor,
  controller,
  close,
}: {
  readonly anchor: InlineAnchorSelection;
  readonly controller: InlineComposerController;
  readonly close: () => void;
}): ReactNode {
  const [body, setBodyState] = useState(() =>
    readInlineBuffer(controller.review, anchor),
  );
  const setBody = (value: string): void => {
    writeInlineBuffer(controller.review, anchor, value);
    setBodyState(value);
  };
  const quickReason = controller.quickReason(anchor);
  const draftReason = controller.draftReason(anchor);
  const reasons = [
    ...new Set(
      [draftReason, quickReason].filter(
        (reason): reason is string => reason !== null,
      ),
    ),
  ];
  const settle = (done: boolean): void => {
    if (!done) return;
    setBodyState("");
    close();
  };
  const runPrimary = (): void => {
    void controller.addToReview(anchor, body).then(settle);
  };
  const runQuick = (): void => {
    void controller.commentNow(anchor, body).then(settle);
  };
  return (
    <section
      className="inline-composer"
      aria-label="Inline comment composer"
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        close();
      }}
    >
      <div className="inline-composer-heading">
        <strong>{anchorLabel(anchor)}</strong>
        {controller.pendingReview && (
          <span className="inline-composer-chip">
            Review in progress, {controller.pendingCount} pending
          </span>
        )}
      </div>
      <textarea
        className="inline-composer-text"
        aria-label="Inline review comment"
        value={body}
        onChange={(event) => setBody(event.target.value)}
        onKeyDown={(event) => {
          if (!(event.ctrlKey || event.metaKey) || event.key !== "Enter") return;
          event.preventDefault();
          runPrimary();
        }}
      />
      {controller.message !== null && (
        <div className="notice notice-error" role="alert">
          {controller.message}
        </div>
      )}
      {reasons.map((reason) => (
        <small key={reason}>{reason}</small>
      ))}
      <div className="inline-composer-actions">
        <button className="button button-secondary" onClick={close}>
          Cancel
        </button>
        <button
          className="button button-secondary"
          disabled={body.length === 0 || quickReason !== null}
          title={quickReason ?? undefined}
          onClick={runQuick}
        >
          Add comment now
        </button>
        <button
          className="button"
          disabled={body.length === 0 || draftReason !== null}
          title={draftReason ?? undefined}
          onClick={runPrimary}
        >
          {controller.pendingReview ? "Add to review" : "Start a review"}
        </button>
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

async function recoverDraft(
  bridge: InlineComposerBridge,
  review: string,
  draftId: string,
): Promise<DraftSnapshotDto | null> {
  try {
    return await bridge.getReviewDraft({ review, draft_id: draftId }).result;
  } catch {
    return null;
  }
}
