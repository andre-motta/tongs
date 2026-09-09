import type { ReviewRevisionDto } from "../../../shared/bridge.js";
import type {
  DraftContentInputDto,
  DraftInlineAnchorInputDto,
  DraftSnapshotDto,
  MutationOutcomeDto,
  ReviewActionReceiptDto,
  SubmissionProgressDto,
} from "../../../shared/review.js";

export interface DisplayedReviewState {
  readonly review: string;
  readonly revision: ReviewRevisionDto;
  readonly latestObservedRevision: ReviewRevisionDto;
}

export interface QuickIntent<T> {
  readonly operationId: string;
  readonly command: T;
  readonly status:
    | "sending"
    | "known"
    | "unknown"
    | "acknowledged_unknown"
    | "rejected";
  readonly outcome: MutationOutcomeDto | ReviewActionReceiptDto | null;
  readonly message: string | null;
}

export interface DraftEditorState {
  readonly remote: DraftSnapshotDto | null;
  readonly local: DraftContentInputDto;
  readonly dirty: boolean;
  readonly conflict: DraftSnapshotDto | null;
  /** Version this window held when `conflict` was recorded, for the notice. */
  readonly conflictHeldVersion: number | null;
  /**
   * Every local text displaced by an explicit take-theirs resolution, retained
   * verbatim so the caller can copy it. Retention is additive: a later conflict
   * appends, so a second resolution can never discard what the first kept. An
   * entry leaves only when its own dismissal is requested.
   */
  readonly supersededLocalDrafts: readonly SupersededDraftText[];
  readonly preservedStaleDrafts: readonly DraftSnapshotDto[];
  readonly pendingSave: {
    readonly review: string;
    readonly draftId: string;
    readonly revision: ReviewRevisionDto;
    readonly expectedVersion: number;
    readonly content: DraftContentInputDto;
  } | null;
}

export interface SupersededDraftText {
  /** Stored version that displaced this text. Versions only ever advance. */
  readonly displacedByVersion: number;
  readonly content: DraftContentInputDto;
}

export type PendingSubmission =
  | {
      readonly kind: "start";
      readonly review: string;
      readonly draftId: string;
      readonly frozenVersion: number;
      readonly revision: ReviewRevisionDto;
    }
  | {
      readonly kind: "resume" | "reconcile";
      readonly review: string;
      readonly attemptId: string;
      readonly draftId: string;
      readonly frozenVersion: number;
    };

export interface SubmissionState {
  readonly progress: SubmissionProgressDto | null;
  readonly pending: PendingSubmission | null;
  readonly message: string | null;
}

export interface ReviewWorkflowState {
  readonly displayed: DisplayedReviewState;
  readonly quick: QuickIntent<unknown> | null;
  readonly draft: DraftEditorState;
  readonly submission: SubmissionState;
}

export interface DraftAnchorSelection {
  readonly review: string;
  readonly revision: ReviewRevisionDto;
  readonly oldPath: string;
  readonly newPath: string;
  readonly side: "old" | "new";
  readonly oldLine: number | null;
  readonly newLine: number | null;
  readonly startLine: number | null;
  readonly startSide: "old" | "new" | null;
  readonly contextLines: readonly string[];
  readonly contextComplete: boolean;
}

/**
 * A change this state deliberately refuses, carrying the sentence written for
 * the reader. Only refusals are raised this way. An invariant that should not
 * be reachable stays a plain `Error` and keeps reaching the reader as the
 * generic failure sentence, so a bug in this module can never publish its own
 * internal wording as advice.
 */
export class ReviewWorkflowRefusal extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ReviewWorkflowRefusal";
  }
}

const EMPTY_CONTENT: DraftContentInputDto = Object.freeze({
  body: "",
  verdict: null,
  comments: Object.freeze([]),
});

