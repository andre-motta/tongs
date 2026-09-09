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
  DraftContentInputDto,
  DraftCommentInputDto,
  DraftSnapshotDto,
  ReconciliationResolution,
  ReviewMutationCapabilitiesDto,
  ReviewVerdict,
  SubmissionProgressDto,
  SubmissionStepDto,
} from "../../../shared/review.js";
import { formatDate } from "../../core/presentation.js";
import { SafeMarkdown } from "../../core/safe-markdown.js";
import {
  ComposerRefusal,
  isConflictError,
  readActiveDrafts,
  recoverDraft,
  releaseActiveDrafts,
  reviewMutationError,
  useInlineReviewComposer,
  type InlineComposerBridge,
  type InlineComposerController,
} from "./composer.js";
import { reviewDrawerIsOpen, useReviewKeyMap } from "./keys.js";
import {
  pendingEntryLabel,
  staleRibbonText,
  type PendingDraftEntry,
} from "./pending-card.js";
import type { SuggestionForge } from "./suggestion.js";
import {
  ReviewWorkflowRefusal,
  adoptDraft,
  beginDraftSave,
  beginSubmission,
  canSaveDraft,
  canStartSubmission,
  chooseRemoteDraft,
  conflictDraftSave,
  dismissSupersededDraft,
  draftNeedsRevisionRecovery,
  editDraft,
  failDraftSave,
  failSubmission,
  finishDraftSave,
  finishSubmission,
  forkDraftToCurrentRevision,
  keepLocalDraft,
  keepLocalDraftRefusal,
  portableDraftContent,
  recoverSubmission,
  type ReviewWorkflowState,
} from "./state.js";

/**
 * The pending entry a surface asked the diff to open its composer on. The
 * drawer can be open over any review panel, so Edit has to survive the
 * navigation to Changes and the diff load that follows it. The request is
 * held by this module for the same reason the composer buffers are: it
 * belongs to the review, not to whichever component happens to be mounted.
 */
interface PendingEditRequest {
  readonly review: string;
  readonly entryId: string;
}

let pendingEditRequest: PendingEditRequest | null = null;
const pendingEditListeners = new Set<() => void>();

export function requestPendingEdit(review: string, entryId: string): void {
  pendingEditRequest = Object.freeze({ review, entryId });
  for (const listener of [...pendingEditListeners]) listener();
}

/**
 * The outstanding request for this review, if there is one, without claiming
 * it. The diff has to wait for the draft to be adopted and for the entry's row
 * to be loaded before it can act, and a request read away in the meantime
 * would be an Edit that silently did nothing.
 */
export function peekPendingEdit(review: string): string | null {
  const request = pendingEditRequest;
  return request && request.review === review ? request.entryId : null;
}

/**
 * Claims the outstanding request for this review, if there is one. Claiming
 * clears it: an Edit is one instruction, and a diff that reloads afterwards
 * must not reopen an editor the reader already closed.
 */
export function takePendingEdit(review: string): string | null {
  const entryId = peekPendingEdit(review);
  if (entryId === null) return null;
  pendingEditRequest = null;
  return entryId;
}

export function subscribePendingEdit(listener: () => void): () => void {
  pendingEditListeners.add(listener);
  return () => pendingEditListeners.delete(listener);
}

/** Forgets any outstanding request. Exported so tests start from a clean slate. */
export function clearPendingEdit(): void {
  pendingEditRequest = null;
}

const drawerOpenListeners = new Map<string, Set<() => void>>();

/**
 * Opens **Your review** from outside the header that mounts it, which is what
 * Shift+C does from the diff. It is a request rather than a shared open flag
 * for the same reason Edit is: the drawer's open state belongs to the mount,
 * and the diff has no business holding it. The answer says whether a mount
 * took the request, so a key pressed on a surface with no drawer is left
 * unclaimed instead of being swallowed.
 */
export function requestDrawerOpen(review: string): boolean {
  const listeners = drawerOpenListeners.get(review);
  if (listeners === undefined || listeners.size === 0) return false;
  for (const listener of [...listeners]) listener();
  return true;
}

export function subscribeDrawerOpen(
  review: string,
  listener: () => void,
): () => void {
  const listeners = drawerOpenListeners.get(review) ?? new Set<() => void>();
  drawerOpenListeners.set(review, listeners);
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) drawerOpenListeners.delete(review);
  };
}

/**
 * The verdicts this review can actually record, in the order the tiles show
 * them and the order `v` cycles them, which is the order the terminal
 * interface cycles them in too (`review_submit.action_cycle_verdict`).
 */
export function allowedVerdicts(
  capabilities: ReviewMutationCapabilitiesDto | null,
): readonly ReviewVerdict[] {
  if (capabilities === null) return [];
  const allowed: ReviewVerdict[] = [];
  if (capabilities.comment_verdict) allowed.push("comment");
  if (capabilities.approve) allowed.push("approve");
  if (capabilities.request_changes) allowed.push("request_changes");
  return allowed;
}

/** The next verdict `v` moves to, wrapping, from whatever is recorded now. */
export function nextVerdict(
  allowed: readonly ReviewVerdict[],
  current: ReviewVerdict | null,
): ReviewVerdict | null {
  if (allowed.length === 0) return null;
  const at = current === null ? -1 : allowed.indexOf(current);
  return allowed[(at + 1) % allowed.length] ?? null;
}

/** Where the drawer sends the reader when a pending entry is jumped to. */
export interface PendingEntryTarget {
  readonly path: string;
  readonly side: "old" | "new";
  readonly line: number;
}

/**
 * The diff coordinates of one pending entry, or null when it has no anchor of
 * its own. The path and the line are read straight off the stored anchor, so
 * the drawer hands the jump exactly the coordinates the existing discussion
 * jump already resolves, and resolves nothing itself.
 */
export function pendingEntryTarget(
  entry: PendingDraftEntry,
): PendingEntryTarget | null {
  const anchor = entry.anchor;
  if (!anchor) return null;
  const line = anchor.side === "old" ? anchor.old_line : anchor.new_line;
  if (line === null) return null;
  return Object.freeze({
    path: anchor.side === "old" ? anchor.old_path : anchor.new_path,
    side: anchor.side,
    line,
  });
}

/**
 * The file a pending entry belongs to, as the drawer groups them. An entry
 * with no anchor is a general comment or a reply, which belongs to the review
 * rather than to a file.
 */
const UNANCHORED_GROUP = "Review-level";

export function pendingEntryGroup(entry: PendingDraftEntry): string {
  const anchor = entry.anchor;
  if (!anchor) return UNANCHORED_GROUP;
  return anchor.side === "old" ? anchor.old_path : anchor.new_path;
}

/**
 * How the drawer names one entry. The in-diff card names only the line,
 * because the file is the header it sits under; a drawer row is read out of
 * that context and lists every file at once, so it carries the file too. It
 * also keeps the two surfaces' controls distinguishable to anything reading
 * their labels, the two Edit buttons included.
 */
