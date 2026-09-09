import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import type {
  DesktopBridge,
  DiscussionDto,
  ReviewRevisionDto,
  ReviewSnapshotDto,
} from "../../../shared/bridge.js";
import type {
  DraftCommentInputDto,
  DraftContentInputDto,
  DraftListResult,
  DraftSnapshotDto,
  GeneralCommentParams,
  ReviewDesktopBridge,
  ReviewMutationCapabilitiesDto,
  ReviewVerdict,
} from "../../../shared/review.js";
import {
  inboxReturnRoute,
  type AppRoute,
  type FeatureContribution,
  type ReviewPanelContribution,
} from "../../core/navigation.js";
import { formatDate, safeError } from "../../core/presentation.js";
import type { QueryCoordinator } from "../../core/query.js";
import { SafeMarkdown } from "../../core/safe-markdown.js";
import { useRetainedRead } from "../../core/use-read.js";
import {
  ComposerRefusal,
  buffersFor,
  composerFailureMessage,
  isConflictError,
  isUncertainError,
  newOperationId,
  readActiveDrafts,
  readMutationCapabilities,
  recoverDraft,
  releaseActiveDrafts,
  releaseMutationCapabilities,
  reviewMutationError,
  useSharedReviewWorkflow,
} from "../review/composer.js";
import {
  acknowledgeQuickUncertainty,
  adoptDraft,
  beginDraftSave,
  beginQuickIntent,
  conflictDraftSave,
  editDraft,
  failDraftSave,
  finishDraftSave,
  markQuickIntentUncertain,
  rejectQuickIntent,
  settleQuickIntent,
  type ReviewWorkflowState,
} from "../review/state.js";
import {
  DiscussionMarkdownBody,
  allocateDiscussionMarkdown,
} from "../review/thread.js";

/** The reads and writes the Overview general composer puts to the sidecar. */
interface OverviewBridge extends DesktopBridge, ReviewDesktopBridge {}

export function ReviewHeader({
  route,
  navigate,
  panels,
  drawer = null,
}: {
  readonly route: Extract<AppRoute, { kind: "review" }>;
  readonly navigate: (route: AppRoute) => void;
  readonly panels: readonly ReviewPanelContribution[];
  /**
   * The "Your review" button and its drawer, from whichever review surface
   * holds the workflow controller. The header carries it because GitLab puts
   * the review's own state in the top right of the review page, not inside one
   * tab, but the header itself owns no review state and mounts no reads: a
   * panel with no controller passes nothing and shows no button.
   */
  readonly drawer?: ReactNode;
}): ReactNode {
  return (
    <>
      <header className="review-header">
        <button
          className="button button-quiet"
          onClick={() => navigate(inboxReturnRoute())}
        >
          ← Reviews
        </button>
        <div className="review-heading">
          <p className="eyebrow">Review #{route.item.summary.number}</p>
          <h1 className="view-title">{route.item.summary.title}</h1>
        </div>
        {drawer}
        <button
          className="button button-secondary"
          onClick={() =>
            void window.tongs.openExternal(route.item.summary.web_url)
          }
        >
          Open on forge
        </button>
      </header>
      <nav className="tabs" aria-label="Review sections">
        {panels.map((panel) => (
          <button
            key={panel.id}
            className={`tab ${route.panel === panel.id ? "tab-active" : ""}`}
            data-panel={panel.id}
            aria-current={route.panel === panel.id ? "page" : undefined}
            onClick={() => navigate({ ...route, panel: panel.id })}
          >
            {panel.label}
          </button>
        ))}
      </nav>
    </>
  );
}

export function createReviewOverviewFeature(): FeatureContribution {
  return {
    id: "review.overview",
    order: 20,
    reviewPanel: { id: "overview", label: "Overview", order: 10 },
    matches: (route) => route.kind === "review" && route.panel === "overview",
    render: (context, route) =>
      route.kind === "review" && route.panel === "overview" ? (
        <ReviewOverview
          bridge={context.bridge}
          queries={context.queries}
          route={route}
          navigate={context.navigate}
          panels={context.reviewPanels}
        />
      ) : null,
  };
}