export function createReviewWorkflowState(
  review: string,
  revision: ReviewRevisionDto,
): ReviewWorkflowState {
  const captured = freezeRevision(revision);
  return Object.freeze({
    displayed: Object.freeze({
      review,
      revision: captured,
      latestObservedRevision: captured,
    }),
    quick: null,
    draft: Object.freeze({
      remote: null,
      local: EMPTY_CONTENT,
      dirty: false,
      conflict: null,
      conflictHeldVersion: null,
      supersededLocalDrafts: Object.freeze([]),
      preservedStaleDrafts: Object.freeze([]),
      pendingSave: null,
    }),
    submission: Object.freeze({ progress: null, pending: null, message: null }),
  });
}

export function observeReviewRevision(
  state: ReviewWorkflowState,
  revision: ReviewRevisionDto,
): ReviewWorkflowState {
  return replace(state, {
    displayed: Object.freeze({
      ...state.displayed,
      latestObservedRevision: freezeRevision(revision),
    }),
  });
}

export function adoptDisplayedRevision(
  state: ReviewWorkflowState,
): ReviewWorkflowState {
  if (state.draft.dirty || state.draft.pendingSave)
    throw new ReviewWorkflowRefusal("Save or resolve local draft edits before changing revision");
  return replace(state, {
    displayed: Object.freeze({
      ...state.displayed,
      revision: state.displayed.latestObservedRevision,
    }),
  });
}

export function beginQuickIntent<T extends object>(
  state: ReviewWorkflowState,
  operationId: string,
  command: T,
): ReviewWorkflowState {
  if (state.quick?.status === "sending" || state.quick?.status === "unknown")
    throw new ReviewWorkflowRefusal("Resolve the previous uncertain action before starting another");
  return replace(state, {
    quick: Object.freeze({
      operationId,
      command: immutableCopy(command),
      status: "sending",
      outcome: null,
      message: null,
    }),
  });
}

export function settleQuickIntent(
  state: ReviewWorkflowState,
  operationId: string,
  outcome: MutationOutcomeDto | ReviewActionReceiptDto,
): ReviewWorkflowState {
  const current = requireQuick(state, operationId);
  if (outcome.operation_id !== operationId)
    throw new Error("Quick action response has a different operation ID");
  assertQuickOutcomeBinding(current.command, outcome);
  return replace(state, {
    quick: Object.freeze({
      ...current,
      status: outcome.outcome,
      outcome,
      message:
        outcome.outcome === "unknown"
          ? "The remote result is unknown. Refresh or reconcile before a new action."
          : null,
    }),
  });
}

export function rejectQuickIntent(
  state: ReviewWorkflowState,
  operationId: string,
  message: string,
): ReviewWorkflowState {
  const current = requireQuick(state, operationId);
  return replace(state, {
    quick: Object.freeze({
      ...current,
      status: "rejected",
      outcome: null,
      message,
    }),
  });
}

export function markQuickIntentUncertain(
  state: ReviewWorkflowState,
  operationId: string,
): ReviewWorkflowState {
  const current = requireQuick(state, operationId);
  return replace(state, {
    quick: Object.freeze({
      ...current,
      status: "unknown",
      outcome: null,
      message:
        "The connection ended after dispatch. The action may have completed remotely.",
    }),
  });
}

export function recoverQuickIntent(
  state: ReviewWorkflowState,
  operationId: string,
  outcome: MutationOutcomeDto | ReviewActionReceiptDto,
): ReviewWorkflowState {
  const current = state.quick;
  if (
    !current ||
    current.operationId !== operationId ||
    current.status !== "unknown"
  ) {
    throw new Error("No matching uncertain quick action exists");
  }
  if (outcome.operation_id !== operationId)
    throw new Error("Recovered action has a different operation ID");
  assertQuickOutcomeBinding(current.command, outcome);
  return replace(state, {
    quick: Object.freeze({
      ...current,
      status: outcome.outcome,
      outcome,
      message:
        outcome.outcome === "unknown"
          ? "The retained result is still unknown. Inspect remote state or acknowledge the uncertainty."
          : null,
    }),
  });
}