export function drawerEntryLabel(
  file: string,
  entry: PendingDraftEntry,
): string {
  if (entry.anchor) return `${file}, ${pendingEntryLabel(entry)}`;
  // A reply names the discussion it answers. The in-diff reply sits under its
  // own thread and needs no name; a drawer row is read away from the thread,
  // and "reply" alone does not say which one it is.
  if (entry.kind === "reply" && entry.threadId !== null)
    return `reply to ${entry.threadId}`;
  return pendingEntryLabel(entry);
}

export interface PendingEntryGroup {
  readonly file: string;
  readonly entries: readonly PendingDraftEntry[];
}

/**
 * The pending entries grouped by file, in the order the draft stores them, so
 * the drawer lists the whole review including entries whose rows the diff has
 * not loaded. First appearance orders the groups: the reader's own order of
 * writing is more useful here than an alphabetical one.
 */
export function groupPendingEntries(
  pending: readonly PendingDraftEntry[],
): readonly PendingEntryGroup[] {
  const groups = new Map<string, PendingDraftEntry[]>();
  for (const entry of pending) {
    const file = pendingEntryGroup(entry);
    const existing = groups.get(file);
    if (existing) existing.push(entry);
    else groups.set(file, [entry]);
  }
  return Object.freeze(
    [...groups.entries()].map(([file, entries]) =>
      Object.freeze({ file, entries: Object.freeze(entries) }),
    ),
  );
}

type DrawerConfirmation =
  | "submit"
  | "discard"
  | "migrate-draft"
  | `reconcile:${ReconciliationResolution}`;

/**
 * Everything the drawer renders and every review-level write it makes. Entry
 * level writes (Edit, Delete) are the composer controller's, taken verbatim,
 * so a pending entry is changed by exactly one code path whether the press
 * lands on an in-diff card or on a drawer row.
 */
export interface ReviewDrawerController {
  readonly review: string;
  readonly workflow: ReviewWorkflowState;
  readonly pending: readonly PendingDraftEntry[];
  readonly groups: readonly PendingEntryGroup[];
  readonly pendingCount: number;
  readonly capabilities: ReviewMutationCapabilitiesDto | null;
  readonly draftCandidates: readonly DraftSnapshotDto[];
  readonly recoveries: readonly SubmissionProgressDto[];
  readonly entryReason: string | null;
  readonly busy: boolean;
  readonly message: string | null;
  readonly setSummary: (body: string) => void;
  readonly setVerdict: (verdict: ReviewVerdict | null) => void;
  readonly save: () => Promise<boolean>;
  readonly submit: () => Promise<void>;
  readonly continueSubmission: (
    mode: "resume" | "reconcile",
    resolution?: ReconciliationResolution,
  ) => Promise<void>;
  readonly recoverDurable: (progress: SubmissionProgressDto) => Promise<void>;
  readonly adoptCandidate: (draft: DraftSnapshotDto) => void;
  readonly keepLocal: () => Promise<void>;
  readonly takeRemote: () => void;
  readonly dismissSuperseded: (displacedByVersion: number) => void;
  readonly migrate: () => Promise<void>;
  readonly discard: () => Promise<void>;
  readonly discardReason: string | null;
  /** What a discard would destroy, for the confirmation and its button. */
  readonly discardSubject: string;
  /** The full sentence a reader confirms before the discard is carried out. */
  readonly discardPrompt: string;
  readonly removeEntry: (entryId: string) => Promise<boolean>;
  readonly updateEntry: (entryId: string, body: string) => Promise<boolean>;
  readonly clearMessage: () => void;
}

/**
 * The review-level half of the review workflow: the summary, the verdict, the
 * save that carries them, submission, recovery, conflict resolution, stale
 * revision migration and discard. It builds on the shared workflow state, so
 * everything it writes is immediately what the in-diff composer reads.
 */
