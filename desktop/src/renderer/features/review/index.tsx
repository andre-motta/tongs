import {
  useCallback,
  useEffect,
  useMemo,
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
  DraftSnapshotDto,
  GeneralCommentParams,
  InlineCommentParams,
  ReviewAction,
  ReviewActionCapabilitiesDto,
  ReviewDesktopBridge,
  ReviewMutationCapabilitiesDto,
  ReviewVerdict,
} from "../../../shared/review.js";
import type {
  AppRoute,
  DiscussionDiffTarget,
  FeatureContribution,
  FeatureContext,
  InlineAnchorSelection,
} from "../../core/navigation.js";
import { formatDate, safeError } from "../../core/presentation.js";
import { SafeMarkdown } from "../../core/safe-markdown.js";
import { ReviewHeader } from "../review-detail/index.js";
import {
  BufferedInlineNotes,
  Composer,
  MULTILINE_REFUSAL,
  anchorIdentity,
  anchorRange,
  buffersFor,
  cacheWorkflow,
  cachedWorkflow,
  draftSelection,
  inlineBufferLabel,
  isUncertainError,
  mutationAnchor,
  newOperationId,
  readActiveDrafts,
  readMutationCapabilities,
  releaseActiveDrafts,
  releaseMutationCapabilities,
  reviewMutationError,
  subscribeWorkflow,
  type ComposerBuffers,
} from "./composer.js";
import type { PendingDraftEntry } from "./pending-card.js";
import {
  ReviewDrawerMount,
  pendingEntryTarget,
  requestPendingEdit,
  useReviewDrawer,
} from "./drawer.js";
import {
  acknowledgeQuickUncertainty,
  adoptDraft,
  beginQuickIntent,
  canCaptureDraftInline,
  captureDraftAnchor,
  createReviewWorkflowState,
  editDraft,
  markQuickIntentUncertain,
  observeReviewRevision,
  recoverQuickIntent,
  rejectQuickIntent,
  settleQuickIntent,
  type ReviewWorkflowState,
} from "./state.js";
import {
  formatSuggestionBody,
  prepareSuggestionTarget,
  suggestionDisabledReason,
  type SuggestionForge,
} from "./suggestion.js";
import {
  DiscussionMarkdownBody,
  allocateDiscussionMarkdown,
  discussionDiffTarget,
  type DiscussionMarkdownAllocation,
} from "./thread.js";

// The discussion allocator and the diff target moved to the thread module,
// which the diff surface now shares. They stay published from here because
// this panel is where the rest of the app already reaches for them.
export { allocateDiscussionMarkdown, discussionDiffTarget };

/**
 * What this panel is waiting to have confirmed. Submission, discard and the
 * stale revision migration left with the draft editor, so their confirmations
 * are the drawer's now and no longer named here.
 */
type Confirmation = ReviewAction | `verdict:${ReviewVerdict}`;

interface ReviewFeatureBridge extends DesktopBridge, ReviewDesktopBridge {}

