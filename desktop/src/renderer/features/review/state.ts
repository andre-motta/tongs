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
  readonly status: "sending" | "known" | "unknown" | "rejected";
  readonly outcome: MutationOutcomeDto | ReviewActionReceiptDto | null;
  readonly message: string | null;
}

export interface DraftEditorState {
  readonly remote: DraftSnapshotDto | null;
  readonly local: DraftContentInputDto;
  readonly dirty: boolean;
  readonly conflict: DraftSnapshotDto | null;
  readonly pendingSave: {
    readonly expectedVersion: number;
    readonly content: DraftContentInputDto;
  } | null;
}

export interface SubmissionState {
  readonly progress: SubmissionProgressDto | null;
  readonly pending: "start" | "resume" | "reconcile" | null;
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
    throw new Error("Save or resolve local draft edits before changing revision");
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
    throw new Error("Resolve the previous uncertain action before starting another");
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

export function editDraft(
  state: ReviewWorkflowState,
  content: DraftContentInputDto,
): ReviewWorkflowState {
  if (state.draft.remote && state.draft.remote.state !== "editable")
    throw new Error("This draft is not editable");
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
  if (!sameRevision(remote.revision, state.displayed.revision))
    throw new Error("Draft belongs to another displayed revision");
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
      pendingSave: null,
    }),
  });
}

export function beginDraftSave(state: ReviewWorkflowState): ReviewWorkflowState {
  const remote = state.draft.remote;
  if (!remote || !state.draft.dirty || state.draft.conflict)
    throw new Error("Draft is not ready to save");
  if (remote.state !== "editable") throw new Error("This draft is not editable");
  return replace(state, {
    draft: Object.freeze({
      ...state.draft,
      pendingSave: Object.freeze({
        expectedVersion: remote.version,
        content: state.draft.local,
      }),
    }),
  });
}

export function finishDraftSave(
  state: ReviewWorkflowState,
  remote: DraftSnapshotDto,
): ReviewWorkflowState {
  const pending = state.draft.pendingSave;
  if (!pending) throw new Error("No draft save is pending");
  if (remote.review !== state.displayed.review)
    throw new Error("Saved draft belongs to another review");
  const editedDuringSave = !sameContent(state.draft.local, pending.content);
  return replace(state, {
    draft: Object.freeze({
      remote,
      local: editedDuringSave ? state.draft.local : contentOf(remote),
      dirty: editedDuringSave,
      conflict: null,
      pendingSave: null,
    }),
  });
}

export function conflictDraftSave(
  state: ReviewWorkflowState,
  remote: DraftSnapshotDto,
): ReviewWorkflowState {
  if (!state.draft.pendingSave) throw new Error("No draft save is pending");
  return replace(state, {
    draft: Object.freeze({
      ...state.draft,
      remote,
      conflict: remote,
      pendingSave: null,
      dirty: true,
    }),
  });
}

export function chooseRemoteDraft(state: ReviewWorkflowState): ReviewWorkflowState {
  const remote = state.draft.conflict;
  if (!remote) throw new Error("No conflicting draft exists");
  return replace(state, {
    draft: Object.freeze({
      remote,
      local: contentOf(remote),
      dirty: false,
      conflict: null,
      pendingSave: null,
    }),
  });
}

export function keepLocalDraft(state: ReviewWorkflowState): ReviewWorkflowState {
  const remote = state.draft.conflict;
  if (!remote) throw new Error("No conflicting draft exists");
  return replace(state, {
    draft: Object.freeze({
      ...state.draft,
      remote,
      conflict: null,
      dirty: true,
      pendingSave: null,
    }),
  });
}

export function beginSubmission(
  state: ReviewWorkflowState,
  kind: SubmissionState["pending"],
): ReviewWorkflowState {
  if (kind === null) throw new Error("Submission operation is required");
  if (state.submission.pending) throw new Error("Submission operation is already pending");
  if (kind === "start" && !canStartSubmission(state))
    throw new Error("Save the editable draft before submitting it");
  if (kind === "resume" && state.submission.progress?.outcome !== "paused")
    throw new Error("Only paused submissions can resume");
  if (kind === "reconcile" && state.submission.progress?.outcome !== "unknown")
    throw new Error("Only unknown submissions can be reconciled");
  return replace(state, {
    submission: Object.freeze({ ...state.submission, pending: kind, message: null }),
  });
}

export function finishSubmission(
  state: ReviewWorkflowState,
  progress: SubmissionProgressDto,
): ReviewWorkflowState {
  if (!state.submission.pending) throw new Error("No submission operation is pending");
  if (progress.review !== state.displayed.review)
    throw new Error("Submission belongs to another review");
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
      !state.draft.dirty &&
      !state.draft.conflict &&
      !state.draft.pendingSave &&
      !state.submission.pending,
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

function sameContent(
  left: DraftContentInputDto,
  right: DraftContentInputDto,
): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
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