export function useReviewDrawer(
  bridge: InlineComposerBridge,
  review: string,
  revision: ReviewRevisionDto,
  forge: SuggestionForge | null = null,
): ReviewDrawerController {
  const composer = useInlineReviewComposer(bridge, review, revision, forge);
  const { workflow, held, apply } = composer.shared;
  const [draftCandidates, setDraftCandidates] = useState<
    readonly DraftSnapshotDto[]
  >([]);
  const [recoveries, setRecoveries] = useState<
    readonly SubmissionProgressDto[]
  >([]);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const clearMessage = useCallback(() => setMessage(null), []);

  // The candidate list is what the recovery choices are drawn from, and the
  // read behind it is the same one mount adoption shares.
  useEffect(() => {
    let live = true;
    let shared: ReturnType<typeof readActiveDrafts> | null = null;
    try {
      shared = readActiveDrafts(bridge, review);
      void shared.result.then(
        (result) => {
          if (live) setDraftCandidates(result.drafts);
        },
        () => undefined,
      );
    } catch {
      // The drawer opens on whatever the shared state already holds.
    }
    return () => {
      live = false;
      if (shared) releaseActiveDrafts(bridge, review, shared);
    };
  }, [bridge, review]);

  useEffect(() => {
    let live = true;
    let token: string | null = null;
    try {
      const read = bridge.listReviewSubmissions({ review, max_items: 100 });
      token = read.requestToken;
      void read.result.then(
        (result) => {
          if (live) setRecoveries(result.attempts);
        },
        () => undefined,
      );
    } catch {
      // An unreadable attempt list leaves the drawer with no recovery rows,
      // which is what it shows when there are none.
    }
    return () => {
      live = false;
      if (token !== null)
        try {
          void bridge.cancelRead(token).catch(() => undefined);
        } catch {
          // A cancellation that cannot be delivered must not fail the unmount.
        }
    };
  }, [bridge, review]);

  const editContent = useCallback(
    (change: (content: DraftContentInputDto) => DraftContentInputDto): void => {
      try {
        apply((state) => editDraft(state, change(state.draft.local)));
        setMessage(null);
      } catch (reason) {
        setMessage(drawerFailureMessage(reason));
      }
    },
    [apply],
  );

  const setSummary = useCallback(
    (body: string): void => editContent((content) => ({ ...content, body })),
    [editContent],
  );

  const setVerdict = useCallback(
    (verdict: ReviewVerdict | null): void =>
      editContent((content) => ({ ...content, verdict })),
    [editContent],
  );

  const save = useCallback(async (): Promise<boolean> => {
    let current: ReviewWorkflowState;
    try {
      current = apply(beginDraftSave);
    } catch (reason) {
      setMessage(drawerFailureMessage(reason));
      return false;
    }
    const remote = current.draft.remote;
    const pending = current.draft.pendingSave;
    if (!remote || !pending) {
      setMessage("The pending review is not ready to save.");
      return false;
    }
    setBusy(true);
    setMessage(null);
    try {
      const saved = await bridge.saveReviewDraft({
        review,
        draft_id: remote.id,
        expected_version: pending.expectedVersion,
        content: pending.content,
      });
      apply((latest) => finishDraftSave(latest, saved));
      setDraftCandidates((items) => [
        saved,
        ...items.filter((item) => item.id !== saved.id),
      ]);
      return true;
    } catch (reason) {
      const conflicting = isConflictError(reason)
        ? await recoverDraft(bridge, review, remote.id)
        : null;
      if (conflicting) apply((latest) => conflictDraftSave(latest, conflicting));
      else apply(failDraftSave);
      setMessage(drawerFailureMessage(reason));
      return false;
    } finally {
      setBusy(false);
    }
  }, [apply, bridge, review]);

  const submit = useCallback(async (): Promise<void> => {
    // Submission is refused on a dirty draft, so the summary and the verdict
    // the reader just typed are stored first and the submission runs on the
    // stored version. A failed save stops here rather than submitting a
    // review the store does not hold.
    if (held.current.draft.dirty && !(await save())) return;
    const ready = held.current;
    if (!canStartSubmission(ready)) {
      setMessage(submitRefusal(ready));
      return;
    }
    let current: ReviewWorkflowState;
    try {
      current = apply((state) => beginSubmission(state, "start"));
    } catch (reason) {
      setMessage(drawerFailureMessage(reason));
      return;
    }
    const draft = current.draft.remote;
    if (!draft) return;
    setBusy(true);
    setMessage(null);
    try {
      const progress = await bridge.startReviewSubmission({
        review,
        draft_id: draft.id,
        expected_version: draft.version,
      });
      apply((state) => finishSubmission(state, progress));
      setRecoveries((items) => [
        progress,
        ...items.filter((item) => item.attempt_id !== progress.attempt_id),
      ]);
    } catch (reason) {
      const recovered = await recoverLatestSubmission(
        bridge,
        review,
        draft.id,
        draft.version,
      );
      if (recovered) {
        apply((state) => recoverSubmission(state, recovered));
        setRecoveries((items) => [
          recovered,
          ...items.filter((item) => item.attempt_id !== recovered.attempt_id),
        ]);
      } else {
        apply((state) => failSubmission(state, drawerFailureMessage(reason)));
      }
      setMessage(drawerFailureMessage(reason));
    } finally {
      setBusy(false);
    }
  }, [apply, bridge, held, review, save]);

  const continueSubmission = useCallback(
    async (
      mode: "resume" | "reconcile",
      resolution?: ReconciliationResolution,
    ): Promise<void> => {
      const attempt = held.current.submission.progress;
      if (!attempt) return;
      try {
        apply((state) => beginSubmission(state, mode));
      } catch (reason) {
        setMessage(drawerFailureMessage(reason));
        return;
      }
      setBusy(true);
      setMessage(null);
      try {
        const progress =
          mode === "resume"
            ? await bridge.resumeReviewSubmission({
                review,
                attempt_id: attempt.attempt_id,
              })
            : await bridge.reconcileReviewSubmission({
                review,
                attempt_id: attempt.attempt_id,
                resolution: resolution ?? "retry_remaining",
              });
        apply((state) => finishSubmission(state, progress));
        if (progress.outcome === "editable") {
          const editable = await recoverDraft(bridge, review, progress.draft_id);
          if (editable) {
            apply((state) => adoptDraft(state, editable));
            setDraftCandidates((items) => [
              editable,
              ...items.filter((item) => item.id !== editable.id),
            ]);
          }
        }
      } catch (reason) {
        const recovered = await recoverSubmissionStatus(
          bridge,
          review,
          attempt.attempt_id,
        );
        if (recovered) apply((state) => recoverSubmission(state, recovered));
        else apply((state) => failSubmission(state, drawerFailureMessage(reason)));
        setMessage(drawerFailureMessage(reason));
      } finally {
        setBusy(false);
      }
    },
    [apply, bridge, held, review],
  );

  const recoverDurable = useCallback(
    async (progress: SubmissionProgressDto): Promise<void> => {
      setMessage(null);
      try {
        const current = held.current;
        const matching =
          current.draft.remote?.id === progress.draft_id
            ? current.draft.remote
            : await bridge.getReviewDraft({
                review,
                draft_id: progress.draft_id,
              }).result;
        apply((state) =>
          recoverSubmission(
            state.draft.remote?.id === progress.draft_id
              ? state
              : adoptDraft(state, matching),
            progress,
          ),
        );
        setDraftCandidates((items) => [
          matching,
          ...items.filter((item) => item.id !== matching.id),
        ]);
      } catch (reason) {
        setMessage(drawerFailureMessage(reason));
      }
    },
    [apply, bridge, held, review],
  );

  const adoptCandidate = useCallback(
    (draft: DraftSnapshotDto): void => {
      try {
        apply((state) => adoptDraft(state, draft));
        setMessage(null);
      } catch (reason) {
        setMessage(drawerFailureMessage(reason));
      }
    },
    [apply],
  );

  const keepLocal = useCallback(async (): Promise<void> => {
    const current = held.current;
    if (!current.draft.conflict) return;
    const refusal = keepLocalDraftRefusal(current);
    if (refusal !== null) {
      setMessage(refusal);
      return;
    }
    setMessage(null);
    apply(keepLocalDraft);
    await save();
  }, [apply, held, save]);

  const takeRemote = useCallback((): void => {
    const remote = held.current.draft.conflict;
    if (!remote) return;
    setMessage(null);
    apply(chooseRemoteDraft);
    setDraftCandidates((items) => [
      remote,
      ...items.filter((item) => item.id !== remote.id),
    ]);
  }, [apply, held]);

  const dismissSuperseded = useCallback(
    (displacedByVersion: number): void => {
      try {
        apply((state) => dismissSupersededDraft(state, displacedByVersion));
      } catch (reason) {
        setMessage(drawerFailureMessage(reason));
      }
    },
    [apply],
  );

  const migrate = useCallback(async (): Promise<void> => {
    const current = held.current;
    const stale = current.draft.remote;
    if (!stale) return;
    setMessage(null);
    setBusy(true);
    try {
      const fresh = await bridge.createReviewDraft({
        review,
        revision: current.displayed.latestObservedRevision,
        content: portableDraftContent(stale),
      });
      apply((state) => forkDraftToCurrentRevision(state, fresh));
      setDraftCandidates((items) => [
        fresh,
        ...items.filter((item) => item.id !== fresh.id),
      ]);
    } catch (reason) {
      setMessage(drawerFailureMessage(reason));
    } finally {
      setBusy(false);
    }
  }, [apply, bridge, held, review]);

  // Discard is the composer controller's, taken verbatim, for the same reason
  // Edit and Delete are: the drawer button and the composer overflow entry must
  // send the identical `drafts.discard` call and leave the identical state
  // behind, and one implementation is how that stays true. The drawer's own
  // slot is cleared first because it wins over the composer's when both hold a
  // sentence: a refused discard has to report its own reason rather than an
  // older unrelated failure, and one that succeeds must not leave that failure
  // standing beside an emptied review.
  const discard = useCallback(async (): Promise<void> => {
    setMessage(null);
    await composer.discardReview();
  }, [composer.discardReview]);

  // A draft this session discarded is no longer a candidate to recover. The
  // composer publishes the id rather than the drawer deriving it, so a discard
  // taken from the in-diff overflow prunes this list exactly as one taken from
  // the button beside it does.
  const discardedDraftId = composer.discardedDraftId;
  useEffect(() => {
    if (discardedDraftId === null) return;
    setDraftCandidates((items) =>
      items.filter((item) => item.id !== discardedDraftId),
    );
  }, [discardedDraftId]);

  const groups = useMemo(
    () => groupPendingEntries(composer.pending),
    [composer.pending],
  );

  return {
    review,
    workflow,
    pending: composer.pending,
    groups,
    pendingCount: composer.pendingCount,
    capabilities: composer.capabilities,
    draftCandidates,
    recoveries,
    entryReason: composer.entryReason,
    busy: busy || composer.busy,
    message: message ?? composer.message,
    setSummary,
    setVerdict,
    save,
    submit,
    continueSubmission,
    recoverDurable,
    adoptCandidate,
    keepLocal,
    takeRemote,
    dismissSuperseded,
    migrate,
    discard,
    discardReason: composer.discardReason,
    discardSubject: composer.discardSubject,
    discardPrompt: composer.discardPrompt,
    removeEntry: composer.removeEntry,
    updateEntry: composer.updateEntry,
    clearMessage: () => {
      clearMessage();
      composer.clearMessage();
    },
  };
}

