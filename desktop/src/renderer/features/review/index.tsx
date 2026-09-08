import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import type {
  DesktopBridge,
  DiscussionDto,
  ReviewRevisionDto,
  ReviewSnapshotDto,
} from "../../../shared/bridge.js";
import type {
  DraftCommentInputDto,
  DraftContentInputDto,
  DraftSnapshotDto,
  GeneralCommentParams,
  InlineCommentParams,
  ReconciliationResolution,
  ReviewAction,
  ReviewActionCapabilitiesDto,
  ReviewDesktopBridge,
  ReviewMutationCapabilitiesDto,
  ReviewVerdict,
  SubmissionProgressDto,
} from "../../../shared/review.js";
import type {
  AppRoute,
  FeatureContribution,
  FeatureContext,
  InlineAnchorSelection,
} from "../../core/navigation.js";
import { safeError } from "../../core/presentation.js";
import { ReviewHeader } from "../review-detail/index.js";
import {
  acknowledgeQuickUncertainty,
  adoptDraft,
  beginDraftSave,
  beginQuickIntent,
  beginSubmission,
  canCaptureDraftInline,
  canStartSubmission,
  captureDraftAnchor,
  conflictDraftSave,
  createReviewWorkflowState,
  draftNeedsRevisionRecovery,
  editDraft,
  failSubmission,
  failDraftSave,
  finishDraftSave,
  finishSubmission,
  forkDraftToCurrentRevision,
  keepLocalDraft,
  markQuickIntentUncertain,
  observeReviewRevision,
  portableDraftContent,
  recoverSubmission,
  recoverQuickIntent,
  rejectQuickIntent,
  settleQuickIntent,
  type DraftAnchorSelection,
  type ReviewWorkflowState,
} from "./state.js";

const ACTIVE_DRAFT_STATES = ["editable", "submitting", "partial", "unknown"] as const;
const workflowCache = new Map<string, ReviewWorkflowState>();
type Confirmation =
  | ReviewAction
  | "submit"
  | "migrate-draft"
  | `verdict:${ReviewVerdict}`
  | `reconcile:${ReconciliationResolution}`;

interface ComposerBuffers {
  general: string;
  readonly inline: Map<string, string>;
  readonly replies: Map<string, string>;
}

const composerCache = new Map<string, ComposerBuffers>();

interface ReviewFeatureBridge extends DesktopBridge, ReviewDesktopBridge {}

export function createReviewFeature(
  bridge: ReviewDesktopBridge,
): FeatureContribution {
  return {
    id: "review.workflow",
    order: 24,
    reviewPanel: { id: "discussions", label: "Discussions", order: 40 },
    commands: [
      {
        id: "review.workflow.open",
        label: "Review workflow",
        order: 40,
        isVisible: (_context, route) => route.kind === "review",
        disabledReason: () => null,
        run: (context, route) => {
          if (route.kind === "review")
            context.navigate({ ...route, panel: "discussions" });
        },
      },
    ],
    matches: (route) => route.kind === "review" && route.panel === "discussions",
    render: (context, route) =>
      route.kind === "review" && route.panel === "discussions" ? (
        <ReviewWorkflow
          bridge={bridge as ReviewFeatureBridge}
          context={context}
          route={route}
        />
      ) : null,
  };
}

