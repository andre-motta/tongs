import { useCallback, useMemo, useState, type ReactNode } from "react";
import type {
  DesktopBridge,
  DiscussionDto,
  DiscussionsResult,
  ReviewRevisionDto,
  ReviewSnapshotDto,
} from "../../../shared/bridge.js";
import type {
  ReviewMutationCapabilitiesDto,
  ReviewVerdict,
} from "../../../shared/review.js";
import {
  inboxReturnRoute,
  type AppRoute,
  type FeatureContribution,
  type ReviewPanelContribution,
} from "../../core/navigation.js";
import type { RepositoryDto } from "../../../shared/bridge.js";
import { formatDate, safeError } from "../../core/presentation.js";
import type { QueryCoordinator } from "../../core/query.js";
import { SafeMarkdown } from "../../core/safe-markdown.js";
import { useRetainedRead, type ReadState } from "../../core/use-read.js";
import {
  InlineComposer,
  clearInlineBuffer,
  isUncertainError,
  newOperationId,
  reviewMutationError,
  useInlineReviewComposer,
} from "../review/composer.js";
import {
  acknowledgeQuickUncertainty,
  beginQuickIntent,
  markQuickIntentUncertain,
  rejectQuickIntent,
  settleQuickIntent,
} from "../review/state.js";
import type { SuggestionForge } from "../review/suggestion.js";
import {
  DiscussionMarkdownBody,
  allocateDiscussionMarkdown,
} from "../review/thread.js";

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
          repositories={context.repositories}
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
  repositories,
  route,
  navigate,
  panels,
}: ReviewProps): ReactNode {
  const review = route.item.handle;
  const forge =
    repositories.find(
      (repository) => repository.handle === route.item.repository,
    )?.forge_type ?? null;
  const begin = useCallback(() => bridge.getReview(review), [bridge, review]);
  const state = useRetainedRead(queries, `review:${review}`, begin, [review]);
  // This route's own discussions read. It is not shared with the Discussions
  // jump list: `QueryCoordinator` tracks reads while they are in flight and
  // keeps no answer once one settles, and the jump list calls the bridge
  // directly rather than through the coordinator. Design 2.3 puts the
  // review-level notes on this tab, so the read belongs on this tab, and it
  // is one read per visit here and one per visit there.
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
          bridge={bridge}
          review={review}
          revision={revision}
          forge={forge}
        />
      )}
      <ReviewLevelDiscussions state={notes} openExternal={openExternal} />
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
          repositories={context.repositories}
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
  readonly repositories: readonly RepositoryDto[];
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
 * The general-comment surface on Overview. The composer itself is the shared
 * in-diff composer in its general mode, so the two writes, every refusal, the
 * pending count and the retained text are one implementation rather than a
 * copy of one: an entry added here is refused by exactly the states that
 * refuse an inline one, a submission in flight included.
 *
 * What this component adds around it is what belongs to the review rather
 * than to the comment: the standing of an immediate write whose result is
 * unknown, and the quick verdicts, which submit the text the reader is
 * looking at and so read the composer's own body.
 */