function ReviewOverview({
  bridge,
  queries,
  route,
  navigate,
  panels,
}: ReviewProps): ReactNode {
  const review = route.item.handle;
  const begin = useCallback(() => bridge.getReview(review), [bridge, review]);
  const state = useRetainedRead(queries, `review:${review}`, begin, [review]);
  // The review-level notes this panel lists come from the same read the
  // Discussions jump list uses; the coordinator holds one answer per key, so
  // listing them here does not put a second question to the sidecar.
  const beginDiscussions = useCallback(
    () => bridge.listDiscussions(review),
    [bridge, review],
  );
  const notes = useRetainedRead(
    queries,
    `discussions:${review}`,
    beginDiscussions,
    [review],
  );
  const openExternal = useCallback(
    (url: string) => bridge.openExternal(url),
    [bridge],
  );
  const revision = state.value?.revision ?? null;
  return (
    <>
      <ReviewHeader route={route} navigate={navigate} panels={panels} />
      <ReadRefreshButton state={state} label="review details" />
      {state.loading && !state.value && (
        <Notice kind="loading">Loading review details…</Notice>
      )}
      {Boolean(state.error) && (
        <Notice kind="error">
          <span>
            {state.value
              ? "Refresh failed. Showing the previous review details."
              : safeError(state.error)}
          </span>
          <button
            className="button button-secondary notice-action"
            disabled={state.loading}
            onClick={state.refresh}
          >
            {state.value ? "Refresh again" : "Retry"}
          </button>
        </Notice>
      )}
      {state.value && (
        <Overview openExternal={openExternal} snapshot={state.value} />
      )}
      {revision && (
        <GeneralComposer
          bridge={bridge as OverviewBridge}
          review={review}
          revision={revision}
          openExternal={openExternal}
        />
      )}
      <ReviewLevelDiscussions
        discussions={notes.value?.discussions ?? []}
        openExternal={openExternal}
      />
    </>
  );
}

export function createCommitsFeature(): FeatureContribution {
  return {
    id: "review.commits",
    order: 21,
    reviewPanel: { id: "commits", label: "Commits", order: 30 },
    matches: (route) => route.kind === "review" && route.panel === "commits",
    render: (context, route) =>
      route.kind === "review" && route.panel === "commits" ? (
        <Commits
          bridge={context.bridge}
          queries={context.queries}
          route={route}
          navigate={context.navigate}
          panels={context.reviewPanels}
        />
      ) : null,
  };
}

function Commits({
  bridge,
  queries,
  route,
  navigate,
  panels,
}: ReviewProps): ReactNode {
  const begin = useCallback(
    () => bridge.listCommits(route.item.handle),
    [bridge, route.item.handle],
  );
  const state = useRetainedRead(
    queries,
    `commits:${route.item.handle}`,
    begin,
    [route.item.handle],
  );
  return (
    <>
      <ReviewHeader route={route} navigate={navigate} panels={panels} />
      <ReadRefreshButton state={state} label="commits" />
      {state.loading && !state.value && (
        <Notice kind="loading">Loading commits…</Notice>
      )}
      {Boolean(state.error) && (
        <Notice kind="error">
          <span>
            {state.value
              ? "Refresh failed. Showing previous commits."
              : safeError(state.error)}
          </span>
          <button
            className="button button-secondary notice-action"
            disabled={state.loading}
            onClick={state.refresh}
          >
            {state.value ? "Refresh again" : "Retry"}
          </button>
        </Notice>
      )}
      {state.value &&
        (state.value.commits.length === 0 ? (
          <Notice kind="empty">This review has no commits.</Notice>
        ) : (
          <div className="commit-list">
            {state.value.commits.map((commit) => (
              <article key={commit.sha} className="commit-card">
                <code className="commit-sha">{commit.short_sha}</code>
                <strong className="commit-title">{commit.title}</strong>
                <span className="review-meta">
                  {commit.author.display_name || commit.author.username} ·{" "}
                  {formatDate(commit.created_at)}
                </span>
              </article>
            ))}
          </div>
        ))}
    </>
  );
}

interface ReviewProps {
  readonly bridge: DesktopBridge;
  readonly queries: QueryCoordinator;
  readonly route: Extract<AppRoute, { kind: "review" }>;
  readonly navigate: (route: AppRoute) => void;
  readonly panels: readonly ReviewPanelContribution[];
}

function ReadRefreshButton({
  state,
  label,
}: {
  readonly state: { readonly loading: boolean; readonly refresh: () => void };
  readonly label: string;
}): ReactNode {
  return (
    <div className="view-actions">
      <button
        className="button button-secondary"
        disabled={state.loading}
        onClick={state.refresh}
      >
        {state.loading ? `Refreshing ${label}…` : `Refresh ${label}`}
      </button>
    </div>
  );
}