export function acknowledgeQuickUncertainty(
  state: ReviewWorkflowState,
  operationId: string,
): ReviewWorkflowState {
  const current = state.quick;
  if (
    !current ||
    current.operationId !== operationId ||
    current.status !== "unknown"
  ) {
    throw new Error("No matching uncertain quick action exists");
  }
  return replace(state, {
    quick: Object.freeze({
      ...current,
      status: "acknowledged_unknown",
      message:
        "Remote outcome remains unknown. This intent was acknowledged without replay.",
    }),
  });
}

export function editDraft(
  state: ReviewWorkflowState,
  content: DraftContentInputDto,
): ReviewWorkflowState {
  if (state.draft.remote && state.draft.remote.state !== "editable")
    throw new ReviewWorkflowRefusal("This draft is not editable");
  if (state.submission.progress && state.submission.progress.outcome !== "editable")
    throw new ReviewWorkflowRefusal("This draft is locked by its durable submission attempt");
  return replace(state, {
    draft: Object.freeze({
      ...state.draft,
      local: immutableContent(content),
      dirty: true,
    }),
  });
}

export function adoptDraft(
  state: ReviewWorkflowState,
  remote: DraftSnapshotDto,
): ReviewWorkflowState {
  if (remote.review !== state.displayed.review)
    throw new Error("Draft belongs to another review");
  if (
    state.draft.dirty &&
    state.draft.remote !== null &&
    remote.id !== state.draft.remote.id
  ) {
    throw new ReviewWorkflowRefusal("Resolve local edits before choosing another draft");
  }
  if (
    state.draft.dirty &&
    state.draft.remote !== null &&
    remote.version !== state.draft.remote.version
  ) {
    return replace(state, {
      draft: Object.freeze({
        ...state.draft,
        remote,
        conflict: remote,
        conflictHeldVersion: state.draft.remote.version,
        pendingSave: null,
      }),
    });
  }
  return replace(state, {
    draft: Object.freeze({
      remote,
      local: contentOf(remote),
      dirty: false,
      conflict: null,
      conflictHeldVersion: null,
      supersededLocalDrafts: state.draft.supersededLocalDrafts,
      preservedStaleDrafts: state.draft.preservedStaleDrafts,
      pendingSave: null,
    }),
    submission:
      state.submission.progress?.draft_id === remote.id &&
      state.submission.progress.outcome === "editable" &&
      remote.state === "editable" &&
      remote.version >= state.submission.progress.frozen_version
        ? Object.freeze({ progress: null, pending: null, message: null })
        : state.submission,
  });
}

export function forkDraftToCurrentRevision(
  state: ReviewWorkflowState,
  fresh: DraftSnapshotDto,
): ReviewWorkflowState {
  const stale = state.draft.remote;
  if (!stale || !draftNeedsRevisionRecovery(state))
    throw new ReviewWorkflowRefusal("No stale draft is available to preserve");
  if (state.draft.dirty || state.draft.pendingSave || state.draft.conflict)
    throw new ReviewWorkflowRefusal("Save or resolve the stale draft before creating a current draft");
  if (
    fresh.id === stale.id ||
    fresh.review !== state.displayed.review ||
    !sameRevision(fresh.revision, state.displayed.latestObservedRevision)
  ) {
    throw new Error("The new draft is not bound to the current review revision");
  }
  if (!sameContent(contentOf(fresh), portableDraftContent(stale)))
    throw new Error("The new draft does not contain the approved portable content");
  const preserved = state.draft.preservedStaleDrafts.some(
    (item) => item.id === stale.id,
  )
    ? state.draft.preservedStaleDrafts
    : Object.freeze([...state.draft.preservedStaleDrafts, stale]);
  return replace(state, {
    displayed: Object.freeze({
      ...state.displayed,
      revision: state.displayed.latestObservedRevision,
    }),
    draft: Object.freeze({
      remote: fresh,
      local: contentOf(fresh),
      dirty: false,
      conflict: null,
      conflictHeldVersion: null,
      supersededLocalDrafts: state.draft.supersededLocalDrafts,
      preservedStaleDrafts: preserved,
      pendingSave: null,
    }),
    submission: Object.freeze({ progress: null, pending: null, message: null }),
  });
}