/**
 * The sentence for a failed drawer action. The workflow state refuses a change
 * with a sentence already written for the reader, so that sentence is what is
 * shown; without this every refusal here reported as "the local service could
 * not complete this read", which names the wrong thing and drops the cause.
 * Anything that is not a deliberate refusal keeps the generic reporting
 * boundary, so an untyped internal failure never reaches the reader as advice.
 */
export function drawerFailureMessage(reason: unknown): string {
  return reason instanceof ReviewWorkflowRefusal ||
    reason instanceof ComposerRefusal
    ? reason.message
    : reviewMutationError(reason);
}

/**
 * Why Submit cannot run on this state, worded for the state that actually
 * blocks it rather than reusing one sentence for every cause.
 */
export function submitRefusal(state: ReviewWorkflowState): string {
  const draft = state.draft.remote;
  if (!draft) return "No pending review is open to submit.";
  // Ordered by what actually holds the draft, most binding first, so a review
  // that is both conflicted and unsaved names the conflict rather than asking
  // for a save that the conflict is refusing.
  if (state.draft.conflict)
    return "This pending review was changed elsewhere. Resolve the conflict before submitting it.";
  if (state.draft.pendingSave)
    return "A draft save is in flight. Let it settle before submitting the review.";
  if (state.submission.pending)
    return "A submission step is already in flight.";
  if (state.submission.progress)
    return "This review already has a submission attempt. Resume or reconcile it below.";
  if (draft.state !== "editable")
    return "This draft is held by a submission attempt, so it cannot be submitted again.";
  if (draftNeedsRevisionRecovery(state))
    return "This review is bound to an earlier revision. Create a current-revision draft before submitting it.";
  if (state.draft.local.comments.some((comment) => comment.body.length === 0))
    return "Edit or remove empty pending comments before submitting the review.";
  if (state.draft.dirty)
    return "Save the summary and verdict before submitting the review.";
  return "This review cannot be submitted yet.";
}

/**
 * Why an unsaved draft cannot be saved right now. The base draft editor said
 * this for the empty-comment case, which only a draft written elsewhere can
 * reach, and losing it left Save disabled with no reason at all.
 */
export function saveRefusal(state: ReviewWorkflowState): string {
  const draft = state.draft.remote;
  if (!draft) return "No pending review is open to save.";
  if (state.draft.conflict)
    return "This pending review was changed elsewhere. Resolve the conflict before saving it.";
  if (state.draft.pendingSave)
    return "A draft save is already in flight.";
  if (draft.state !== "editable")
    return "This draft is held by a submission attempt, so it cannot be saved.";
  if (state.draft.local.comments.some((comment) => comment.body.length === 0))
    return "Edit or remove empty pending comments before saving.";
  return "This review cannot be saved yet.";
}

/**
 * The "Your review" button and the drawer it opens. The button carries the
 * pending count so the state of the review is readable without opening
 * anything, which is the whole point of the drawer existing.
 */