function Overview({
  openExternal,
  snapshot,
}: {
  readonly openExternal: (url: string) => Promise<boolean>;
  readonly snapshot: ReviewSnapshotDto;
}): ReactNode {
  const facts = [
    ["State", snapshot.detail.state],
    ["Merge", snapshot.detail.merge_status],
    ["CI", snapshot.detail.ci_status],
    ["Updated", formatDate(snapshot.detail.updated_at)],
    [
      "Changes",
      `${snapshot.detail.additions ?? "?"} additions, ${snapshot.detail.deletions ?? "?"} deletions`,
    ],
  ];
  return (
    <div className="overview-grid">
      <section className="panel">
        <h2 className="section-title">Description</h2>
        {snapshot.detail.description.length === 0 ? (
          <Notice kind="empty">No description was provided.</Notice>
        ) : (
          <SafeMarkdown
            openExternal={openExternal}
            source={snapshot.detail.description}
          />
        )}
      </section>
      <aside className="panel facts">
        <h2 className="section-title">Review status</h2>
        {facts.map(([label, value]) => (
          <p className="fact" key={label}>
            <span className="fact-label">{label}</span>
            <strong className="fact-value">{value}</strong>
          </p>
        ))}
        {!snapshot.revision && (
          <Notice kind="error">
            The current revision is unavailable. Revision-bound reads are
            disabled.
          </Notice>
        )}
      </aside>
    </div>
  );
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
 * The general-comment composer, in the shape the in-diff composer already
 * uses: two writes that never relabel in place, a Preview toggle, and text
 * kept in the review's own general buffer so it returns only to this review.
 *
 * The primary write adds an entry with no anchor, which is what makes it a
 * general entry rather than an inline one and keeps it out of the review's
 * Summary, the draft `body` the drawer owns. The write paths are spelled here
 * rather than taken from the in-diff controller because that controller binds
 * every write to an `InlineAnchorSelection`; generalising it is the follow-up
 * this card names rather than an edit made under a concurrent card.
 */
function GeneralComposer({
  bridge,
  review,
  revision,
  openExternal,
}: {
  readonly bridge: OverviewBridge;
  readonly review: string;
  readonly revision: ReviewRevisionDto;
  readonly openExternal: (url: string) => Promise<boolean>;
}): ReactNode {
  const shared = useSharedReviewWorkflow(bridge, review, revision);
  const { workflow, held, apply } = shared;
  const [capabilities, setCapabilities] =
    useState<ReviewMutationCapabilitiesDto | null>(null);
  const [body, setBodyState] = useState(() => buffersFor(review).general);
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState<ReviewVerdict | null>(null);

  useEffect(() => {
    let live = true;
    let read: ReturnType<typeof readMutationCapabilities> | null = null;
    try {
      read = readMutationCapabilities(bridge, review);
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
      if (read) releaseMutationCapabilities(bridge, review, read);
    };
  }, [bridge, review]);

  useEffect(() => setBodyState(buffersFor(review).general), [review]);

  const setBody = (value: string): void => {
    buffersFor(review).general = value;
    setBodyState(value);
  };

  const quick = workflow.quick;
  const pendingReview = workflow.draft.remote !== null;
  const pendingCount = workflow.draft.local.comments.length;
  const quickBlocked =
    workflow.quick?.status === "sending" || workflow.quick?.status === "unknown";

  const quickReason = ((): string | null => {
    if (capabilities === null)
      return "General comment support for this review is still loading.";
    if (!capabilities.general_comment)
      return "General comments are unsupported for this review.";
    if (quickBlocked)
      return "Resolve or acknowledge the previous action in the review workflow before another mutation.";
    if (busy) return "A review write is already in flight.";
    return null;
  })();

  const draftReason = ((): string | null => {
    if (quickReason !== null) return quickReason;
    if (workflow.draft.conflict)
      return "This pending review was changed elsewhere. Resolve the conflict in Your review before adding a general comment.";
    if (workflow.draft.remote && workflow.draft.remote.state !== "editable")
      return "The pending review is no longer editable. Settle it in Your review before adding a general comment.";
    if (
      workflow.submission.progress &&
      workflow.submission.progress.outcome !== "editable"
    )
      return "The pending review is bound to a durable submission attempt. Settle it in Your review before adding a general comment.";
    return null;
  })();

  const bindDraft = async (): Promise<ReviewWorkflowState> => {
    const current = held.current;
    if (current.draft.remote) return current;
    // Shares whatever active-draft read is already in flight, for the same
    // reason the in-diff composer does: a first press during mount adoption
    // asks the sidecar one question rather than two identical ones.
    const read = readActiveDrafts(bridge, review);
    let result: DraftListResult;
    try {
      result = await read.result;
    } finally {
      releaseActiveDrafts(bridge, review, read);
    }
    if (result.drafts.length > 1)
      throw new ComposerRefusal(
        "Several pending reviews were recovered. Resume one in Your review before commenting.",
      );
    const draft =
      result.drafts[0] ??
      (await bridge.createReviewDraft({ review, revision }));
    return apply((state) => adoptDraft(state, draft));
  };

  const saveDraft = async (): Promise<void> => {
    const current = apply(beginDraftSave);
    const remote = current.draft.remote;
    const pending = current.draft.pendingSave;
    if (!remote || !pending)
      throw new ComposerRefusal(
        "The pending review is not ready to save. Settle it in Your review before adding a general comment.",
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
      if (conflicting) apply((latest) => conflictDraftSave(latest, conflicting));
      else apply(failDraftSave);
      throw failure;
    }
  };

  /**
   * Undoes the local entry a failed save was carrying, so the obvious retry
   * cannot write the same general comment twice. A draft that was clean before
   * the entry is restored from its own remote snapshot rather than by content,
   * which leaves `dirty` unset on content nothing changed.
   */
  const rollBack = (
    remote: DraftSnapshotDto | null,
    wasDirty: boolean,
    content: DraftContentInputDto,
  ): boolean => {
    try {
      apply((current) => {
        const bound = current.draft.remote;
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
  };

  const commentNow = async (): Promise<void> => {
    if (body.length === 0) return;
    if (quickReason !== null) {
      setMessage(quickReason);
      return;
    }
    setBusy(true);
    setMessage(null);
    const operationId = newOperationId("comment");
    const command: GeneralCommentParams = {
      operation_id: operationId,
      review,
      body,
    };
    try {
      apply((current) => beginQuickIntent(current, operationId, command));
      try {
        const outcome = await bridge.postReviewComment(command);
        apply((current) => settleQuickIntent(current, operationId, outcome));
        if (outcome.outcome === "known") setBody("");
      } catch (failure) {
        // The intent's own standing is the notice below; reporting it here as
        // well would say the same thing twice about one write.
        apply((current) =>
          isUncertainError(failure)
            ? markQuickIntentUncertain(current, operationId)
            : rejectQuickIntent(
                current,
                operationId,
                reviewMutationError(failure),
              ),
        );
      }
    } catch (failure) {
      setMessage(composerFailureMessage(failure));
    } finally {
      setBusy(false);
    }
  };

  const addToReview = async (): Promise<void> => {
    if (body.length === 0) return;
    if (draftReason !== null) {
      setMessage(draftReason);
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const bound = await bindDraft();
      const comment: DraftCommentInputDto = Object.freeze({
        id: crypto.randomUUID(),
        kind: "general",
        body,
      });
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
            `${composerFailureMessage(failure)} The general comment stayed in the pending review; open Your review and remove it before retrying.`,
          );
        throw failure;
      }
      setBody("");
    } catch (failure) {
      setMessage(composerFailureMessage(failure));
    } finally {
      setBusy(false);
    }
  };

  const runQuickVerdict = async (verdict: ReviewVerdict): Promise<void> => {
    if (workflow.draft.remote) return;
    if (verdict !== "comment" && confirmation !== verdict) {
      setConfirmation(verdict);
      return;
    }
    const operationId = newOperationId(verdict);
    const command = {
      operation_id: operationId,
      review,
      revision: workflow.displayed.revision,
      verdict,
      body: verdict === "approve" ? "" : body,
    };
    setConfirmation(null);
    setBusy(true);
    try {
      apply((current) => beginQuickIntent(current, operationId, command));
      try {
        const outcome = await bridge.submitReviewVerdict(command);
        apply((current) => settleQuickIntent(current, operationId, outcome));
        if (verdict !== "approve" && outcome.outcome === "known") setBody("");
      } catch (failure) {
        apply((current) =>
          isUncertainError(failure)
            ? markQuickIntentUncertain(current, operationId)
            : rejectQuickIntent(
                current,
                operationId,
                reviewMutationError(failure),
              ),
        );
      }
    } catch (failure) {
      setMessage(composerFailureMessage(failure));
    } finally {
      setBusy(false);
    }
  };

  // A refusal already reported as the notice is not repeated underneath it.
  const reasons = [
    ...new Set(
      [draftReason, quickReason].filter(
        (reason): reason is string => reason !== null && reason !== message,
      ),
    ),
  ];
  return (
    <section
      className="panel review-workflow-composer general-composer"
      aria-label="General comment composer"
      onKeyDown={(event) => {
        if (!(event.ctrlKey || event.metaKey) || event.key !== "Enter") return;
        event.preventDefault();
        void addToReview();
      }}
    >
      <div className="inline-composer-heading">
        <strong>General comment</strong>
        {pendingReview && (
          <span className="inline-composer-chip">
            Review in progress, {pendingCount} pending
          </span>
        )}
      </div>
      {preview ? (
        <div className="inline-composer-preview" aria-label="Comment preview">
          {body.length === 0 ? (
            <small>Nothing to preview yet.</small>
          ) : (
            <SafeMarkdown source={body} openExternal={openExternal} />
          )}
        </div>
      ) : (
        <textarea
          className="inline-composer-text"
          aria-label="General review comment"
          value={body}
          onChange={(event) => setBody(event.target.value)}
        />
      )}
      {message !== null && (
        <div className="notice notice-error" role="alert">
          {message}
        </div>
      )}
      {quick?.message && (
        <div
          className={`notice notice-${quick.status === "rejected" ? "error" : "warning"}`}
          role={quick.status === "rejected" ? "alert" : "status"}
        >
          <span>{quick.message}</span>
          {quick.status === "unknown" && (
            <button
              className="button button-secondary notice-action"
              onClick={() =>
                apply((current) =>
                  acknowledgeQuickUncertainty(current, quick.operationId),
                )
              }
            >
              I inspected the forge; acknowledge uncertainty
            </button>
          )}
        </div>
      )}
      {reasons.map((reason) => (
        <small key={reason}>{reason}</small>
      ))}
      <div className="inline-composer-actions">
        <div className="inline-composer-toolbar">
          <button
            className="button button-secondary"
            aria-pressed={preview}
            onClick={() => setPreview(!preview)}
          >
            Preview
          </button>
        </div>
        <div className="inline-composer-writes">
          <button
            className="button button-secondary"
            disabled={body.length === 0 || quickReason !== null}
            title={quickReason ?? undefined}
            onClick={() => void commentNow()}
          >
            Add comment now
          </button>
          <button
            className="button"
            disabled={body.length === 0 || draftReason !== null}
            title={draftReason ?? undefined}
            onClick={() => void addToReview()}
          >
            {pendingReview ? "Add to review" : "Start a review"}
          </button>
        </div>
      </div>
      {!pendingReview && (
        <QuickVerdicts
          capabilities={capabilities}
          blocked={quickBlocked || busy}
          bodyAvailable={body.length > 0}
          confirmation={confirmation}
          run={runQuickVerdict}
        />
      )}
      {pendingReview && (
        <p className="review-workflow-thread-meta" role="status">
          The summary, the verdict, submission and recovery live in Your review,
          on the Discussions tab.
        </p>
      )}
    </section>
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
  readonly confirmation: ReviewVerdict | null;
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
            {confirmation === verdict ? `Confirm ${text.toLowerCase()}` : text}
          </button>
        ))}
      </div>
    </section>
  );
}