export function portableDraftContent(
  draft: DraftSnapshotDto,
): DraftContentInputDto {
  return immutableContent({
    body: draft.body,
    verdict: draft.verdict,
    comments: draft.comments.filter((comment) => comment.kind === "general"),
  });
}

export function beginDraftSave(state: ReviewWorkflowState): ReviewWorkflowState {
  const remote = state.draft.remote;
  if (!remote || !state.draft.dirty || state.draft.conflict)
    throw new ReviewWorkflowRefusal("Draft is not ready to save");
  if (state.draft.pendingSave) throw new ReviewWorkflowRefusal("A draft save is already pending");
  if (remote.state !== "editable") throw new ReviewWorkflowRefusal("This draft is not editable");
  if (hasEmptyDraftComment(state.draft.local))
    throw new ReviewWorkflowRefusal("Edit or remove empty draft comments before saving");
  return replace(state, {
    draft: Object.freeze({
      ...state.draft,
      pendingSave: Object.freeze({
        review: remote.review,
        draftId: remote.id,
        revision: freezeRevision(remote.revision),
        expectedVersion: remote.version,
        content: state.draft.local,
      }),
    }),
  });
}

export function canSaveDraft(state: ReviewWorkflowState): boolean {
  return Boolean(
    state.draft.remote?.state === "editable" &&
      state.draft.dirty &&
      !state.draft.pendingSave &&
      !state.draft.conflict &&
      !hasEmptyDraftComment(state.draft.local),
  );
}

export function finishDraftSave(
  state: ReviewWorkflowState,
  remote: DraftSnapshotDto,
): ReviewWorkflowState {
  const pending = state.draft.pendingSave;
  if (!pending) throw new Error("No draft save is pending");
  assertDraftSaveBinding(pending, remote, true);
  const editedDuringSave = !sameContent(state.draft.local, pending.content);
  return replace(state, {
    draft: Object.freeze({
      remote,
      local: editedDuringSave ? state.draft.local : contentOf(remote),
      dirty: editedDuringSave,
      conflict: null,
      conflictHeldVersion: null,
      supersededLocalDrafts: state.draft.supersededLocalDrafts,
      preservedStaleDrafts: state.draft.preservedStaleDrafts,
      pendingSave: null,
    }),
  });
}

export function conflictDraftSave(
  state: ReviewWorkflowState,
  remote: DraftSnapshotDto,
): ReviewWorkflowState {
  const pending = state.draft.pendingSave;
  if (!pending) throw new Error("No draft save is pending");
  assertDraftSaveBinding(pending, remote, false);
  return replace(state, {
    draft: Object.freeze({
      ...state.draft,
      remote,
      conflict: remote,
      conflictHeldVersion: pending.expectedVersion,
      pendingSave: null,
      dirty: true,
    }),
  });
}

export function failDraftSave(state: ReviewWorkflowState): ReviewWorkflowState {
  if (!state.draft.pendingSave) throw new Error("No draft save is pending");
  return replace(state, {
    draft: Object.freeze({
      ...state.draft,
      pendingSave: null,
      dirty: true,
    }),
  });
}

export function chooseRemoteDraft(state: ReviewWorkflowState): ReviewWorkflowState {
  const remote = state.draft.conflict;
  if (!remote) throw new ReviewWorkflowRefusal("No conflicting draft exists");
  const adopted = contentOf(remote);
  const displaced = state.draft.local;
  return replace(state, {
    draft: Object.freeze({
      remote,
      local: adopted,
      dirty: false,
      conflict: null,
      conflictHeldVersion: null,
      supersededLocalDrafts: sameContent(displaced, adopted)
        ? state.draft.supersededLocalDrafts
        : Object.freeze([
            ...state.draft.supersededLocalDrafts,
            Object.freeze({
              displacedByVersion: remote.version,
              content: displaced,
            }),
          ]),
      preservedStaleDrafts: state.draft.preservedStaleDrafts,
      pendingSave: null,
    }),
  });
}