export function ReviewDrawerMount({
  controller,
  jump,
  edit,
  openExternal,
}: {
  readonly controller: ReviewDrawerController;
  /** Sends the reader to the entry's line, over the existing jump path. */
  readonly jump: (entry: PendingDraftEntry) => void;
  /** Opens the in-diff composer on the entry, in its edit shape. */
  readonly edit: (entry: PendingDraftEntry) => void;
  readonly openExternal: (url: string) => Promise<boolean>;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement | null>(null);
  const panel = useRef<HTMLDivElement | null>(null);
  // Closing drops whatever refusal is standing, so the next open is not
  // greeted by an answer to a question nobody asked. Opening drops nothing: a
  // write that failed while the drawer was shut is exactly the thing the
  // reader opens it to find.
  const close = useCallback((): void => {
    controller.clearMessage();
    setOpen(false);
    button.current?.focus();
  }, [controller]);
  // Focus enters the drawer when it opens, so Escape is heard by the drawer
  // rather than by whatever the reader last clicked outside it.
  useEffect(() => {
    if (open) panel.current?.focus();
  }, [open]);
  // Shift+C from the diff opens the same drawer this button opens. It only
  // opens: a drawer already on screen owns the keyboard, and Escape is what
  // closes it, so the key never becomes a toggle that could close a drawer the
  // reader is typing in.
  useEffect(
    () => subscribeDrawerOpen(controller.review, () => setOpen(true)),
    [controller.review],
  );
  return (
    <>
      <button
        ref={button}
        className="button button-secondary review-drawer-toggle"
        aria-expanded={open}
        aria-label={`Your review, ${controller.pendingCount} pending`}
        onClick={() => (open ? close() : setOpen(true))}
      >
        Your review
        <span className="badge review-drawer-badge">
          {controller.pendingCount}
        </span>
      </button>
      {open && (
        <ReviewDrawer
          controller={controller}
          close={close}
          jump={(entry) => {
            jump(entry);
            close();
          }}
          edit={(entry) => {
            edit(entry);
            close();
          }}
          openExternal={openExternal}
          panelRef={panel}
        />
      )}
    </>
  );
}

function ReviewDrawer({
  controller,
  close,
  jump,
  edit,
  openExternal,
  panelRef,
}: {
  readonly controller: ReviewDrawerController;
  readonly close: () => void;
  readonly jump: (entry: PendingDraftEntry) => void;
  readonly edit: (entry: PendingDraftEntry) => void;
  readonly openExternal: (url: string) => Promise<boolean>;
  readonly panelRef: { current: HTMLDivElement | null };
}): ReactNode {
  const [confirmation, setConfirmation] = useState<DrawerConfirmation | null>(
    null,
  );
  const workflow = controller.workflow;
  const draft = workflow.draft.remote;
  const content = workflow.draft.local;
  const capabilities = controller.capabilities;
  const stale = draftNeedsRevisionRecovery(workflow);
  const locked = Boolean(
    workflow.submission.pending ||
      (workflow.submission.progress &&
        workflow.submission.progress.outcome !== "editable"),
  );
  // Submit saves an unsent summary or verdict on the way, so a draft that is
  // merely unsaved does not block it. A draft that cannot be saved does: the
  // save would be refused, and the refusal that matters is the one holding the
  // draft, not the missing save.
  const submitReason =
    canStartSubmission(workflow) || canSaveDraft(workflow)
      ? null
      : submitRefusal(workflow);
  // Save is disabled both when there is nothing to save and when something
  // holds the draft. Only the second is worth a sentence: a draft that matches
  // the store needs no explanation for not being saved again.
  const saveReason =
    canSaveDraft(workflow) || !workflow.draft.dirty
      ? null
      : saveRefusal(workflow);
  // The panel is held as state as well as in the mount's ref, because the
  // keyboard map subscribes to the document that owns the panel and has to be
  // told when that node arrives.
  const [panelNode, setPanelNode] = useState<HTMLDivElement | null>(null);
  const verdicts = allowedVerdicts(capabilities);
  // What Escape does, written once. The panel's own handler answers it while
  // the focus is inside the drawer, including from the Summary field, which
  // the map's text-entry guard would otherwise hold; the map answers it from
  // everywhere else, which is where the focus sits after a click on the diff
  // behind this non-modal dialog. Both call this, so they cannot disagree
  // about the stages.
  const answerEscape = (): boolean => {
    // An armed confirmation is the nearest thing Escape can answer. It is
    // cancelled without closing, so backing out of a destructive question does
    // not also take away the drawer the reader was working in, and a second
    // Escape then closes as it always did.
    if (confirmation !== null) {
      setConfirmation(null);
      return true;
    }
    close();
    return true;
  };
  useReviewKeyMap(
    panelNode,
    [
      {
        key: "v",
        run: (): boolean => {
          // `v` records a verdict, so it stands down for the same reasons the
          // tiles are disabled: nothing to choose from, or a submission
          // holding the draft.
          const next = locked ? null : nextVerdict(verdicts, content.verdict);
          if (next === null) return false;
          controller.setVerdict(next);
          return true;
        },
      },
      { key: "Escape", run: answerEscape },
    ],
    // The diff behind this drawer stands down on exactly this predicate, so a
    // keystroke that landed there is claimed here rather than reaching nobody.
    { claimOutside: reviewDrawerIsOpen },
  );
  return (
    <div
      ref={(node) => {
        panelRef.current = node;
        setPanelNode(node);
      }}
      className="review-drawer"
      role="dialog"
      aria-label="Your review"
      tabIndex={-1}
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        event.preventDefault();
        answerEscape();
      }}
    >
      <header className="review-drawer-header">
        <h2 className="review-drawer-title">
          Your review, {controller.pendingCount} pending
        </h2>
        <button
          className="button button-quiet"
          aria-label="Close your review"
          onClick={close}
        >
          Close
        </button>
      </header>
      {controller.message && (
        <Notice kind="error">{controller.message}</Notice>
      )}
      {!draft && (
        <Notice kind="empty">
          No pending review is open. Start one from a diff line, or choose a
          preserved draft below.
        </Notice>
      )}
      {controller.draftCandidates.length > 1 && !draft && (
        <DraftRecoveryList
          drafts={controller.draftCandidates}
          recover={controller.adoptCandidate}
        />
      )}
      {controller.recoveries.length > 0 && !workflow.submission.progress && (
        <RecoveryList
          recoveries={controller.recoveries}
          recover={(item) => void controller.recoverDurable(item)}
        />
      )}
      {draft && stale && (
        <StaleRevisionNotice
          workflow={workflow}
          confirmation={confirmation}
          setConfirmation={setConfirmation}
          migrate={() => void controller.migrate()}
        />
      )}
      {workflow.draft.conflict && (
        <ConflictBanner
          workflow={workflow}
          keepLocal={() => void controller.keepLocal()}
          takeRemote={controller.takeRemote}
        />
      )}
      {workflow.draft.supersededLocalDrafts.map((superseded) => (
        <Notice kind="warning" key={superseded.displacedByVersion}>
          <p>
            Your text was replaced by stored version{" "}
            {superseded.displacedByVersion} and was never saved. Copy anything
            you still need, then dismiss it. A later conflict adds another copy
            rather than replacing this one.
          </p>
          <textarea
            readOnly
            aria-label={`My superseded draft text replaced by version ${superseded.displacedByVersion}`}
            value={draftContentTranscript(superseded.content)}
          />
          <button
            className="button button-secondary"
            onClick={() =>
              controller.dismissSuperseded(superseded.displacedByVersion)
            }
          >
            Dismiss my text replaced by version{" "}
            {superseded.displacedByVersion}
          </button>
        </Notice>
      ))}
      {draft && (
        <>
          <PendingEntryList
            groups={controller.groups}
            reason={controller.entryReason}
            busy={controller.busy}
            openExternal={openExternal}
            jump={jump}
            edit={edit}
            remove={(entry) => void controller.removeEntry(entry.id)}
            update={controller.updateEntry}
          />
          <label className="review-drawer-summary">
            Summary
            <textarea
              value={content.body}
              disabled={locked}
              onChange={(event) => controller.setSummary(event.target.value)}
            />
          </label>
          <VerdictTiles
            verdict={content.verdict}
            capabilities={capabilities}
            disabled={locked}
            setVerdict={controller.setVerdict}
          />
          <div className="review-drawer-actions">
            <button
              className="button button-secondary"
              disabled={!canSaveDraft(workflow) || controller.busy}
              title={saveReason ?? undefined}
              onClick={() => void controller.save()}
            >
              {workflow.draft.pendingSave ? "Saving…" : "Save summary and verdict"}
            </button>
            {saveReason !== null && (
              <p role="status" className="review-drawer-confirm">
                {saveReason}
              </p>
            )}
            <button
              className="button button-danger"
              disabled={controller.discardReason !== null || controller.busy}
              title={controller.discardReason ?? undefined}
              onClick={() => {
                if (confirmation !== "discard") {
                  setConfirmation("discard");
                  return;
                }
                setConfirmation(null);
                void controller.discard();
              }}
            >
              {confirmation === "discard"
                ? `Confirm discard of ${controller.discardSubject}`
                : "Discard review"}
            </button>
            <button
              className="button"
              disabled={submitReason !== null || controller.busy}
              title={submitReason ?? undefined}
              onClick={() => {
                if (confirmation !== "submit") {
                  setConfirmation("submit");
                  return;
                }
                setConfirmation(null);
                void controller.submit();
              }}
            >
              {confirmation === "submit"
                ? "Confirm submit review"
                : "Submit review"}
            </button>
          </div>
          {confirmation === "discard" && (
            <p role="status" className="review-drawer-confirm">
              {controller.discardPrompt}
            </p>
          )}
        </>
      )}
      {workflow.draft.preservedStaleDrafts.map((preserved) => (
        <section key={preserved.id} className="review-workflow-preserved-draft">
          <strong>Preserved old draft {preserved.id}</strong>
          <p>
            Revision {preserved.revision.head_sha}. Its content was not deleted
            or retargeted.
          </p>
          {preserved.comments
            .filter((comment) => comment.kind !== "general")
            .map((comment) => (
              <article key={comment.id}>
                <strong>{draftCommentLabel(comment)}</strong>
                <p>{comment.body}</p>
              </article>
            ))}
        </section>
      ))}
      {workflow.submission.progress && (
        <SubmissionProgress
          progress={workflow.submission.progress}
          message={workflow.submission.message}
          pending={workflow.submission.pending !== null}
          resume={() => void controller.continueSubmission("resume")}
          reconcile={(resolution) =>
            void controller.continueSubmission("reconcile", resolution)
          }
          confirmation={confirmation}
          setConfirmation={setConfirmation}
        />
      )}
      {draft && <DrawerFooter draft={draft} />}
    </div>
  );
}