function ReviewWorkflow({
  bridge,
  context,
  route,
}: {
  readonly bridge: ReviewFeatureBridge;
  readonly context: FeatureContext;
  readonly route: Extract<AppRoute, { kind: "review" }>;
}): ReactNode {
  const review = route.item.handle;
  const [snapshot, setSnapshot] = useState<ReviewSnapshotDto | null>(null);
  const [discussions, setDiscussions] = useState<readonly DiscussionDto[]>([]);
  const [mutationCapabilities, setMutationCapabilities] =
    useState<ReviewMutationCapabilitiesDto | null>(null);
  const [actionCapabilities, setActionCapabilities] =
    useState<ReviewActionCapabilitiesDto | null>(null);
  const [draftCandidates, setDraftCandidates] =
    useState<readonly DraftSnapshotDto[]>([]);
  const [recoveries, setRecoveries] =
    useState<readonly SubmissionProgressDto[]>([]);
  const [workflow, setWorkflow] = useState<ReviewWorkflowState | null>(
    workflowCache.get(review) ?? null,
  );
  const workflowRef = useRef(workflow);
  const anchorRef = useRef(context.inlineAnchor);
  const [error, setError] = useState<string | null>(null);
  const initialBuffers = buffersFor(review);
  const initialAnchorKey = anchorIdentity(context.inlineAnchor);
  const [generalBody, setGeneralBodyState] = useState(initialBuffers.general);
  const [inlineBody, setInlineBodyState] = useState(
    initialAnchorKey ? initialBuffers.inline.get(initialAnchorKey) ?? "" : "",
  );
  const [reply, setReply] = useState<{ readonly id: string; readonly body: string } | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [mergeOptions, setMergeOptions] = useState({
    squash: false,
    cleanup: false,
  });
  const quickBlocked =
    workflow?.quick?.status === "sending" || workflow?.quick?.status === "unknown";
  const setGeneralBody = (body: string): void => {
    buffersFor(review).general = body;
    setGeneralBodyState(body);
  };
  const setInlineBody = (body: string): void => {
    const key = anchorIdentity(anchorRef.current);
    if (key) buffersFor(review).inline.set(key, body);
    setInlineBodyState(body);
  };
  const setReplyBuffer = (
    value: { readonly id: string; readonly body: string } | null,
  ): void => {
    const next =
      value && value.body === "" && reply?.id !== value.id
        ? { ...value, body: buffersFor(review).replies.get(value.id) ?? "" }
        : value;
    if (next) buffersFor(review).replies.set(next.id, next.body);
    setReply(next);
  };
  useEffect(() => {
    anchorRef.current = context.inlineAnchor;
    const key = anchorIdentity(context.inlineAnchor);
    setInlineBodyState(key ? buffersFor(review).inline.get(key) ?? "" : "");
  }, [context.inlineAnchor]);
  useEffect(() => {
    const buffers = buffersFor(review);
    setGeneralBodyState(buffers.general);
    const key = anchorIdentity(context.inlineAnchor);
    setInlineBodyState(key ? buffers.inline.get(key) ?? "" : "");
    setReply(null);
  }, [context.inlineAnchor, review]);
  const apply = useCallback(
    (change: (current: ReviewWorkflowState) => ReviewWorkflowState): ReviewWorkflowState => {
      const current = workflowRef.current;
      if (!current) throw new Error("Review workflow is not ready");
      const next = change(current);
      workflowRef.current = next;
      workflowCache.set(review, next);
      setWorkflow(next);
      return next;
    },
    [review],
  );

  useEffect(() => {
    let current = true;
    const reads = [
      bridge.getReview(review),
      bridge.listDiscussions(review),
      bridge.getReviewMutationCapabilities(review),
      bridge.getReviewActionCapabilities(review),
      bridge.listReviewDrafts({ review, states: ACTIVE_DRAFT_STATES, max_items: 100 }),
      bridge.listReviewSubmissions({ review, max_items: 100 }),
    ] as const;
    void Promise.all([
      reads[0].result,
      reads[1].result,
      reads[2].result,
      reads[3].result,
      reads[4].result,
      reads[5].result,
    ] as const)
      .then(([detail, discussionResult, mutationResult, actionResult, draftResult, submissionResult]) => {
        if (!current) return;
        if (!detail.revision)
          throw new Error("The current review revision is unavailable.");
        setSnapshot(detail);
        setDiscussions(discussionResult.discussions);
        setMutationCapabilities(mutationResult.capabilities);
        setActionCapabilities(actionResult.capabilities);
        setDraftCandidates(draftResult.drafts);
        setRecoveries(submissionResult.attempts);
        const cached = workflowCache.get(review);
        const next = cached
          ? observeReviewRevision(cached, detail.revision)
          : createReviewWorkflowState(review, detail.revision);
        workflowRef.current = next;
        workflowCache.set(review, next);
        setWorkflow(next);
      })
      .catch((reason: unknown) => current && setError(safeError(reason)));
    return () => {
      current = false;
      for (const item of reads) void bridge.cancelRead(item.requestToken);
    };
  }, [bridge, review]);

  const startReview = async (): Promise<void> => {
    if (!workflow || !snapshot?.revision) return;
    setError(null);
    if (draftCandidates.length > 1) {
      setError("Multiple active drafts were recovered. Choose one before editing.");
      return;
    }
    try {
      const draft =
        draftCandidates[0] ??
        (await bridge.createReviewDraft({
          review,
          revision: workflow.displayed.revision,
        }));
      apply((current) => adoptDraft(current, draft));
      setDraftCandidates([draft]);
    } catch (reason) {
      setError(safeError(reason));
    }
  };

  const saveDraft = async (): Promise<void> => {
    const current = apply(beginDraftSave);
    const remote = current.draft.remote;
    const pending = current.draft.pendingSave;
    if (!remote || !pending) return;
    setError(null);
    try {
      const saved = await bridge.saveReviewDraft({
        review,
        draft_id: remote.id,
        expected_version: pending.expectedVersion,
        content: pending.content,
      });
      apply((latest) => finishDraftSave(latest, saved));
      setDraftCandidates([saved]);
    } catch (reason) {
      if (isConflictError(reason)) {
        const remoteDraft = await recoverDraft(bridge, review, remote.id);
        if (remoteDraft)
          apply((latest) => conflictDraftSave(latest, remoteDraft));
        else apply(failDraftSave);
      } else {
        apply(failDraftSave);
      }
      setError(safeError(reason));
    }
  };

  const migrateDraft = async (): Promise<void> => {
    const current = workflowRef.current;
    const stale = current?.draft.remote;
    if (!current || !stale || !snapshot?.revision) return;
    if (confirmation !== "migrate-draft") {
      setConfirmation("migrate-draft");
      return;
    }
    setError(null);
    setConfirmation(null);
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
      setError(safeError(reason));
    }
  };

  const quickComment = async (
    body: string,
    anchor: InlineAnchorSelection | null,
  ): Promise<void> => {
    if (!body || !workflow) return;
    const operationId = newOperationId("comment");
    const command = anchor
      ? { operation_id: operationId, review, revision: anchor.revision, anchor: mutationAnchor(anchor), body }
      : { operation_id: operationId, review, body };
    apply((current) => beginQuickIntent(current, operationId, command));
    setError(null);
    try {
      const outcome = anchor
        ? await bridge.postInlineReviewComment(command as InlineCommentParams)
        : await bridge.postReviewComment(command as GeneralCommentParams);
      apply((current) => settleQuickIntent(current, operationId, outcome));
      if (anchor) setInlineBody("");
      else setGeneralBody("");
    } catch (reason) {
      if (isUncertainError(reason))
        apply((current) => markQuickIntentUncertain(current, operationId));
      else
        apply((current) => rejectQuickIntent(current, operationId, safeError(reason)));
      setError(safeError(reason));
    }
  };

  const addDraftComment = async (
    body: string,
    anchor: InlineAnchorSelection | null,
  ): Promise<void> => {
    if (!workflow?.draft.remote || !body) return;
    let comment: DraftCommentInputDto;
    if (anchor) {
      const draftAnchor = await captureDraftAnchor(
        draftSelection(anchor),
        () => {
          const current = anchorRef.current;
          return current ? draftSelection(current) : null;
        },
      );
      comment = Object.freeze({
        id: crypto.randomUUID(),
        kind: "inline",
        body,
        anchor: draftAnchor,
      });
    } else {
      comment = Object.freeze({ id: crypto.randomUUID(), kind: "general", body });
    }
    apply((current) =>
      editDraft(current, {
        ...current.draft.local,
        comments: [...current.draft.local.comments, comment],
      }),
    );
    if (anchor) setInlineBody("");
    else setGeneralBody("");
  };

  const sendComment = async (
    body: string,
    anchor: InlineAnchorSelection | null,
  ): Promise<void> => {
    try {
      if (workflow?.draft.remote) await addDraftComment(body, anchor);
      else await quickComment(body, anchor);
    } catch (reason) {
      setError(safeError(reason));
    }
  };

  const sendReply = async (): Promise<void> => {
    if (!reply || !snapshot?.revision || !reply.body) return;
    if (workflow?.draft.remote) {
      apply((current) =>
        editDraft(current, {
          ...current.draft.local,
          comments: [
            ...current.draft.local.comments,
            { id: crypto.randomUUID(), kind: "reply", thread_id: reply.id, body: reply.body },
          ],
        }),
      );
      buffersFor(review).replies.delete(reply.id);
      setReply(null);
      return;
    }
    const operationId = newOperationId("reply");
    const command = {
      operation_id: operationId,
      review,
      revision: workflow?.displayed.revision ?? snapshot.revision,
      discussion_id: reply.id,
      body: reply.body,
    };
    if (!workflow) return;
    apply((current) => beginQuickIntent(current, operationId, command));
    try {
      const outcome = await bridge.replyReviewDiscussion(command);
      apply((current) => settleQuickIntent(current, operationId, outcome));
      buffersFor(review).replies.delete(reply.id);
      setReply(null);
    } catch (reason) {
      apply((current) =>
        isUncertainError(reason)
          ? markQuickIntentUncertain(current, operationId)
          : rejectQuickIntent(current, operationId, safeError(reason)),
      );
      setError(safeError(reason));
    }
  };

  const resolveDiscussion = async (
    discussion: DiscussionDto,
  ): Promise<void> => {
    if (!workflow) return;
    const operationId = newOperationId("resolve");
    const command = {
      operation_id: operationId,
      review,
      revision: workflow.displayed.revision,
      discussion_id: discussion.id,
      resolved: !discussion.is_resolved,
    };
    apply((current) => beginQuickIntent(current, operationId, command));
    try {
      const outcome = await bridge.resolveReviewDiscussion(command);
      apply((current) => settleQuickIntent(current, operationId, outcome));
      if (outcome.outcome === "known")
        setDiscussions((items) =>
          items.map((item) =>
            item.id === discussion.id
              ? { ...item, is_resolved: command.resolved }
              : item,
          ),
        );
    } catch (reason) {
      apply((current) =>
        isUncertainError(reason)
          ? markQuickIntentUncertain(current, operationId)
          : rejectQuickIntent(current, operationId, safeError(reason)),
      );
      setError(safeError(reason));
    }
  };

  const submitDraft = async (): Promise<void> => {
    if (!workflow?.draft.remote || !canStartSubmission(workflow)) return;
    const current = apply((state) => beginSubmission(state, "start"));
    const draft = current.draft.remote;
    if (!draft) return;
    setConfirmation(null);
    try {
      const progress = await bridge.startReviewSubmission({
        review,
        draft_id: draft.id,
        expected_version: draft.version,
      });
      apply((state) => finishSubmission(state, progress));
      setRecoveries([progress]);
    } catch (reason) {
      const recovered = await recoverLatestSubmission(
        bridge,
        review,
        draft.id,
        draft.version,
      );
      if (recovered) {
        apply((state) => recoverSubmission(state, recovered));
        setRecoveries((items) => [recovered, ...items.filter((item) => item.attempt_id !== recovered.attempt_id)]);
      } else {
        apply((state) => failSubmission(state, safeError(reason)));
      }
      setError(safeError(reason));
    }
  };

  const continueSubmission = async (
    mode: "resume" | "reconcile",
    resolution?: ReconciliationResolution,
  ): Promise<void> => {
    if (!workflow?.submission.progress) return;
    const attempt = workflow.submission.progress;
    apply((state) => beginSubmission(state, mode));
    setConfirmation(null);
    try {
      const progress =
        mode === "resume"
          ? await bridge.resumeReviewSubmission({ review, attempt_id: attempt.attempt_id })
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
      else apply((state) => failSubmission(state, safeError(reason)));
      setError(safeError(reason));
    }
  };

  const runAction = async (action: ReviewAction): Promise<void> => {
    if (!workflow) return;
    if (confirmation !== action) {
      setConfirmation(action);
      return;
    }
    const operationId = newOperationId(action);
    const params = {
      operation_id: operationId,
      review,
      revision: workflow.displayed.revision,
    };
    apply((current) => beginQuickIntent(current, operationId, { action, ...params }));
    setConfirmation(null);
    try {
      const receipt =
        action === "merge"
          ? await bridge.mergeReview({
              ...params,
              squash: mergeOptions.squash,
              source_cleanup: mergeOptions.cleanup
                ? { branch: route.item.summary.source_branch }
                : null,
            })
          : action === "close"
            ? await bridge.closeReview(params)
            : action === "reopen"
              ? await bridge.reopenReview(params)
              : await bridge.unapproveReview(params);
      apply((current) => settleQuickIntent(current, operationId, receipt));
    } catch (reason) {
      apply((current) =>
        isUncertainError(reason)
          ? markQuickIntentUncertain(current, operationId)
          : rejectQuickIntent(current, operationId, safeError(reason)),
      );
      setError(safeError(reason));
    }
  };

  const runQuickVerdict = async (verdict: ReviewVerdict): Promise<void> => {
    if (!workflow || workflow.draft.remote) return;
    const confirmationId: Confirmation = `verdict:${verdict}`;
    if (verdict !== "comment" && confirmation !== confirmationId) {
      setConfirmation(confirmationId);
      return;
    }
    const operationId = newOperationId(verdict);
    const command = {
      operation_id: operationId,
      review,
      revision: workflow.displayed.revision,
      verdict,
      body: verdict === "approve" ? "" : generalBody,
    };
    apply((current) => beginQuickIntent(current, operationId, command));
    setConfirmation(null);
    try {
      const outcome = await bridge.submitReviewVerdict(command);
      apply((current) => settleQuickIntent(current, operationId, outcome));
      if (verdict !== "approve") setGeneralBody("");
    } catch (reason) {
      apply((current) =>
        isUncertainError(reason)
          ? markQuickIntentUncertain(current, operationId)
          : rejectQuickIntent(current, operationId, safeError(reason)),
      );
      setError(safeError(reason));
    }
  };

  const recoverQuickAction = async (): Promise<void> => {
    const quick = workflowRef.current?.quick;
    if (!quick || quick.status !== "unknown" || !isActionCommand(quick.command))
      return;
    const read = bridge.getReviewActionReceipt({
      operation_id: quick.operationId,
      review,
      revision: quick.command.revision,
      action: quick.command.action,
    });
    try {
      const result = await read.result;
      const receipt = result.receipt;
      if (receipt)
        apply((current) => recoverQuickIntent(current, quick.operationId, receipt));
      else
        setError("No retained action receipt is available. Inspect the forge before acknowledging uncertainty.");
    } catch (reason) {
      setError(safeError(reason));
    }
  };

  return (
    <>
      <ReviewHeader route={route} navigate={context.navigate} panels={context.reviewPanels} />
      <section className="review-workflow-shell" aria-label="Review workflow">
        <div className="review-workflow-toolbar">
          {!workflow?.draft.remote ? (
            <button className="button" disabled={!snapshot?.revision} onClick={() => void startReview()}>
              {draftCandidates.length === 1 ? "Resume review" : "Start review"}
            </button>
          ) : (
            <strong>Draft review active</strong>
          )}
          <ActionButtons
            capabilities={actionCapabilities}
            confirmation={confirmation}
            blocked={quickBlocked}
            mergeOptions={mergeOptions}
            setMergeOptions={(value) => {
              setMergeOptions(value);
              if (confirmation === "merge") setConfirmation(null);
            }}
            run={runAction}
          />
        </div>
        {error && <Notice kind="error">{error}</Notice>}
        {workflow?.quick?.message && (
          <Notice kind="warning">
            <span>{workflow.quick.message}</span>
            {workflow.quick.status === "unknown" && (
              <span className="review-workflow-row">
                {isActionCommand(workflow.quick.command) && (
                  <button className="button button-secondary" onClick={() => void recoverQuickAction()}>
                    Check retained receipt
                  </button>
                )}
                <button
                  className="button button-secondary"
                  onClick={() =>
                    apply((current) =>
                      acknowledgeQuickUncertainty(current, workflow.quick!.operationId),
                    )
                  }
                >
                  I inspected the forge; acknowledge uncertainty
                </button>
              </span>
            )}
          </Notice>
        )}
        {recoveries.length > 0 && !workflow?.submission.progress && (
          <RecoveryList recoveries={recoveries} recover={(item) => apply((state) => recoverSubmission(state, item))} />
        )}
        {draftCandidates.length > 1 && !workflow?.draft.remote && (
          <DraftRecoveryList
            drafts={draftCandidates}
            recover={(item) => apply((state) => adoptDraft(state, item))}
          />
        )}
        <div className="review-workflow-grid">
          <section className="review-workflow-discussions">
            <h2>Discussions</h2>
            {discussions.length === 0 ? (
              <Notice kind="empty">No discussions yet.</Notice>
            ) : (
              <div className="review-workflow-thread-list" onKeyDown={moveButtonFocus}>
                {discussions.map((discussion) => (
                  <DiscussionCard
                    key={discussion.id}
                    discussion={discussion}
                    canReply={mutationCapabilities?.reply === true && !quickBlocked}
                    canResolve={mutationCapabilities?.resolve === true && !quickBlocked}
                    reply={reply}
                    setReply={setReplyBuffer}
                    sendReply={sendReply}
                    resolve={resolveDiscussion}
                  />
                ))}
              </div>
            )}
          </section>
          <aside className="review-workflow-compose">
            <Composer
              label={workflow?.draft.remote ? "Add general draft comment" : "Quick comment"}
              body={generalBody}
              setBody={setGeneralBody}
              disabled={mutationCapabilities?.general_comment !== true || quickBlocked}
              disabledReason={
                quickBlocked
                  ? "Resolve or acknowledge the previous action before another mutation."
                  : capabilityReason(mutationCapabilities?.general_comment)
              }
              submit={() => void sendComment(generalBody, null)}
            />
            {!workflow?.draft.remote && (
              <QuickVerdicts
                capabilities={mutationCapabilities}
                blocked={quickBlocked}
                bodyAvailable={generalBody.length > 0}
                confirmation={confirmation}
                run={runQuickVerdict}
              />
            )}
            <Composer
              label={workflow?.draft.remote ? "Add selected line to draft" : "Quick inline comment"}
              body={inlineBody}
              setBody={setInlineBody}
              disabled={
                mutationCapabilities?.inline_comment !== true ||
                quickBlocked ||
                context.inlineAnchor?.review !== review ||
                (Boolean(workflow?.draft.remote) &&
                  (context.inlineAnchor?.contextComplete !== true ||
                    !workflow ||
                    !canCaptureDraftInline(workflow)))
              }
              disabledReason={
                quickBlocked
                  ? "Resolve or acknowledge the previous action before another mutation."
                  : inlineDisabledReason(
                      mutationCapabilities,
                      context.inlineAnchor,
                      review,
                      Boolean(workflow?.draft.remote),
                      workflow,
                    )
              }
              submit={() => void sendComment(inlineBody, context.inlineAnchor)}
            />
            <BufferedInlineNotes
              entries={buffersFor(review).inline}
              currentKey={anchorIdentity(context.inlineAnchor)}
            />
            {workflow?.draft.remote && (
              <DraftEditor
                workflow={workflow}
                edit={(content) => apply((state) => editDraft(state, content))}
                save={() => void saveDraft()}
                keepLocal={() => apply(keepLocalDraft)}
                migrate={() => void migrateDraft()}
                submit={submitDraft}
                confirmation={confirmation}
                setConfirmation={setConfirmation}
                capabilities={mutationCapabilities}
              />
            )}
            {workflow?.submission.progress && (
              <SubmissionProgress
                progress={workflow.submission.progress}
                message={workflow.submission.message}
                pending={workflow.submission.pending !== null}
                resume={() => void continueSubmission("resume")}
                reconcile={(resolution) => void continueSubmission("reconcile", resolution)}
                confirmation={confirmation}
                setConfirmation={setConfirmation}
              />
            )}
          </aside>
        </div>
      </section>
    </>
  );
}