export function dismissSupersededDraft(
  state: ReviewWorkflowState,
  displacedByVersion: number,
): ReviewWorkflowState {
  const remaining = state.draft.supersededLocalDrafts.filter(
    (item) => item.displacedByVersion !== displacedByVersion,
  );
  if (remaining.length === state.draft.supersededLocalDrafts.length)
    throw new ReviewWorkflowRefusal("No superseded local draft text is retained for that version");
  return replace(state, {
    draft: Object.freeze({
      ...state.draft,
      supersededLocalDrafts: Object.freeze(remaining),
    }),
  });
}

export function keepLocalDraft(state: ReviewWorkflowState): ReviewWorkflowState {
  const remote = state.draft.conflict;
  if (!remote) throw new ReviewWorkflowRefusal("No conflicting draft exists");
  if (remote.state !== "editable")
    throw new ReviewWorkflowRefusal("The conflicting draft is no longer editable");
  return replace(state, {
    draft: Object.freeze({
      ...state.draft,
      remote,
      conflict: null,
      conflictHeldVersion: null,
      dirty: true,
      pendingSave: null,
    }),
  });
}

/**
 * Why keeping the local text cannot be saved over the conflicting draft, or
 * null when it can. Evaluated on the conflicted state so a refusal leaves the
 * comparison and the take-theirs choice in place.
 */
export function keepLocalDraftRefusal(state: ReviewWorkflowState): string | null {
  const conflict = state.draft.conflict;
  if (!conflict) return "No draft conflict is waiting to be resolved.";
  if (conflict.state === "submitted")
    return "The stored draft was already submitted, so nothing can be saved over it. Take the stored version and copy your text out.";
  if (conflict.state !== "editable")
    return "The stored draft is held by a submission attempt elsewhere, so nothing can be saved over it yet. Take the stored version and copy your text out.";
  if (hasEmptyDraftComment(state.draft.local))
    return "Edit or remove empty draft comments, then keep your text.";
  if (!canSaveDraft(keepLocalDraft(state)))
    return "Your kept text cannot be saved over the stored draft yet. Take the stored version and copy your text out.";
  return null;
}

/**
 * Why the pending review cannot be discarded now, or null when it can. A
 * discard destroys stored content, so it is refused while anything else holds
 * the draft rather than racing it: an in-flight save would resurrect what was
 * discarded under its own expected version, and a submission attempt owns the
 * draft until it is reconciled.
 */
export function discardDraftRefusal(state: ReviewWorkflowState): string | null {
  const remote = state.draft.remote;
  if (!remote) return "No pending review is open to discard.";
  if (state.draft.pendingSave)
    return "A draft save is in flight. Let it settle before discarding the review.";
  if (state.submission.pending)
    return "A submission step is in flight. Let it settle before discarding the review.";
  if (state.submission.progress && state.submission.progress.outcome !== "editable")
    return "This review is held by its submission attempt. Reconcile the attempt before discarding it.";
  if (remote.state !== "editable")
    return "This draft is held by a submission attempt, so it cannot be discarded yet.";
  return null;
}

/**
 * What a discard would destroy, named for the reader. Both counts are read
 * from the content the surfaces actually hold rather than assumed: a review
 * with no summary must not be told it is losing one, and the comment count is
 * the same number the drawer badge and the drawer title show. Shared by the
 * drawer and the in-diff composer so one press and the other are answering
 * literally the same question.
 */
export function discardSubject(state: ReviewWorkflowState): string {
  const comments = state.draft.local.comments.length;
  const summary = state.draft.local.body.trim().length > 0;
  const counted =
    comments === 1 ? "1 pending comment" : `${comments} pending comments`;
  if (comments === 0) return summary ? "the summary" : "this pending review";
  return summary ? `${counted} and the summary` : counted;
}

/**
 * The sentence shown before a discard is carried out. The wording is fixed and
 * the two facts inside it are real, so a reader who confirms has been told
 * exactly what leaves.
 */
