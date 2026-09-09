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
  DraftSnapshotDto,
  ReviewAction,
  ReviewActionCapabilitiesDto,
  ReviewDesktopBridge,
} from "../../../shared/review.js";
import type {
  AppRoute,
  DiscussionDiffTarget,
  FeatureContribution,
  FeatureContext,
} from "../../core/navigation.js";
import { safeError } from "../../core/presentation.js";
import { ReviewHeader } from "../review-detail/index.js";
import {
  cacheWorkflow,
  cachedWorkflow,
  isUncertainError,
  newOperationId,
  readActiveDrafts,
  releaseActiveDrafts,
  reviewMutationError,
  subscribeWorkflow,
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
  createReviewWorkflowState,
  markQuickIntentUncertain,
  observeReviewRevision,
  recoverQuickIntent,
  rejectQuickIntent,
  settleQuickIntent,
  type ReviewWorkflowState,
} from "./state.js";
import type { SuggestionForge } from "./suggestion.js";
import {
  DiscussionMarkdownBody,
  allocateDiscussionMarkdown,
  discussionDiffTarget,
  threadAnchorLabel,
  threadSummaryText,
  type DiscussionMarkdownAllocation,
} from "./thread.js";

// The discussion allocator and the diff target moved to the thread module,
// which the diff surface now shares. They stay published from here because
// this panel is where the rest of the app already reaches for them.
export { allocateDiscussionMarkdown, discussionDiffTarget };

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
  const [actionCapabilities, setActionCapabilities] =
    useState<ReviewActionCapabilitiesDto | null>(null);
  const [draftCandidates, setDraftCandidates] =
    useState<readonly DraftSnapshotDto[]>([]);
  const [workflow, setWorkflow] = useState<ReviewWorkflowState | null>(
    cachedWorkflow(review),
  );
  const workflowRef = useRef(workflow);
  const [error, setError] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<ReviewAction | null>(null);
  const [mergeOptions, setMergeOptions] = useState({
    squash: false,
    cleanup: false,
  });
  const openExternal = useCallback(
    (url: string) => bridge.openExternal(url),
    [bridge],
  );
  const quickBlocked =
    workflow?.quick?.status === "sending" || workflow?.quick?.status === "unknown";
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
    const reads = [
      bridge.getReview(review),
      bridge.listDiscussions(review),
      bridge.getReviewActionCapabilities(review),
    ] as const;
    void Promise.all([reads[0].result, reads[1].result, reads[2].result] as const)
      .then(([detail, discussionResult, actionResult]) => {
        if (!current) return;
        if (!detail.revision)
          throw new Error("The current review revision is unavailable.");
        setSnapshot(detail);
        setDiscussions(discussionResult.discussions);
        setActionCapabilities(actionResult.capabilities);
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
    };
  }, [bridge, review]);

  // The active-draft read waits for the revision deliberately. The drawer
  // mounts on the same condition, so both surfaces ask in one commit and the
  // shared reader answers them from one question; asking during the batch
  // above settled the read before the drawer existed and put the same
  // question to the sidecar twice on every visit to this tab.
  const revisionReady = Boolean(snapshot?.revision);
  useEffect(() => {
    if (!revisionReady) return;
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
      // The toolbar still offers Start review, which reads again on the press.
    }
    return () => {
      live = false;
      if (shared) releaseActiveDrafts(bridge, review, shared);
    };
  }, [bridge, review, revisionReady]);

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
        <ThreadJumpList
          discussions={discussions}
          openExternal={openExternal}
          showInDiff={(target) =>
            context.navigate({ ...route, panel: "diff", diffTarget: target })
          }
        />
      </section>
    </>
  );
}

/**
 * The Discussions panel: every published thread that resolves to a diff
 * position, unresolved ones first, each with the jump that opens it where it
 * was written. Writing happens where the thread is, in the diff, or on
 * Overview for a review-level comment, so this panel offers no composer and no
 * reply or resolve control of its own.
 *
 * The jump goes through `discussionDiffTarget` untouched, so the coordinates a
 * row resolves to are exactly the ones the diff route already resolves and
 * nothing here decides where a line is.
 */
function ThreadJumpList({
  discussions,
  openExternal,
  showInDiff,
}: {
  readonly discussions: readonly DiscussionDto[];
  readonly openExternal: (url: string) => Promise<boolean>;
  readonly showInDiff: (target: DiscussionDiffTarget) => void;
}): ReactNode {
  // The Markdown budget is allocated over the whole list in its source order
  // and read back by id, so a thread is allocated the same way here as on the
  // diff surface even though this panel lists a filtered, reordered subset.
  const allocations = useMemo(() => {
    const allocated = allocateDiscussionMarkdown(discussions);
    return new Map(
      discussions.map((discussion, index) => [discussion.id, allocated[index]]),
    );
  }, [discussions]);
  // Unresolved first. `sort` is stable, so threads that share a standing keep
  // the order the forge listed them in.
  const threads = useMemo(
    () =>
      discussions
        .filter((discussion) => discussion.is_inline)
        .slice()
        .sort(
          (left, right) =>
            Number(left.is_resolved) - Number(right.is_resolved),
        ),
    [discussions],
  );
  return (
    <section className="review-workflow-discussions">
      <h2>Discussions</h2>
      {threads.length === 0 ? (
        <Notice kind="empty">No discussions yet.</Notice>
      ) : (
        <div className="review-workflow-thread-list" onKeyDown={moveButtonFocus}>
          {threads.map((discussion) => (
            <ThreadJumpRow
              key={discussion.id}
              discussion={discussion}
              markdownAllocation={allocations.get(discussion.id)}
              openExternal={openExternal}
              showInDiff={showInDiff}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function ThreadJumpRow({
  discussion,
  markdownAllocation,
  openExternal,
  showInDiff,
}: {
  readonly discussion: DiscussionDto;
  readonly markdownAllocation: DiscussionMarkdownAllocation | undefined;
  readonly openExternal: (url: string) => Promise<boolean>;
  readonly showInDiff: (target: DiscussionDiffTarget) => void;
}): ReactNode {
  const diffTarget = discussionDiffTarget(discussion);
  return (
    <article className="review-workflow-thread" tabIndex={-1}>
      <p className="review-workflow-thread-meta">
        {discussion.root_comment.author.display_name ||
          discussion.root_comment.author.username}
        {discussion.root_comment.file_path !== null
          ? ` \u00b7 ${discussion.root_comment.file_path}`
          : ""}
        {diffTarget ? ` \u00b7 ${threadAnchorLabel(diffTarget)}` : ""}
      </p>
      <p className="review-workflow-thread-summary">
        {threadSummaryText(discussion)}
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
      </div>
    </article>
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

function ActionButtons({
  capabilities,
  confirmation,
  blocked,
  mergeOptions,
  setMergeOptions,
  run,
}: {
  readonly capabilities: ReviewActionCapabilitiesDto | null;
  readonly confirmation: ReviewAction | null;
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

function Notice({ kind, children }: { readonly kind: string; readonly children: ReactNode }): ReactNode {
  return <div className={`notice notice-${kind}`} role={kind === "error" ? "alert" : "status"}>{children}</div>;
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
