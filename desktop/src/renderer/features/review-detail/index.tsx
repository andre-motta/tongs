import { useCallback, type ReactNode } from "react";
import type {
  DesktopBridge,
  ReviewSnapshotDto,
} from "../../../shared/bridge.js";
import { parseInertMarkdown } from "../../core/markdown.js";
import type {
  AppRoute,
  FeatureContribution,
  ReviewPanel,
} from "../../core/navigation.js";
import { formatDate, safeError } from "../../core/presentation.js";
import type { QueryCoordinator } from "../../core/query.js";
import { useRetainedRead } from "../../core/use-read.js";

export function ReviewHeader({
  route,
  navigate,
}: {
  readonly route: Extract<AppRoute, { kind: "review" }>;
  readonly navigate: (route: AppRoute) => void;
}): ReactNode {
  return (
    <>
      <header className="review-header">
        <button
          className="button button-quiet"
          onClick={() => navigate({ kind: "inbox", repository: null })}
        >
          ← Reviews
        </button>
        <div className="review-heading">
          <p className="eyebrow">Review #{route.item.summary.number}</p>
          <h1 className="view-title">{route.item.summary.title}</h1>
        </div>
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
        {(["overview", "diff", "commits"] as const).map((panel) => (
          <button
            key={panel}
            className={`tab ${route.panel === panel ? "tab-active" : ""}`}
            data-panel={panel}
            aria-current={route.panel === panel ? "page" : undefined}
            onClick={() => navigate({ ...route, panel: panel as ReviewPanel })}
          >
            {panel === "diff"
              ? "Files changed"
              : panel[0]?.toUpperCase() + panel.slice(1)}
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
    matches: (route) => route.kind === "review" && route.panel === "overview",
    render: (context, route) =>
      route.kind === "review" && route.panel === "overview" ? (
        <ReviewOverview
          bridge={context.bridge}
          queries={context.queries}
          route={route}
          navigate={context.navigate}
        />
      ) : null,
  };
}

function ReviewOverview({
  bridge,
  queries,
  route,
  navigate,
}: ReviewProps): ReactNode {
  const begin = useCallback(
    () => bridge.getReview(route.item.handle),
    [bridge, route.item.handle],
  );
  const state = useRetainedRead(queries, `review:${route.item.handle}`, begin, [
    route.item.handle,
  ]);
  return (
    <>
      <ReviewHeader route={route} navigate={navigate} />
      {state.loading && !state.value && (
        <Notice kind="loading">Loading review details…</Notice>
      )}
      {Boolean(state.error) && (
        <Notice kind="error">
          {state.value
            ? "Refresh failed. Showing the previous review details."
            : safeError(state.error)}
        </Notice>
      )}
      {state.value && <Overview snapshot={state.value} />}
    </>
  );
}

export function createCommitsFeature(): FeatureContribution {
  return {
    id: "review.commits",
    order: 21,
    matches: (route) => route.kind === "review" && route.panel === "commits",
    render: (context, route) =>
      route.kind === "review" && route.panel === "commits" ? (
        <Commits
          bridge={context.bridge}
          queries={context.queries}
          route={route}
          navigate={context.navigate}
        />
      ) : null,
  };
}

function Commits({ bridge, queries, route, navigate }: ReviewProps): ReactNode {
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
      <ReviewHeader route={route} navigate={navigate} />
      {state.loading && !state.value && (
        <Notice kind="loading">Loading commits…</Notice>
      )}
      {Boolean(state.error) && (
        <Notice kind="error">
          {state.value
            ? "Refresh failed. Showing previous commits."
            : safeError(state.error)}
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
}

function Overview({
  snapshot,
}: {
  readonly snapshot: ReviewSnapshotDto;
}): ReactNode {
  const blocks = parseInertMarkdown(snapshot.detail.description);
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
        {blocks.length === 0 ? (
          <Notice kind="empty">No description was provided.</Notice>
        ) : (
          blocks.map((block, index) =>
            block.kind === "heading" ? (
              <h3 key={index}>{block.text}</h3>
            ) : block.kind === "code" ? (
              <pre key={index}>{block.text}</pre>
            ) : block.kind === "list" ? (
              <li key={index}>{block.text}</li>
            ) : (
              <p key={index}>{block.text}</p>
            ),
          )
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