export function discardConfirmationPrompt(state: ReviewWorkflowState): string {
  return `Discard ${discardSubject(state)}? This cannot be undone.`;
}

/**
 * Drops the discarded draft and returns the review to its no-draft state. The
 * retained texts are kept deliberately: `supersededLocalDrafts` is text the
 * reader was promised would stay until dismissed on its own, and
 * `preservedStaleDrafts` records old drafts this session did not delete, so
 * neither is this action's to throw away.
 */
export function discardDraft(state: ReviewWorkflowState): ReviewWorkflowState {
  const refusal = discardDraftRefusal(state);
  if (refusal !== null) throw new ReviewWorkflowRefusal(refusal);
  return replace(state, {
    draft: Object.freeze({
      remote: null,
      local: EMPTY_CONTENT,
      dirty: false,
      conflict: null,
      conflictHeldVersion: null,
      supersededLocalDrafts: state.draft.supersededLocalDrafts,
      preservedStaleDrafts: state.draft.preservedStaleDrafts,
      pendingSave: null,
    }),
    submission: Object.freeze({ progress: null, pending: null, message: null }),
  });
}

export function beginSubmission(
  state: ReviewWorkflowState,
  kind: PendingSubmission["kind"],
): ReviewWorkflowState {
  if (state.submission.pending) throw new ReviewWorkflowRefusal("Submission operation is already pending");
  if (kind === "start" && !canStartSubmission(state))
    throw new ReviewWorkflowRefusal("Save the editable draft before submitting it");
  if (kind === "resume" && state.submission.progress?.outcome !== "paused")
    throw new ReviewWorkflowRefusal("Only paused submissions can resume");
  if (kind === "reconcile" && state.submission.progress?.outcome !== "unknown")
    throw new ReviewWorkflowRefusal("Only unknown submissions can be reconciled");
  const remote = state.draft.remote;
  const progress = state.submission.progress;
  const pending: PendingSubmission =
    kind === "start"
      ? Object.freeze({
          kind,
          review: state.displayed.review,
          draftId: remote!.id,
          frozenVersion: remote!.version,
          revision: freezeRevision(remote!.revision),
        })
      : Object.freeze({
          kind,
          review: state.displayed.review,
          attemptId: progress!.attempt_id,
          draftId: progress!.draft_id,
          frozenVersion: progress!.frozen_version,
        });
  return replace(state, {
    submission: Object.freeze({ ...state.submission, pending, message: null }),
  });
}

export function finishSubmission(
  state: ReviewWorkflowState,
  progress: SubmissionProgressDto,
): ReviewWorkflowState {
  const pending = state.submission.pending;
  if (!pending) throw new Error("No submission operation is pending");
  assertSubmissionBinding(pending, progress);
  return replace(state, {
    submission: Object.freeze({
      progress,
      pending: null,
      message: submissionGuidance(progress),
    }),
  });
}

export function recoverSubmission(
  state: ReviewWorkflowState,
  progress: SubmissionProgressDto,
): ReviewWorkflowState {
  if (progress.review !== state.displayed.review)
    throw new Error("Submission belongs to another review");
  if (
    state.draft.remote?.id !== progress.draft_id ||
    state.draft.remote.version < progress.frozen_version
  ) {
    throw new Error("Submission does not match the active draft");
  }
  if (state.submission.pending)
    assertSubmissionBinding(state.submission.pending, progress);
  return replace(state, {
    submission: Object.freeze({
      progress,
      pending: null,
      message: submissionGuidance(progress),
    }),
  });
}

export function failSubmission(
  state: ReviewWorkflowState,
  message: string,
): ReviewWorkflowState {
  if (!state.submission.pending) throw new Error("No submission operation is pending");
  return replace(state, {
    submission: Object.freeze({
      ...state.submission,
      pending: null,
      message,
    }),
  });
}