/**
 * The S45 evidence, in the UI: the identity of the draft, the revision it was
 * captured at and the version the store last confirmed. The revision is short
 * form because that is the identity the reader compares against the diff
 * toolbar and the stale ribbons, which are short form too.
 */
function DrawerFooter({
  draft,
}: {
  readonly draft: DraftSnapshotDto;
}): ReactNode {
  return (
    <footer className="review-drawer-footer">
      <span data-draft-id={draft.id}>Draft {draft.id}</span>
      <span data-head-sha={draft.revision.head_sha}>
        Revision {draft.revision.head_sha.slice(0, 7)}
      </span>
      <span data-draft-version={String(draft.version)}>
        Version {draft.version}
      </span>
    </footer>
  );
}

function PendingEntryList({
  groups,
  reason,
  busy,
  openExternal,
  jump,
  edit,
  remove,
  update,
}: {
  readonly groups: readonly PendingEntryGroup[];
  readonly reason: string | null;
  readonly busy: boolean;
  readonly openExternal: (url: string) => Promise<boolean>;
  readonly jump: (entry: PendingDraftEntry) => void;
  readonly edit: (entry: PendingDraftEntry) => void;
  readonly remove: (entry: PendingDraftEntry) => void;
  readonly update: (entryId: string, body: string) => Promise<boolean>;
}): ReactNode {
  if (groups.length === 0)
    return (
      <Notice kind="empty">
        This review has no pending comments yet. Add one from a diff line.
      </Notice>
    );
  return (
    <section className="review-drawer-entries" aria-label="Pending comments">
      {groups.map((group) => (
        <section key={group.file} className="review-drawer-file">
          <h3 className="review-drawer-file-name">
            {group.file}
            <span className="review-drawer-file-count">
              {group.entries.length} pending
            </span>
          </h3>
          {group.entries.map((entry) => (
            <DrawerEntryRow
              key={entry.id}
              entry={entry}
              file={group.file}
              reason={reason}
              busy={busy}
              openExternal={openExternal}
              jump={() => jump(entry)}
              edit={() => edit(entry)}
              remove={() => remove(entry)}
              update={update}
            />
          ))}
        </section>
      ))}
    </section>
  );
}

/**
 * One pending entry as the drawer lists it. Jump is offered only where there
 * is somewhere to jump to, and Edit is withheld from a stale entry for the
 * same reason the in-diff card withholds it: the anchor it holds must never be
 * recaptured against the current revision.
 */