/**
 * The review-level notes: the discussions that carry no diff anchor and so
 * have nowhere to be jumped to. They are read here rather than in the
 * Discussions jump list, which lists only threads that resolve to a diff
 * position. The Markdown budget is allocated over the whole discussion list in
 * its source order and looked up by id, so a note is allocated the same way
 * here as it is on every other surface that shows it.
 */
function ReviewLevelDiscussions({
  discussions,
  openExternal,
}: {
  readonly discussions: readonly DiscussionDto[];
  readonly openExternal: (url: string) => Promise<boolean>;
}): ReactNode {
  const allocations = useMemo(() => {
    const allocated = allocateDiscussionMarkdown(discussions);
    return new Map(
      discussions.map((discussion, index) => [
        discussion.id,
        allocated[index],
      ]),
    );
  }, [discussions]);
  const notes = useMemo(
    () => discussions.filter((discussion) => !discussion.is_inline),
    [discussions],
  );
  return (
    <section className="panel review-level-discussions">
      <h2 className="section-title">Review discussions</h2>
      {notes.length === 0 ? (
        <Notice kind="empty">No review-level discussions yet.</Notice>
      ) : (
        notes.map((discussion) => {
          const allocation = allocations.get(discussion.id);
          return (
            <article key={discussion.id} className="review-workflow-thread">
              <p className="review-workflow-thread-meta">
                {discussion.root_comment.author.display_name ||
                  discussion.root_comment.author.username}
              </p>
              <DiscussionMarkdownBody
                allocated={allocation?.root === true}
                body={discussion.root_comment.body}
                openExternal={openExternal}
              />
              {discussion.root_comment.replies.map((item, index) => (
                <blockquote key={item.id}>
                  <DiscussionMarkdownBody
                    allocated={allocation?.replies[index] === true}
                    body={item.body}
                    openExternal={openExternal}
                  />
                </blockquote>
              ))}
            </article>
          );
        })
      )}
    </section>
  );
}