export function canStartSubmission(state: ReviewWorkflowState): boolean {
  return Boolean(
    state.draft.remote?.state === "editable" &&
      sameRevision(
        state.draft.remote.revision,
        state.displayed.latestObservedRevision,
      ) &&
      !state.draft.remote.comments.some(
        (comment) => comment.kind === "inline" && comment.anchor.stale,
      ) &&
      !state.draft.dirty &&
      !state.draft.conflict &&
      !state.draft.pendingSave &&
      !state.submission.pending &&
      !state.submission.progress,
  );
}

export function draftNeedsRevisionRecovery(state: ReviewWorkflowState): boolean {
  const draft = state.draft.remote;
  return Boolean(
    draft &&
      (!sameRevision(draft.revision, state.displayed.latestObservedRevision) ||
        draft.comments.some(
          (comment) => comment.kind === "inline" && comment.anchor.stale,
        )),
  );
}

export function canCaptureDraftInline(state: ReviewWorkflowState): boolean {
  return Boolean(
    state.draft.remote?.state === "editable" &&
      sameRevision(
        state.draft.remote.revision,
        state.displayed.latestObservedRevision,
      ) &&
      !state.submission.pending &&
      !state.submission.progress,
  );
}

export function submissionGuidance(progress: SubmissionProgressDto): string | null {
  if (progress.outcome === "submitted") return "Review submitted.";
  if (progress.outcome === "unknown")
    return `Remote status is unknown for ${progress.unknown_step_ids.length} step(s). Reconcile before continuing.`;
  if (progress.outcome === "paused")
    return "Submission paused after confirmed steps. Resume uses the existing durable attempt.";
  return "The draft is editable again.";
}

export async function captureDraftAnchor(
  selection: DraftAnchorSelection,
  current: () => DraftAnchorSelection | null,
): Promise<DraftInlineAnchorInputDto> {
  if (!selection.contextComplete)
    throw new Error("The selected diff context is partial. Refresh before drafting inline feedback.");
  const frozen = immutableSelection(selection);
  const fingerprint = await contextFingerprint(frozen.contextLines);
  if (!sameSelection(frozen, current()))
    throw new Error("The inline selection changed while its context was captured");
  return Object.freeze({
    revision: frozen.revision,
    old_path: frozen.oldPath,
    new_path: frozen.newPath,
    old_line: frozen.oldLine,
    new_line: frozen.newLine,
    side: frozen.side,
    context_fingerprint: fingerprint,
    start_line: frozen.startLine,
    start_side: frozen.startSide,
  });
}

export async function contextFingerprint(
  contextLines: readonly string[],
): Promise<string> {
  const encoder = new TextEncoder();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (const line of contextLines) {
    const encoded = encoder.encode(line);
    const length = new Uint8Array(8);
    new DataView(length.buffer).setBigUint64(0, BigInt(encoded.byteLength), false);
    chunks.push(length, encoded);
    size += length.byteLength + encoded.byteLength;
  }
  const input = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    input.set(chunk, offset);
    offset += chunk.byteLength;
  }
  const digest = await crypto.subtle.digest("SHA-256", input);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

function requireQuick(
  state: ReviewWorkflowState,
  operationId: string,
): QuickIntent<unknown> {
  if (!state.quick || state.quick.operationId !== operationId)
    throw new Error("Quick action response does not match the current intent");
  if (state.quick.status !== "sending")
    throw new Error("Quick action is no longer awaiting a response");
  return state.quick;
}

function assertQuickOutcomeBinding(
  command: unknown,
  outcome: MutationOutcomeDto | ReviewActionReceiptDto,
): void {
  if (!("action" in outcome)) return;
  if (command === null || typeof command !== "object")
    throw new Error("Review action receipt has no matching frozen command");
  const frozen = command as Record<string, unknown>;
  const action = frozen.action;
  const expectedState = action === "reopen" ? "closed" : "open";
  if (
    outcome.action !== action ||
    outcome.review !== frozen.review ||
    outcome.expected_state !== expectedState ||
    !isRevision(frozen.revision) ||
    !sameRevision(outcome.revision, frozen.revision)
  ) {
    throw new Error("Review action receipt does not match the frozen target");
  }
}