function DrawerEntryRow({
  entry,
  file,
  reason,
  busy,
  openExternal,
  jump,
  edit,
  remove,
  update,
}: {
  readonly entry: PendingDraftEntry;
  readonly file: string;
  readonly reason: string | null;
  readonly busy: boolean;
  readonly openExternal: (url: string) => Promise<boolean>;
  readonly jump: () => void;
  readonly edit: () => void;
  readonly remove: () => void;
  readonly update: (entryId: string, body: string) => Promise<boolean>;
}): ReactNode {
  const label = drawerEntryLabel(file, entry);
  const stale = entry.anchor?.stale === true;
  const target = pendingEntryTarget(entry);
  // Edited here rather than in the diff when there is no in-diff composer to
  // open on: a general comment and a reply have no diff line at all, and a
  // stale entry must never be opened on a current row, because the composer
  // there would sit on a line that is not the one the entry was stored
  // against. A body edit changes only the body: the stored anchor travels back
  // out exactly as it came in, which is what keeps S46's "nothing is silently
  // retargeted" true while still leaving no pending entry of any kind readable
  // but unchangeable.
  const inPlace = entry.anchor === null || stale;
  const [draftBody, setDraftBody] = useState<string | null>(null);
  return (
    <article
      className={`review-drawer-entry${stale ? " review-drawer-entry-stale" : ""}`}
      aria-label={`Pending review comment on ${label}`}
    >
      <header className="review-drawer-entry-heading">
        <span className="badge pending-badge">Pending</span>
        <span className="review-drawer-entry-anchor">{label}</span>
        <div className="review-drawer-entry-actions">
          {target && !stale && (
            <button
              className="button button-quiet"
              aria-label={`Jump to pending comment on ${label}`}
              onClick={jump}
            >
              Jump
            </button>
          )}
          <button
            className="button button-secondary"
            aria-label={`Edit pending comment on ${label}`}
            disabled={busy || reason !== null}
            title={reason ?? undefined}
            onClick={() => (inPlace ? setDraftBody(entry.body) : edit())}
          >
            Edit
          </button>
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
        <p className="review-drawer-entry-ribbon" role="status">
          {staleRibbonText(entry.anchor)}. It is listed here by the line it was
          stored against, and offers no jump, because no row of the current
          diff is known to be that line. Its text can still be edited here, and
          its anchor is never retargeted.
        </p>
      )}
      {draftBody === null ? (
        <div className="review-drawer-entry-body">
          <SafeMarkdown source={entry.body} openExternal={openExternal} />
        </div>
      ) : (
        <div className="review-drawer-entry-editor">
          <label>
            {inPlaceEditorLabel(entry, label)}
            <textarea
              value={draftBody}
              disabled={busy}
              onChange={(event) => setDraftBody(event.target.value)}
            />
          </label>
          <div className="review-workflow-row">
            <button
              className="button"
              disabled={busy || draftBody.length === 0}
              onClick={() => {
                const body = draftBody;
                setDraftBody(null);
                void update(entry.id, body);
              }}
            >
              {`Save pending comment on ${label}`}
            </button>
            <button
              className="button button-secondary"
              disabled={busy}
              onClick={() => setDraftBody(null)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </article>
  );
}

/**
 * What the in-drawer editor is called. A stale entry names its own anchor,
 * because the reader needs to know which stored line the text they are editing
 * belongs to when no current row can be pointed at.
 */
function inPlaceEditorLabel(entry: PendingDraftEntry, label: string): string {
  if (entry.anchor) return `Edit pending stale comment on ${label}`;
  return entry.kind === "reply"
    ? "Edit pending reply"
    : "Edit pending general comment";
}

/**
 * The verdict, as the three radio tiles both forges offer. A capability the
 * forge or the permission does not grant hides its tile rather than showing a
 * dead one, and what happens with no tile selected is stated rather than left
 * to be discovered on submit.
 */
function VerdictTiles({
  verdict,
  capabilities,
  disabled,
  setVerdict,
}: {
  readonly verdict: ReviewVerdict | null;
  readonly capabilities: ReviewMutationCapabilitiesDto | null;
  readonly disabled: boolean;
  readonly setVerdict: (verdict: ReviewVerdict | null) => void;
}): ReactNode {
  const labels: Readonly<Record<ReviewVerdict, string>> = {
    comment: "Comment",
    approve: "Approve",
    request_changes: "Request changes",
  };
  // The same list `v` cycles, so the tiles and the key can never disagree
  // about which verdicts this review records or in which order.
  const allowed = allowedVerdicts(capabilities).map((value) => ({
    value,
    label: labels[value],
  }));
  if (capabilities === null)
    return (
      <section className="review-drawer-verdict" aria-label="Verdict">
        <p role="status">Verdict support for this review is still loading.</p>
      </section>
    );
  if (allowed.length === 0)
    return (
      <section className="review-drawer-verdict" aria-label="Verdict">
        <p role="status">
          This review records no verdict on this forge or with this permission.
          Submitting posts the summary and the pending comments without one.
        </p>
      </section>
    );
  return (
    <section className="review-drawer-verdict" aria-label="Verdict">
      <h3 className="review-drawer-verdict-title">Verdict</h3>
      <div className="review-drawer-verdict-tiles" role="radiogroup" aria-label="Verdict">
        {allowed.map((tile) => (
          <label key={tile.value} className="review-drawer-verdict-tile">
            <input
              type="radio"
              name="review-drawer-verdict"
              value={tile.value}
              checked={verdict === tile.value}
              disabled={disabled}
              onChange={() => setVerdict(tile.value)}
            />
            <span>{tile.label}</span>
          </label>
        ))}
      </div>
      {verdict === null ? (
        <p role="status">
          {capabilities.comment_verdict
            ? "No verdict selected. Submitting posts this review as a comment."
            : "No verdict selected. Submitting posts the summary and the pending comments without a verdict."}
        </p>
      ) : (
        <button
          className="button button-quiet"
          disabled={disabled}
          onClick={() => setVerdict(null)}
        >
          Clear verdict
        </button>
      )}
    </section>
  );
}

function StaleRevisionNotice({
  workflow,
  confirmation,
  setConfirmation,
  migrate,
}: {
  readonly workflow: ReviewWorkflowState;
  readonly confirmation: DrawerConfirmation | null;
  readonly setConfirmation: (value: DrawerConfirmation | null) => void;
  readonly migrate: () => void;
}): ReactNode {
  const content = workflow.draft.local;
  const generalCount = content.comments.filter(
    (item) => item.kind === "general",
  ).length;
  const inlineCount = content.comments.filter(
    (item) => item.kind === "inline",
  ).length;
  const replyCount = content.comments.filter(
    (item) => item.kind === "reply",
  ).length;
  return (
    <Notice kind="warning">
      <p>
        This draft remains preserved at revision{" "}
        {workflow.draft.remote?.revision.head_sha}. It cannot be submitted or
        receive a new inline anchor at the current revision.
      </p>
      <p>
        A separate current-revision draft will copy the body, verdict, and{" "}
        {generalCount} general comment(s). The {inlineCount} inline comment(s)
        and {replyCount} reply/replies remain on this old draft for deliberate
        recreation after checking their current targets.
      </p>
      <button
        className="button button-secondary"
        disabled={
          workflow.draft.dirty ||
          Boolean(workflow.draft.pendingSave) ||
          Boolean(workflow.draft.conflict)
        }
        onClick={() => {
          if (confirmation !== "migrate-draft") {
            setConfirmation("migrate-draft");
            return;
          }
          setConfirmation(null);
          migrate();
        }}
      >
        {confirmation === "migrate-draft"
          ? "Confirm create separate current-revision draft"
          : "Create current-revision draft"}
      </button>
    </Notice>
  );
}

function ConflictBanner({
  workflow,
  keepLocal,
  takeRemote,
}: {
  readonly workflow: ReviewWorkflowState;
  readonly keepLocal: () => void;
  readonly takeRemote: () => void;
}): ReactNode {
  const conflict = workflow.draft.conflict;
  if (!conflict) return null;
  return (
    <Notice kind="warning">
      <p>
        The stored draft is now version {conflict.version}
        {workflow.draft.conflictHeldVersion === null
          ? ""
          : `, and you were editing version ${workflow.draft.conflictHeldVersion}`}
        . Neither side was discarded. Both are shown below; choose one
        deliberately.
      </p>
      <div className="review-workflow-conflict">
        <article>
          <strong>Your text, not stored</strong>
          <p className="review-workflow-thread-meta">Typed in this window.</p>
          <textarea
            readOnly
            aria-label="My unsaved draft text"
            value={draftContentTranscript(workflow.draft.local)}
          />
        </article>
        <article>
          <strong>Stored draft, version {conflict.version}</strong>
          <p className="review-workflow-thread-meta">
            Saved outside this window, last updated{" "}
            {formatDate(conflict.updated_at)}. This draft store records no
            author, so the writer is not identified.
          </p>
          <textarea
            readOnly
            aria-label="Stored draft text"
            value={draftContentTranscript(conflict)}
          />
        </article>
      </div>
      <div className="review-workflow-row">
        <button className="button" onClick={keepLocal}>
          Keep my text and save over version {conflict.version}
        </button>
        <button className="button button-secondary" onClick={takeRemote}>
          Take the stored version and keep mine to copy
        </button>
      </div>
    </Notice>
  );
}

const STEP_LABELS: Readonly<Record<SubmissionStepDto["kind"], string>> =
  Object.freeze({
    general_comment: "General comment",
    inline_comment: "Inline comment",
    reply: "Reply",
    body: "Review body",
    verdict: "Verdict",
    github_review: "GitHub review",
  });

/**
 * The step list the engine already reports, with each step's confirmed or
 * unknown standing. A step that is neither is one the attempt has not reached,
 * which is a different thing from one whose remote result is unknown, so the
 * two are never worded the same.
 */
function stepStanding(
  progress: SubmissionProgressDto,
  step: SubmissionStepDto,
): string {
  if (progress.completed_step_ids.includes(step.id)) return "confirmed";
  if (progress.unknown_step_ids.includes(step.id)) return "unknown";
  return "not started";
}

function SubmissionProgress({
  progress,
  message,
  pending,
  resume,
  reconcile,
  confirmation,
  setConfirmation,
}: {
  readonly progress: SubmissionProgressDto;
  readonly message: string | null;
  readonly pending: boolean;
  readonly resume: () => void;
  readonly reconcile: (resolution: ReconciliationResolution) => void;
  readonly confirmation: DrawerConfirmation | null;
  readonly setConfirmation: (value: DrawerConfirmation | null) => void;
}): ReactNode {
  return (
    <section className="review-workflow-progress" aria-live="polite">
      <h2>Submission progress</h2>
      {message && <p>{message}</p>}
      <p>
        {progress.completed_step_ids.length} confirmed ·{" "}
        {progress.unknown_step_ids.length} unknown
      </p>
      <ol className="review-drawer-steps" aria-label="Submission steps">
        {progress.steps.map((step) => (
          <li key={step.id} data-step-standing={stepStanding(progress, step)}>
            {STEP_LABELS[step.kind]}: {stepStanding(progress, step)}
          </li>
        ))}
      </ol>
      {progress.failure && <Notice kind="error">{progress.failure.message}</Notice>}
      {progress.outcome === "paused" && (
        <>
          <p>
            Confirmed steps stay excluded. Resume retries the next definitely
            unconfirmed step.
          </p>
          <button className="button" disabled={pending} onClick={resume}>
            Resume confirmed attempt
          </button>
        </>
      )}
      {progress.outcome === "unknown" && (
        <>
          <Notice kind="warning">
            Retry remaining can repeat an unconfirmed remote write. Confirmed
            receipt steps stay excluded. Mark submitted records your assertion
            after you inspect the forge; it is not a verified receipt.
          </Notice>
          <div className="review-workflow-row">
            <button
              className="button"
              disabled={pending}
              onClick={() =>
                confirmation === "reconcile:retry_remaining"
                  ? reconcile("retry_remaining")
                  : setConfirmation("reconcile:retry_remaining")
              }
            >
              {confirmation === "reconcile:retry_remaining"
                ? "Confirm possible repeat of remaining writes"
                : "Retry only remaining steps"}
            </button>
            <button
              className="button button-secondary"
              disabled={pending}
              onClick={() => reconcile("return_editable")}
            >
              Return draft to editing
            </button>
            <button
              className="button button-secondary"
              disabled={pending}
              onClick={() =>
                confirmation === "reconcile:mark_submitted"
                  ? reconcile("mark_submitted")
                  : setConfirmation("reconcile:mark_submitted")
              }
            >
              {confirmation === "reconcile:mark_submitted"
                ? "Confirm inspected remote is submitted"
                : "Mark submitted"}
            </button>
          </div>
        </>
      )}
    </section>
  );
}

function DraftRecoveryList({
  drafts,
  recover,
}: {
  readonly drafts: readonly DraftSnapshotDto[];
  readonly recover: (item: DraftSnapshotDto) => void;
}): ReactNode {
  return (
    <section className="review-workflow-recovery">
      <h2>Choose a preserved draft</h2>
      {drafts.map((item) => (
        <button
          key={item.id}
          className="button button-secondary"
          onClick={() => recover(item)}
        >
          Draft {item.id} at {item.revision.head_sha}, version {item.version}
        </button>
      ))}
    </section>
  );
}

function RecoveryList({
  recoveries,
  recover,
}: {
  readonly recoveries: readonly SubmissionProgressDto[];
  readonly recover: (item: SubmissionProgressDto) => void;
}): ReactNode {
  return (
    <section className="review-workflow-recovery">
      <h2>Durable submission recovery</h2>
      {recoveries.map((item) => (
        <button
          key={item.attempt_id}
          className="button button-secondary"
          onClick={() => recover(item)}
        >
          Recover {item.outcome} attempt with {item.completed_step_ids.length}{" "}
          confirmed step(s)
        </button>
      ))}
    </section>
  );
}

/**
 * The whole of one draft's content as text the reader can compare and copy.
 * Both sides of the S46 conflict are read from this, and a retained text that
 * was displaced is kept in it, so it carries the body itself rather than a
 * label for it, and separates entries by a blank line.
 */
export function draftContentTranscript(content: DraftContentInputDto): string {
  const sections = [content.body, `Verdict: ${content.verdict ?? "none"}`];
  for (const comment of content.comments)
    sections.push(`${draftCommentLabel(comment)}\n${comment.body}`);
  return sections.join("\n\n");
}

/**
 * How one draft entry is named in a transcript and in the preserved old draft
 * list. It carries the entry's own id, because two entries can share one
 * anchor and a reader comparing two versions has to tell them apart, and it
 * carries the side and the stale marker, because a stale old-side anchor is
 * exactly what S46 asks to stay legible after a revision moves.
 */
export function draftCommentLabel(comment: DraftCommentInputDto): string {
  if (comment.kind === "general") return `General comment ${comment.id}`;
  if (comment.kind === "reply")
    return `Reply ${comment.id} to discussion ${comment.thread_id}`;
  const line =
    comment.anchor.side === "old"
      ? comment.anchor.old_line
      : comment.anchor.new_line;
  const stale = comment.anchor.stale === true ? " \u00b7 stale anchor" : "";
  return `Inline ${comment.id} \u00b7 ${comment.anchor.new_path} \u00b7 ${comment.anchor.side} line ${line}${stale}`;
}

function Notice({
  kind,
  children,
}: {
  readonly kind: string;
  readonly children: ReactNode;
}): ReactNode {
  return (
    <div
      className={`notice notice-${kind}`}
      role={kind === "error" ? "alert" : "status"}
    >
      {children}
    </div>
  );
}

/**
 * The durable attempt that matches this draft and frozen version, or null when
 * none is retained. Shared with the review panel's own recovery so a start
 * whose response was lost is never replayed as a second attempt.
 */
async function recoverLatestSubmission(
  bridge: InlineComposerBridge,
  review: string,
  draftId: string,
  frozenVersion: number,
): Promise<SubmissionProgressDto | null> {
  try {
    const result = await bridge.listReviewSubmissions({
      review,
      max_items: 100,
    }).result;
    return (
      result.attempts.find(
        (attempt) =>
          attempt.draft_id === draftId &&
          attempt.frozen_version === frozenVersion,
      ) ?? null
    );
  } catch {
    return null;
  }
}

async function recoverSubmissionStatus(
  bridge: InlineComposerBridge,
  review: string,
  attemptId: string,
): Promise<SubmissionProgressDto | null> {
  try {
    return await bridge.getReviewSubmission({ review, attempt_id: attemptId })
      .result;
  } catch {
    return null;
  }
}