function GeneralComposer({
  bridge,
  review,
  revision,
  forge,
}: {
  readonly bridge: DesktopBridge;
  readonly review: string;
  readonly revision: ReviewRevisionDto;
  readonly forge: SuggestionForge | null;
}): ReactNode {
  const controller = useInlineReviewComposer(bridge, review, revision, forge);
  const { workflow, apply } = controller.shared;
  const [body, setBody] = useState("");
  const [confirmation, setConfirmation] = useState<ReviewVerdict | null>(null);
  const quick = workflow.quick;

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
    apply((current) => beginQuickIntent(current, operationId, command));
    try {
      const outcome = await bridge.submitReviewVerdict(command);
      apply((current) => settleQuickIntent(current, operationId, outcome));
      if (verdict !== "approve" && outcome.outcome === "known")
        clearInlineBuffer(review, null);
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
  };

  return (
    <section className="panel general-composer-panel">
      <InlineComposer anchor={null} controller={controller} onBody={setBody} />
      {quick?.message && (
        <div
          className={`notice notice-${quick.status === "rejected" ? "error" : "warning"}`}
          role={quick.status === "rejected" ? "alert" : "status"}
        >
          {/* The composer already reports the sentence it was given, so it is
              not said twice; what is only here is the way out of an unknown
              result, which otherwise blocks every further write. */}
          {quick.message !== controller.message && <span>{quick.message}</span>}
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
      {!controller.pendingReview ? (
        <QuickVerdicts
          capabilities={controller.capabilities}
          blocked={
            quick?.status === "sending" ||
            quick?.status === "unknown" ||
            controller.busy
          }
          bodyAvailable={body.length > 0}
          confirmation={confirmation}
          run={runQuickVerdict}
        />
      ) : (
        <p className="review-workflow-thread-meta" role="status">
          The summary, the verdict, submission and recovery live in Your review,
          on the Discussions tab.
        </p>
      )}
    </section>
  );
}

/**
 * Said while the capability read behind the tiles is in flight. A capability
 * that has not been answered for is not an unsupported one, and this panel is
 * the first thing a reader sees on a slow sidecar.
 */
const VERDICTS_LOADING = "Verdict support for this review is still loading.";

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
              capabilities === null
                ? VERDICTS_LOADING
                : supported !== true
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
 * Said when the discussions read fails on this tab. It is the diff surface's
 * `THREAD_READ_REFUSAL` worded for the notes list, because it is the same
 * failure of the same operation: what the reader is owed is the statement that
 * nothing is known, never the statement that there is nothing.
 */
const NOTES_READ_REFUSAL =
  "The published discussions could not be read, so this review shows no review-level notes. Retry, or open the review on the forge.";

/** Said when a refresh fails on top of notes that were read successfully. */
const NOTES_REFRESH_REFUSAL =
  "Rereading the discussions failed. Showing the review-level notes from the previous read.";

/**
 * The review-level notes: the discussions that carry no diff anchor and so
 * have nowhere to be jumped to. They are read here rather than in the
 * Discussions jump list, which lists only threads that resolve to a diff
 * position. The Markdown budget is allocated over the whole discussion list in
 * its source order and looked up by id, so a note is allocated the same way
 * here as it is on every other surface that shows it.
 *
 * The empty sentence is said only once a read has established the absence. A
 * read that failed and a read still in flight each get their own words, and
 * the failure gets a retry of its own, because the page's Refresh button
 * refreshes the detail read and not this one.
 */
function ReviewLevelDiscussions({
  state,
  openExternal,
}: {
  readonly state: ReadState<DiscussionsResult>;
  readonly openExternal: (url: string) => Promise<boolean>;
}): ReactNode {
  const discussions = state.value?.discussions ?? EMPTY_DISCUSSIONS;
  const allocations = useMemo(() => {
    const allocated = allocateDiscussionMarkdown(discussions);
    return new Map(
      discussions.map((discussion, index) => [discussion.id, allocated[index]]),
    );
  }, [discussions]);
  const notes = useMemo(
    () => discussions.filter((discussion) => !discussion.is_inline),
    [discussions],
  );
  return (
    <section className="panel review-level-discussions">
      <h2 className="section-title">Review discussions</h2>
      {state.loading && !state.value && (
        <Notice kind="loading">Loading review discussions…</Notice>
      )}
      {Boolean(state.error) && (
        <Notice kind="error">
          <span>
            {state.value ? NOTES_REFRESH_REFUSAL : NOTES_READ_REFUSAL}
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
      {!state.loading && !state.error && notes.length === 0 && (
        <Notice kind="empty">No review-level discussions yet.</Notice>
      )}
      {notes.map((discussion) => {
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
      })}
    </section>
  );
}

const EMPTY_DISCUSSIONS: readonly DiscussionDto[] = Object.freeze([]);