function DiscussionCard({
  discussion,
  canReply,
  canResolve,
  reply,
  setReply,
  sendReply,
  resolve,
}: {
  readonly discussion: DiscussionDto;
  readonly canReply: boolean;
  readonly canResolve: boolean;
  readonly reply: { readonly id: string; readonly body: string } | null;
  readonly setReply: (value: { readonly id: string; readonly body: string } | null) => void;
  readonly sendReply: () => Promise<void>;
  readonly resolve: (discussion: DiscussionDto) => Promise<void>;
}): ReactNode {
  const composing = reply?.id === discussion.id;
  return (
    <article className="review-workflow-thread" tabIndex={-1}>
      <p className="review-workflow-thread-meta">
        {discussion.root_comment.author.display_name || discussion.root_comment.author.username}
        {discussion.is_inline ? ` · ${discussion.root_comment.file_path}` : ""}
      </p>
      <p>{discussion.root_comment.body}</p>
      {discussion.root_comment.replies.map((item) => (
        <blockquote key={item.id}>{item.body}</blockquote>
      ))}
      <div className="review-workflow-row">
        <button
          className="button button-secondary"
          disabled={!canReply}
          title={canReply ? undefined : "Replies are unsupported for this review."}
          onClick={() => setReply({ id: discussion.id, body: "" })}
        >
          Reply
        </button>
        <button
          className="button button-secondary"
          disabled={!canResolve || !discussion.resolvable}
          title={!canResolve || !discussion.resolvable ? "Resolution is unsupported for this discussion." : undefined}
          onClick={() => void resolve(discussion)}
        >
          {discussion.is_resolved ? "Reopen thread" : "Resolve"}
        </button>
      </div>
      {composing && (
        <div className="review-workflow-reply">
          <textarea
            autoFocus
            aria-label="Reply body"
            value={reply.body}
            onChange={(event) => setReply({ id: discussion.id, body: event.target.value })}
            onKeyDown={(event) => {
              if (event.key === "Escape" && reply.body.length === 0) setReply(null);
              if ((event.ctrlKey || event.metaKey) && event.key === "Enter") void sendReply();
            }}
          />
          <button className="button" disabled={!reply.body} onClick={() => void sendReply()}>
            Add reply
          </button>
        </div>
      )}
    </article>
  );
}