function assertDraftSaveBinding(
  pending: NonNullable<DraftEditorState["pendingSave"]>,
  remote: DraftSnapshotDto,
  completed: boolean,
): void {
  if (
    remote.review !== pending.review ||
    remote.id !== pending.draftId ||
    !sameRevision(remote.revision, pending.revision) ||
    (completed
      ? remote.version !== pending.expectedVersion + 1
      : remote.version <= pending.expectedVersion)
  ) {
    throw new Error("Draft save result does not match the pending version");
  }
}

function assertSubmissionBinding(
  pending: PendingSubmission,
  progress: SubmissionProgressDto,
): void {
  if (
    progress.review !== pending.review ||
    progress.draft_id !== pending.draftId ||
    progress.frozen_version !== pending.frozenVersion ||
    (pending.kind !== "start" && progress.attempt_id !== pending.attemptId)
  ) {
    throw new Error("Submission result does not match the pending attempt");
  }
}

function replace(
  state: ReviewWorkflowState,
  update: Partial<ReviewWorkflowState>,
): ReviewWorkflowState {
  return Object.freeze({ ...state, ...update });
}

function contentOf(draft: DraftSnapshotDto): DraftContentInputDto {
  return immutableContent({
    body: draft.body,
    verdict: draft.verdict,
    comments: draft.comments.map((comment) =>
      comment.kind === "inline"
        ? {
            id: comment.id,
            kind: comment.kind,
            body: comment.body,
            anchor: {
              revision: comment.anchor.revision,
              old_path: comment.anchor.old_path,
              new_path: comment.anchor.new_path,
              old_line: comment.anchor.old_line,
              new_line: comment.anchor.new_line,
              side: comment.anchor.side,
              context_fingerprint: comment.anchor.context_fingerprint,
              start_line: comment.anchor.start_line,
              start_side: comment.anchor.start_side,
            },
          }
        : comment,
    ),
  });
}

function immutableContent(content: DraftContentInputDto): DraftContentInputDto {
  return immutableCopy(content);
}

function immutableSelection(selection: DraftAnchorSelection): DraftAnchorSelection {
  return Object.freeze({
    ...selection,
    revision: freezeRevision(selection.revision),
    contextLines: Object.freeze([...selection.contextLines]),
  });
}

function immutableCopy<T>(value: T): T {
  if (Array.isArray(value))
    return Object.freeze(value.map((item) => immutableCopy(item))) as T;
  if (value !== null && typeof value === "object") {
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) result[key] = immutableCopy(item);
    return Object.freeze(result) as T;
  }
  return value;
}

function freezeRevision(revision: ReviewRevisionDto): ReviewRevisionDto {
  return Object.freeze({ ...revision });
}

function sameRevision(left: ReviewRevisionDto, right: ReviewRevisionDto): boolean {
  return (
    left.head_sha === right.head_sha &&
    left.base_sha === right.base_sha &&
    left.start_sha === right.start_sha
  );
}

function isRevision(value: unknown): value is ReviewRevisionDto {
  return (
    value !== null &&
    typeof value === "object" &&
    "head_sha" in value &&
    "base_sha" in value &&
    "start_sha" in value
  );
}

function sameContent(
  left: DraftContentInputDto,
  right: DraftContentInputDto,
): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function hasEmptyDraftComment(content: DraftContentInputDto): boolean {
  return content.comments.some((comment) => comment.body.length === 0);
}

function sameSelection(
  left: DraftAnchorSelection,
  right: DraftAnchorSelection | null,
): boolean {
  return Boolean(
    right &&
      left.review === right.review &&
      sameRevision(left.revision, right.revision) &&
      left.oldPath === right.oldPath &&
      left.newPath === right.newPath &&
      left.side === right.side &&
      left.oldLine === right.oldLine &&
      left.newLine === right.newLine &&
      left.startLine === right.startLine &&
      left.startSide === right.startSide &&
      left.contextComplete === right.contextComplete &&
      left.contextLines.length === right.contextLines.length &&
      left.contextLines.every((line, index) => line === right.contextLines[index]),
  );
}