export function createReviewFeature(
  bridge: DesktopBridge,
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
          bridge={bridge}
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
  const forge =
    context.repositories.find(
      (repository) => repository.handle === route.item.repository,
    )?.forge_type ?? null;
  const [snapshot, setSnapshot] = useState<ReviewSnapshotDto | null>(null);
  const [discussions, setDiscussions] = useState<readonly DiscussionDto[]>([]);
  const [mutationCapabilities, setMutationCapabilities] =
    useState<ReviewMutationCapabilitiesDto | null>(null);
  const [actionCapabilities, setActionCapabilities] =
    useState<ReviewActionCapabilitiesDto | null>(null);
  const [draftCandidates, setDraftCandidates] =
    useState<readonly DraftSnapshotDto[]>([]);
  const [workflow, setWorkflow] = useState<ReviewWorkflowState | null>(
    cachedWorkflow(review),
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
  const initialSuggestion = suggestionBuffer(
    initialBuffers,
    initialAnchorKey,
    context.inlineAnchor,
  );
  const [suggestionComment, setSuggestionCommentState] = useState(
    initialSuggestion.comment,
  );
  const [suggestionReplacement, setSuggestionReplacementState] = useState(
    initialSuggestion.replacement,
  );
  const [suggestionOpen, setSuggestionOpen] = useState(
    context.inlineAnchor !== null,
  );
  const [reply, setReply] = useState<{ readonly id: string; readonly body: string } | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [mergeOptions, setMergeOptions] = useState({
    squash: false,
    cleanup: false,
  });
  const openExternal = useCallback(
    (url: string) => bridge.openExternal(url),
    [bridge],
  );
  const markdownAllocations = useMemo(
    () => allocateDiscussionMarkdown(discussions),
    [discussions],
  );
  const quickBlocked =
    workflow?.quick?.status === "sending" || workflow?.quick?.status === "unknown";
  // The selection this panel writes is the diff's, so a range reaches this
  // composer too and has to meet the same capability the in-diff one checks.
  const multilineBlocked =
    context.inlineAnchor !== null &&
    anchorRange(context.inlineAnchor) !== null &&
    mutationCapabilities?.multiline_comment !== true;
  const setGeneralBody = (body: string): void => {
    buffersFor(review).general = body;
    setGeneralBodyState(body);
  };
  const setInlineBody = (body: string): void => {
    const key = anchorIdentity(anchorRef.current);
    if (key) buffersFor(review).inline.set(key, body);
    setInlineBodyState(body);
  };
  const setSuggestionBuffer = (
    comment: string,
    replacement: string,
  ): void => {
    const key = anchorIdentity(anchorRef.current);
    if (key)
      buffersFor(review).suggestions.set(key, { comment, replacement });
    setSuggestionCommentState(comment);
    setSuggestionReplacementState(replacement);
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
    const suggestion = suggestionBuffer(
      buffersFor(review),
      key,
      context.inlineAnchor,
    );
    setSuggestionCommentState(suggestion.comment);
    setSuggestionReplacementState(suggestion.replacement);
    setSuggestionOpen(context.inlineAnchor !== null);
  }, [context.inlineAnchor]);
  useEffect(() => {
    const buffers = buffersFor(review);
    setGeneralBodyState(buffers.general);
    const key = anchorIdentity(context.inlineAnchor);
    setInlineBodyState(key ? buffers.inline.get(key) ?? "" : "");
    const suggestion = suggestionBuffer(buffers, key, context.inlineAnchor);
    setSuggestionCommentState(suggestion.comment);
    setSuggestionReplacementState(suggestion.replacement);
    setSuggestionOpen(context.inlineAnchor !== null);
    setReply(null);
  }, [context.inlineAnchor, review]);
  const apply = useCallback(
    (change: (current: ReviewWorkflowState) => ReviewWorkflowState): ReviewWorkflowState => {
      const current = workflowRef.current;
      if (!current) throw new Error("Review workflow is not ready");
      const next = change(current);
      workflowRef.current = next;
      cacheWorkflow(review, next);
      setWorkflow(next);
      return next;
    },
    [review],
  );

  // The drawer writes the same workflow state this panel reads, and both are
  // mounted here at once, so the panel adopts what the drawer publishes rather
  // than rendering from a copy that stopped being true.
  useEffect(
    () =>
      subscribeWorkflow(review, (next) => {
        if (next === workflowRef.current) return;
        workflowRef.current = next;
        setWorkflow(next);
      }),
    [review],
  );
  useEffect(() => {
    let current = true;
    // The active drafts and the mutation capabilities go through the shared
    // readers, because the drawer this panel now mounts asks for both as well.
    // Reading them directly here would put the same two questions to the
    // sidecar twice on every visit to this tab.
    const drafts = readActiveDrafts(bridge, review);
    const capabilities = readMutationCapabilities(bridge, review);
    const reads = [
      bridge.getReview(review),
      bridge.listDiscussions(review),
      bridge.getReviewActionCapabilities(review),
    ] as const;
    void Promise.all([
      reads[0].result,
      reads[1].result,
      reads[2].result,
      capabilities.result,
      drafts.result,
    ] as const)
      .then(([detail, discussionResult, actionResult, mutationResult, draftResult]) => {
        if (!current) return;
        if (!detail.revision)
          throw new Error("The current review revision is unavailable.");
        setSnapshot(detail);
        setDiscussions(discussionResult.discussions);
        setMutationCapabilities(mutationResult.capabilities);
        setActionCapabilities(actionResult.capabilities);
        setDraftCandidates(draftResult.drafts);
        const cached = cachedWorkflow(review);
        const next = cached
          ? observeReviewRevision(cached, detail.revision)
          : createReviewWorkflowState(review, detail.revision);
        workflowRef.current = next;
        cacheWorkflow(review, next);
        setWorkflow(next);
      })
      .catch((reason: unknown) => current && setError(safeError(reason)));
    return () => {
      current = false;
      for (const item of reads) void bridge.cancelRead(item.requestToken);
      releaseActiveDrafts(bridge, review, drafts);
      releaseMutationCapabilities(bridge, review, capabilities);
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
      setError(reviewMutationError(reason));
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
        apply((current) => rejectQuickIntent(current, operationId, reviewMutationError(reason)));
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
      setError(reviewMutationError(reason));
    }
  };

  const sendSuggestion = async (): Promise<void> => {
    const selection = anchorRef.current;
    if (!workflow || !selection || !forge) return;
    const reason = suggestionActionDisabledReason(
      selection,
      forge,
      mutationCapabilities,
      Boolean(workflow.draft.remote),
      workflow,
      quickBlocked,
    );
    if (reason) {
      setError(reason);
      return;
    }
    try {
      const target = prepareSuggestionTarget(selection, forge);
      const body = formatSuggestionBody(
        suggestionReplacement,
        target.originalLineCount,
        forge,
        suggestionComment,
      );
      if (workflow.draft.remote) {
        const draftAnchor = await captureDraftAnchor(
          target.draftSelection,
          () => {
            const current = anchorRef.current;
            if (!current) return null;
            try {
              return prepareSuggestionTarget(current, forge).draftSelection;
            } catch {
              return null;
            }
          },
        );
        apply((current) =>
          editDraft(current, {
            ...current.draft.local,
            comments: [
              ...current.draft.local.comments,
              {
                id: crypto.randomUUID(),
                kind: "inline",
                body,
                anchor: draftAnchor,
              },
            ],
          }),
        );
        clearSuggestionBuffer(review, selection);
        setSuggestionCommentState("");
        setSuggestionReplacementState(target.originalCode);
        setSuggestionOpen(false);
        return;
      }
      const operationId = newOperationId("suggestion");
      const command: InlineCommentParams = {
        operation_id: operationId,
        review,
        revision: selection.revision,
        anchor: target.mutationAnchor,
        body,
      };
      apply((current) => beginQuickIntent(current, operationId, command));
      setError(null);
      try {
        const outcome = await bridge.postInlineReviewComment(command);
        apply((current) => settleQuickIntent(current, operationId, outcome));
        if (outcome.outcome === "known") {
          clearSuggestionBuffer(review, selection);
          setSuggestionCommentState("");
          setSuggestionReplacementState(target.originalCode);
          setSuggestionOpen(false);
        }
      } catch (reason) {
        apply((current) =>
          isUncertainError(reason)
            ? markQuickIntentUncertain(current, operationId)
            : rejectQuickIntent(
                current,
                operationId,
                reviewMutationError(reason),
              ),
        );
      }
    } catch (reason) {
      setError(reviewMutationError(reason));
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
          : rejectQuickIntent(current, operationId, reviewMutationError(reason)),
      );
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
          : rejectQuickIntent(current, operationId, reviewMutationError(reason)),
      );
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
          : rejectQuickIntent(current, operationId, reviewMutationError(reason)),
      );
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
          : rejectQuickIntent(current, operationId, reviewMutationError(reason)),
      );
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
      <ReviewHeader
        route={route}
        navigate={context.navigate}
        panels={context.reviewPanels}
        drawer={
          snapshot?.revision ? (
            <PanelReviewDrawer
              bridge={bridge}
              review={review}
              forge={forge}
              revision={snapshot.revision}
              openDiffAt={(target) =>
                context.navigate({ ...route, panel: "diff", diffTarget: target })
              }
            />
          ) : null
        }
      />
      <section className="review-workflow-shell" aria-label="Review workflow">
        <div className="review-workflow-toolbar">
          {!workflow?.draft.remote ? (
            <button className="button" disabled={!snapshot?.revision} onClick={() => void startReview()}>
              {draftCandidates.length === 1 ? "Resume review" : "Start review"}
            </button>
          ) : (
            <>
              <strong>Draft review active</strong>
              <span className="review-workflow-thread-meta">
                Stored version {workflow.draft.remote.version}
              </span>
            </>
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
          <Notice kind={workflow.quick.status === "rejected" ? "error" : "warning"}>
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
        <div className="review-workflow-grid">
          <section className="review-workflow-discussions">
            <h2>Discussions</h2>
            {discussions.length === 0 ? (
              <Notice kind="empty">No discussions yet.</Notice>
            ) : (
              <div className="review-workflow-thread-list" onKeyDown={moveButtonFocus}>
                {discussions.map((discussion, index) => (
                  <DiscussionCard
                    key={discussion.id}
                    discussion={discussion}
                    canReply={mutationCapabilities?.reply === true && !quickBlocked}
                    canResolve={mutationCapabilities?.resolve === true && !quickBlocked}
                    reply={reply}
                    setReply={setReplyBuffer}
                    sendReply={sendReply}
                    resolve={resolveDiscussion}
                    markdownAllocation={markdownAllocations[index]}
                    openExternal={openExternal}
                    showInDiff={(target) =>
                      context.navigate({
                        ...route,
                        panel: "diff",
                        diffTarget: target,
                      })
                    }
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
                multilineBlocked ||
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
            <SuggestionComposer
              selection={context.inlineAnchor}
              forge={forge}
              durable={Boolean(workflow?.draft.remote)}
              capabilities={mutationCapabilities}
              workflow={workflow}
              quickBlocked={quickBlocked}
              open={suggestionOpen}
              comment={suggestionComment}
              replacement={suggestionReplacement}
              setOpen={setSuggestionOpen}
              setComment={(value) =>
                setSuggestionBuffer(value, suggestionReplacement)
              }
              setReplacement={(value) =>
                setSuggestionBuffer(suggestionComment, value)
              }
              submit={() => void sendSuggestion()}
            />
            <BufferedInlineNotes
              entries={buffersFor(review).inline}
              currentKey={anchorIdentity(context.inlineAnchor)}
            />
            {workflow?.draft.remote && (
              <p className="review-workflow-thread-meta" role="status">
                The summary, the verdict, submission and recovery live in Your
                review, at the top of this page.
              </p>
            )}
          </aside>
        </div>
      </section>
    </>
  );
}

/**
 * The review drawer as the Discussions panel mounts it. Jump and Edit send the
 * reader to the Changes tab over the existing discussion jump route, so the
 * coordinates a pending entry resolves to are the ones that route already
 * resolves and nothing new decides where a line is.
 */
function PanelReviewDrawer({
  bridge,
  review,
  forge,
  revision,
  openDiffAt,
}: {
  readonly bridge: ReviewFeatureBridge;
  readonly review: string;
  readonly forge: SuggestionForge | null;
  readonly revision: ReviewRevisionDto;
  readonly openDiffAt: (target: DiscussionDiffTarget) => void;
}): ReactNode {
  const controller = useReviewDrawer(bridge, review, revision, forge);
  const target = (entry: PendingDraftEntry): DiscussionDiffTarget | null => {
    const anchored = pendingEntryTarget(entry);
    return anchored ? { discussionId: `pending:${entry.id}`, ...anchored } : null;
  };
  return (
    <ReviewDrawerMount
      controller={controller}
      openExternal={(url) => bridge.openExternal(url)}
      jump={(entry) => {
        const to = target(entry);
        if (to) openDiffAt(to);
      }}
      edit={(entry) => {
        requestPendingEdit(review, entry.id);
        const to = target(entry);
        if (to) openDiffAt(to);
      }}
    />
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
  showInDiff,
  markdownAllocation,
  openExternal,
}: {
  readonly discussion: DiscussionDto;
  readonly canReply: boolean;
  readonly canResolve: boolean;
  readonly reply: { readonly id: string; readonly body: string } | null;
  readonly setReply: (value: { readonly id: string; readonly body: string } | null) => void;
  readonly sendReply: () => Promise<void>;
  readonly resolve: (discussion: DiscussionDto) => Promise<void>;
  readonly showInDiff: (target: DiscussionDiffTarget) => void;
  readonly markdownAllocation: DiscussionMarkdownAllocation | undefined;
  readonly openExternal: (url: string) => Promise<boolean>;
}): ReactNode {
  const composing = reply?.id === discussion.id;
  const diffTarget = discussionDiffTarget(discussion);
  return (
    <article className="review-workflow-thread" tabIndex={-1}>
      <p className="review-workflow-thread-meta">
        {discussion.root_comment.author.display_name || discussion.root_comment.author.username}
        {discussion.is_inline && discussion.root_comment.file_path !== null
          ? ` · ${discussion.root_comment.file_path}`
          : ""}
      </p>
      <DiscussionMarkdownBody
        allocated={markdownAllocation?.root === true}
        body={discussion.root_comment.body}
        openExternal={openExternal}
      />
      {discussion.root_comment.replies.map((item, index) => (
        <blockquote key={item.id}>
          <DiscussionMarkdownBody
            allocated={markdownAllocation?.replies[index] === true}
            body={item.body}
            openExternal={openExternal}
          />
        </blockquote>
      ))}
      <div className="review-workflow-row">
        {discussion.is_inline && (
          <button
            className="button button-secondary"
            disabled={!diffTarget}
            title={
              diffTarget
                ? "Resolve this discussion against the currently displayed diff."
                : "This discussion has no usable diff location."
            }
            onClick={() => diffTarget && showInDiff(diffTarget)}
          >
            Show in diff
          </button>
        )}
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

function SuggestionComposer({
  selection,
  forge,
  durable,
  capabilities,
  workflow,
  quickBlocked,
  open,
  comment,
  replacement,
  setOpen,
  setComment,
  setReplacement,
  submit,
}: {
  readonly selection: InlineAnchorSelection | null;
  readonly forge: SuggestionForge | null;
  readonly durable: boolean;
  readonly capabilities: ReviewMutationCapabilitiesDto | null;
  readonly workflow: ReviewWorkflowState | null;
  readonly quickBlocked: boolean;
  readonly open: boolean;
  readonly comment: string;
  readonly replacement: string;
  readonly setOpen: (value: boolean) => void;
  readonly setComment: (value: string) => void;
  readonly setReplacement: (value: string) => void;
  readonly submit: () => void;
}): ReactNode {
  const reason = suggestionActionDisabledReason(
    selection,
    forge,
    capabilities,
    durable,
    workflow,
    quickBlocked,
  );
  const count = selection?.selectedLines?.length ?? 0;
  const lineStart = selection?.selectedLines?.[0]?.newLine ?? null;
  const lineEnd = selection?.selectedLines?.at(-1)?.newLine ?? null;
  return (
    <section className="review-workflow-composer suggestion-composer">
      <div className="review-workflow-row suggestion-heading">
        <strong>Suggested replacement</strong>
        <button
          className="button button-secondary"
          disabled={reason !== null}
          title={reason ?? undefined}
          onClick={() => setOpen(!open)}
        >
          {open ? "Hide editor" : "Suggest replacement"}
        </button>
      </div>
      {reason && <small>{reason}</small>}
      {!reason && selection && (
        <small>
          {selection.newPath}, new line {lineStart}
          {lineEnd !== lineStart ? ` through ${lineEnd}` : ""} ({count} source
          {count === 1 ? " line" : " lines"})
        </small>
      )}
      {open && !reason && selection && forge && (
        <>
          <label>
            Optional explanation
            <textarea
              aria-label="Suggestion explanation"
              value={comment}
              onChange={(event) => setComment(event.target.value)}
            />
          </label>
          <label>
            Replacement code
            <textarea
              className="suggestion-code"
              aria-label="Suggestion replacement code"
              value={replacement}
              spellCheck={false}
              onChange={(event) => setReplacement(event.target.value)}
              onKeyDown={(event) => {
                if ((event.ctrlKey || event.metaKey) && event.key === "Enter")
                  submit();
              }}
            />
          </label>
          <div className="review-workflow-row">
            <button className="button" onClick={submit}>
              {durable ? "Add suggestion to draft" : "Post quick suggestion"}
            </button>
            <button
              className="button button-secondary"
              onClick={() => setOpen(false)}
            >
              Cancel and keep text
            </button>
          </div>
        </>
      )}
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
  if (anchorRange(anchor) !== null && capabilities.multiline_comment !== true)
    return MULTILINE_REFUSAL;
  if (durable && !anchor.contextComplete)
    return "The selected context is partial. Refresh the complete diff before drafting.";
  if (durable && workflow && !canCaptureDraftInline(workflow))
    return "The draft is bound to an earlier revision or a durable submission attempt.";
  return null;
}

function suggestionBuffer(
  buffers: ComposerBuffers,
  key: string | null,
  selection: InlineAnchorSelection | null,
): { readonly comment: string; readonly replacement: string } {
  if (!key || !selection) return { comment: "", replacement: "" };
  return (
    buffers.suggestions.get(key) ?? {
      comment: "",
      replacement:
        selection.selectedLines?.map((line) => line.content).join("\n") ?? "",
    }
  );
}

function clearSuggestionBuffer(
  review: string,
  selection: InlineAnchorSelection,
): void {
  const key = anchorIdentity(selection);
  if (key) buffersFor(review).suggestions.delete(key);
}

function suggestionActionDisabledReason(
  selection: InlineAnchorSelection | null,
  forge: SuggestionForge | null,
  capabilities: ReviewMutationCapabilitiesDto | null,
  durable: boolean,
  workflow: ReviewWorkflowState | null,
  quickBlocked: boolean,
): string | null {
  if (quickBlocked)
    return "Resolve or acknowledge the previous action before another mutation.";
  if (capabilities?.inline_comment !== true)
    return "Inline comments are unsupported for this review.";
  const reason = suggestionDisabledReason(selection, forge);
  if (reason) return reason;
  if (!selection || !workflow) return "The review workflow is still loading.";
  if (selection.review !== workflow.displayed.review)
    return "Select source code in the current review diff.";
  if (!sameRevision(selection.revision, workflow.displayed.latestObservedRevision))
    return "The selected code belongs to an earlier revision. Refresh the diff.";
  if (
    (selection.selectedLines?.length ?? 0) > 1 &&
    capabilities.multiline_comment !== true
  )
    return "This forge does not support multi-line suggestions for the selected review.";
  if (durable && !canCaptureDraftInline(workflow))
    return "The draft is bound to an earlier revision or a durable submission attempt.";
  return null;
}

function sameRevision(
  left: ReviewRevisionDto,
  right: ReviewRevisionDto,
): boolean {
  return (
    left.head_sha === right.head_sha &&
    left.base_sha === right.base_sha &&
    left.start_sha === right.start_sha
  );
}

function capabilityReason(supported: boolean | undefined): string | null {
  return supported === true ? null : "General comments are unsupported for this review.";
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