function Composer({
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
            if ((event.ctrlKey || event.metaKey) && event.key === "Enter") submit();
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

function BufferedInlineNotes({
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

function DraftEditor({
  workflow,
  edit,
  save,
  keepLocal,
  migrate,
  submit,
  confirmation,
  setConfirmation,
  capabilities,
}: {
  readonly workflow: ReviewWorkflowState;
  readonly edit: (content: DraftContentInputDto) => void;
  readonly save: () => void;
  readonly keepLocal: () => void;
  readonly migrate: () => void;
  readonly submit: () => Promise<void>;
  readonly confirmation: Confirmation | null;
  readonly setConfirmation: (value: Confirmation | null) => void;
  readonly capabilities: ReviewMutationCapabilitiesDto | null;
}): ReactNode {
  const content = workflow.draft.local;
  const stale = draftNeedsRevisionRecovery(workflow);
  const generalCount = content.comments.filter((item) => item.kind === "general").length;
  const inlineCount = content.comments.filter((item) => item.kind === "inline").length;
  const replyCount = content.comments.filter((item) => item.kind === "reply").length;
  const locked = Boolean(
    workflow.submission.pending ||
      (workflow.submission.progress && workflow.submission.progress.outcome !== "editable"),
  );
  return (
    <section className="review-workflow-draft">
      <h2>Draft review</h2>
      {stale && (
        <Notice kind="warning">
          <p>
            This draft remains preserved at revision {workflow.draft.remote?.revision.head_sha}.
            It cannot be submitted or receive a new inline anchor at the current revision.
          </p>
          <p>
            A separate current-revision draft will copy the body, verdict, and {generalCount} general
            comment(s). The {inlineCount} inline comment(s) and {replyCount} reply/replies remain on
            this old draft for deliberate recreation after checking their current targets.
          </p>
          <button
            className="button button-secondary"
            disabled={workflow.draft.dirty || Boolean(workflow.draft.pendingSave) || Boolean(workflow.draft.conflict)}
            onClick={migrate}
          >
            {confirmation === "migrate-draft"
              ? "Confirm create separate current-revision draft"
              : "Create current-revision draft"}
          </button>
        </Notice>
      )}
      {workflow.draft.conflict && (
        <Notice kind="warning">
          The durable draft changed elsewhere. Your unsaved text is preserved.
          <button className="button button-secondary" onClick={keepLocal}>Keep my text</button>
        </Notice>
      )}
      <label>
        Review body
        <textarea
          value={content.body}
          disabled={locked}
          onChange={(event) => edit({ ...content, body: event.target.value })}
        />
      </label>
      <label>
        Verdict
        <select
          value={content.verdict ?? ""}
          disabled={locked}
          onChange={(event) =>
            edit({ ...content, verdict: event.target.value === "" ? null : event.target.value as DraftContentInputDto["verdict"] })
          }
        >
          <option value="">No verdict</option>
          <option value="comment" disabled={capabilities?.comment_verdict !== true}>Comment</option>
          <option value="approve" disabled={capabilities?.approve !== true}>Approve</option>
          <option value="request_changes" disabled={capabilities?.request_changes !== true}>Request changes</option>
        </select>
      </label>
      <div className="review-workflow-draft-comments">
        <strong>{content.comments.length} draft comment(s)</strong>
        {content.comments.map((comment) => (
          <article key={comment.id} className="review-workflow-draft-comment">
            <p><strong>{draftCommentLabel(comment)}</strong></p>
            <textarea
              aria-label={`Edit draft comment ${comment.id}`}
              value={comment.body}
              disabled={locked}
              onChange={(event) =>
                edit({
                  ...content,
                  comments: content.comments.map((item) =>
                    item.id === comment.id
                      ? { ...item, body: event.target.value }
                      : item,
                  ),
                })
              }
            />
            <button
              className="button button-secondary"
              disabled={locked}
              onClick={() =>
                edit({
                  ...content,
                  comments: content.comments.filter((item) => item.id !== comment.id),
                })
              }
            >
              Remove draft comment
            </button>
          </article>
        ))}
      </div>
      {workflow.draft.preservedStaleDrafts.map((draft) => (
        <section key={draft.id} className="review-workflow-preserved-draft">
          <strong>Preserved old draft {draft.id}</strong>
          <p>Revision {draft.revision.head_sha}. Its content was not deleted or retargeted.</p>
          {draft.comments
            .filter((comment) => comment.kind !== "general")
            .map((comment) => (
              <article key={comment.id}>
                <strong>{draftCommentLabel(comment)}</strong>
                <p>{comment.body}</p>
              </article>
            ))}
        </section>
      ))}
      <div className="review-workflow-row">
        <button
          className="button button-secondary"
          disabled={!workflow.draft.dirty || Boolean(workflow.draft.pendingSave) || Boolean(workflow.draft.conflict)}
          onClick={save}
        >
          {workflow.draft.pendingSave ? "Saving…" : "Save draft"}
        </button>
        <button
          className="button"
          disabled={!canStartSubmission(workflow)}
          onClick={() => confirmation === "submit" ? void submit() : setConfirmation("submit")}
        >
          {confirmation === "submit" ? "Confirm submit review" : "Submit review"}
        </button>
      </div>
    </section>
  );
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
  readonly confirmation: Confirmation | null;
  readonly setConfirmation: (value: Confirmation | null) => void;
}): ReactNode {
  return (
    <section className="review-workflow-progress" aria-live="polite">
      <h2>Submission progress</h2>
      {message && <p>{message}</p>}
      <p>{progress.completed_step_ids.length} confirmed · {progress.unknown_step_ids.length} unknown</p>
      {progress.failure && <Notice kind="error">{progress.failure.message}</Notice>}
      {progress.outcome === "paused" && (
        <>
          <p>Confirmed steps stay excluded. Resume retries the next definitely unconfirmed step.</p>
          <button className="button" disabled={pending} onClick={resume}>Resume confirmed attempt</button>
        </>
      )}
      {progress.outcome === "unknown" && (
        <>
          <Notice kind="warning">
            Retry remaining can repeat an unconfirmed remote write. Confirmed receipt steps stay excluded.
            Mark submitted records your assertion after you inspect the forge; it is not a verified receipt.
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
            <button className="button button-secondary" disabled={pending} onClick={() => reconcile("return_editable")}>Return draft to editing</button>
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
        <button key={item.attempt_id} className="button button-secondary" onClick={() => recover(item)}>
          Recover {item.outcome} attempt with {item.completed_step_ids.length} confirmed step(s)
        </button>
      ))}
    </section>
  );
}

function ActionButtons({
  capabilities,
  confirmation,
  blocked,
  mergeOptions,
  setMergeOptions,
  run,
}: {
  readonly capabilities: ReviewActionCapabilitiesDto | null;
  readonly confirmation: Confirmation | null;
  readonly blocked: boolean;
  readonly mergeOptions: { readonly squash: boolean; readonly cleanup: boolean };
  readonly setMergeOptions: (value: { readonly squash: boolean; readonly cleanup: boolean }) => void;
  readonly run: (action: ReviewAction) => Promise<void>;
}): ReactNode {
  return (
    <div className="review-workflow-actions">
      <div className="review-workflow-row">
        {(["merge", "close", "reopen", "unapprove"] as const).map((action) => {
          const supported = capabilities?.[action] === true;
          return (
            <button
              key={action}
              className="button button-secondary"
              disabled={!supported || blocked}
              title={
                blocked
                  ? "Resolve or acknowledge the previous action before another mutation."
                  : supported
                    ? undefined
                    : `${label(action)} is unsupported for this review.`
              }
              onClick={() => void run(action)}
            >
              {confirmation === action ? `Confirm ${label(action)}` : label(action)}
            </button>
          );
        })}
      </div>
      {capabilities?.merge && (
        <div className="review-workflow-row">
          <label>
            <input
              type="checkbox"
              checked={mergeOptions.squash}
              onChange={(event) =>
                setMergeOptions({ ...mergeOptions, squash: event.target.checked })
              }
            />
            Squash commits
          </label>
          <label>
            <input
              type="checkbox"
              checked={mergeOptions.cleanup}
              onChange={(event) =>
                setMergeOptions({ ...mergeOptions, cleanup: event.target.checked })
              }
            />
            Delete source branch after merge
          </label>
        </div>
      )}
    </div>
  );
}

function QuickVerdicts({
  capabilities,
  blocked,
  bodyAvailable,
  confirmation,
  run,
}: {
  readonly capabilities: ReviewMutationCapabilitiesDto | null;
  readonly blocked: boolean;
  readonly bodyAvailable: boolean;
  readonly confirmation: Confirmation | null;
  readonly run: (verdict: ReviewVerdict) => Promise<void>;
}): ReactNode {
  const options = [
    ["comment", "Submit comment verdict", capabilities?.comment_verdict],
    ["approve", "Approve review", capabilities?.approve],
    ["request_changes", "Request changes", capabilities?.request_changes],
  ] as const;
  return (
    <section className="review-workflow-verdicts">
      <strong>Quick verdict</strong>
      <div className="review-workflow-row">
        {options.map(([verdict, text, supported]) => (
          <button
            key={verdict}
            className="button button-secondary"
            disabled={
              supported !== true ||
              blocked ||
              (verdict !== "approve" && !bodyAvailable)
            }
            title={
              supported !== true
                ? `${text} is unsupported for this review.`
                : verdict !== "approve" && !bodyAvailable
                  ? "Enter a review body in the general composer first."
                  : undefined
            }
            onClick={() => void run(verdict)}
          >
            {confirmation === `verdict:${verdict}`
              ? `Confirm ${text.toLowerCase()}`
              : text}
          </button>
        ))}
      </div>
    </section>
  );
}

function Notice({ kind, children }: { readonly kind: string; readonly children: ReactNode }): ReactNode {
  return <div className={`notice notice-${kind}`} role={kind === "error" ? "alert" : "status"}>{children}</div>;
}

function mutationAnchor(selection: InlineAnchorSelection): {
  readonly old_path: string;
  readonly new_path: string;
  readonly line: number;
  readonly side: "LEFT" | "RIGHT";
} {
  const line = selection.side === "old" ? selection.oldLine : selection.newLine;
  if (line === null) throw new Error("The selected diff side has no source line");
  return Object.freeze({
    old_path: selection.oldPath,
    new_path: selection.newPath,
    line,
    side: selection.side === "old" ? "LEFT" : "RIGHT",
  });
}

function draftSelection(selection: InlineAnchorSelection): DraftAnchorSelection {
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

async function recoverDraft(
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

async function recoverLatestSubmission(
  bridge: ReviewDesktopBridge,
  review: string,
  draftId: string,
  frozenVersion: number,
): Promise<SubmissionProgressDto | null> {
  try {
    const result = await bridge.listReviewSubmissions({ review, max_items: 100 }).result;
    return (
      result.attempts.find(
        (item) =>
          item.draft_id === draftId && item.frozen_version === frozenVersion,
      ) ?? null
    );
  } catch {
    return null;
  }
}

async function recoverSubmissionStatus(
  bridge: ReviewDesktopBridge,
  review: string,
  attemptId: string,
): Promise<SubmissionProgressDto | null> {
  try {
    return await bridge.getReviewSubmission({ review, attempt_id: attemptId }).result;
  } catch {
    return null;
  }
}

function inlineDisabledReason(
  capabilities: ReviewMutationCapabilitiesDto | null,
  anchor: InlineAnchorSelection | null,
  review: string,
  durable: boolean,
  workflow: ReviewWorkflowState | null,
): string | null {
  if (capabilities?.inline_comment !== true)
    return "Inline comments are unsupported for this review.";
  if (!anchor || anchor.review !== review)
    return "Select a source line in the current review diff.";
  if (durable && !anchor.contextComplete)
    return "The selected context is partial. Refresh the complete diff before drafting.";
  if (durable && workflow && !canCaptureDraftInline(workflow))
    return "The draft is bound to an earlier revision or a durable submission attempt.";
  return null;
}

function draftCommentLabel(comment: DraftCommentInputDto): string {
  if (comment.kind === "general") return `General comment ${comment.id}`;
  if (comment.kind === "reply")
    return `Reply ${comment.id} to discussion ${comment.thread_id}`;
  const line =
    comment.anchor.side === "old"
      ? comment.anchor.old_line
      : comment.anchor.new_line;
  const stale = comment.anchor.stale === true ? " · stale anchor" : "";
  return `Inline ${comment.id} · ${comment.anchor.new_path} · ${comment.anchor.side} line ${line}${stale}`;
}

function buffersFor(review: string): ComposerBuffers {
  let buffers = composerCache.get(review);
  if (!buffers) {
    buffers = { general: "", inline: new Map(), replies: new Map() };
    composerCache.set(review, buffers);
  }
  return buffers;
}

function anchorIdentity(anchor: InlineAnchorSelection | null): string | null {
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
  });
}

function inlineBufferLabel(key: string): string {
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

function capabilityReason(supported: boolean | undefined): string | null {
  return supported === true ? null : "General comments are unsupported for this review.";
}

function isUncertainError(value: unknown): boolean {
  if (value === null || typeof value !== "object") return false;
  const code = "code" in value && typeof value.code === "string" ? value.code : "";
  const serviceCode =
    "service_code" in value && typeof value.service_code === "string"
      ? value.service_code
      : "";
  return code === "mutation_timeout" || code === "connection_lost" || serviceCode === "request_cancelled";
}

function isConflictError(value: unknown): boolean {
  if (value === null || typeof value !== "object") return false;
  return (
    ("code" in value && value.code === "conflict") ||
    ("service_code" in value && value.service_code === "conflict")
  );
}

function isActionCommand(value: unknown): value is {
  readonly action: ReviewAction;
  readonly revision: ReviewRevisionDto;
} {
  return (
    value !== null &&
    typeof value === "object" &&
    "action" in value &&
    (value.action === "merge" ||
      value.action === "close" ||
      value.action === "reopen" ||
      value.action === "unapprove") &&
    "revision" in value &&
    value.revision !== null &&
    typeof value.revision === "object"
  );
}

function newOperationId(kind: string): string {
  return `desktop:${kind}:${crypto.randomUUID()}`;
}

function label(action: ReviewAction): string {
  return action === "merge"
    ? "Merge"
    : action === "close"
      ? "Close"
      : action === "reopen"
        ? "Reopen"
        : "Remove approval";
}

function moveButtonFocus(event: KeyboardEvent<HTMLElement>): void {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
  if (buttons.length === 0) return;
  const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
  const direction = event.key === "ArrowDown" ? 1 : -1;
  const next = buttons[(current < 0 ? 0 : current + direction + buttons.length) % buttons.length];
  if (next) {
    event.preventDefault();
    next.focus();
  }
}

export function sameReviewRevision(
  left: ReviewRevisionDto,
  right: ReviewRevisionDto,
): boolean {
  return left.head_sha === right.head_sha && left.base_sha === right.base_sha && left.start_sha === right.start_sha;
}
