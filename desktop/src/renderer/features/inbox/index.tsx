import {
  useCallback,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type {
  DesktopBridge,
  ReviewListItemDto,
  ReviewListResult,
} from "../../../shared/bridge.js";
import type { AppRoute, FeatureContribution } from "../../core/navigation.js";
import { formatDate, safeError } from "../../core/presentation.js";
import type { CoordinatedRead } from "../../core/query.js";
import { useRetainedRead } from "../../core/use-read.js";

export function createInboxFeature(): FeatureContribution {
  return {
    id: "reviews.inbox",
    order: 10,
    matches: (route) => route.kind === "inbox",
    render: (context, route) =>
      route.kind === "inbox" ? (
        <InboxView
          bridge={context.bridge}
          queries={context.queries}
          route={route}
          repositories={context.repositories}
          repositoriesReady={context.repositoriesReady}
          repositoryGeneration={context.repositoryGeneration}
          navigate={context.navigate}
        />
      ) : null,
  };
}

function InboxView({
  bridge,
  queries,
  route,
  repositories,
  repositoriesReady,
  repositoryGeneration,
  navigate,
}: {
  readonly bridge: DesktopBridge;
  readonly queries: FeatureParameters["queries"];
  readonly route: Extract<AppRoute, { kind: "inbox" }>;
  readonly repositories: FeatureParameters["repositories"];
  readonly repositoriesReady: boolean;
  readonly repositoryGeneration: number;
  readonly navigate: (route: AppRoute) => void;
}): ReactNode {
  const repository = route.repository;
  const [reviewState, setReviewState] = useState<"open" | "closed">("open");
  const begin = useCallback(
    () =>
      repository
        ? bridge.listReviews({
            scope: "all_open",
            state: reviewState,
            repository: repository.handle,
          })
        : listDiscoveredReviews(bridge, repositories, reviewState),
    [bridge, repositories, repository, reviewState],
  );
  const state = useRetainedRead(
    queries,
    `inbox:${repository?.handle ?? "all"}:${reviewState}`,
    begin,
    [repository?.handle, repositoryGeneration, reviewState],
    repositoriesReady,
  );
  const title = repository?.display_name ?? "All reviews";
  return (
    <>
      <header className="view-header">
        <div>
          <p className="eyebrow">{repository?.forge_type ?? "Workspace"}</p>
          <h1 className="view-title">{title}</h1>
        </div>
        <div className="header-actions">
          <button
            className={`button ${reviewState === "open" ? "button-active" : "button-secondary"}`}
            data-review-state="open"
            onClick={() => setReviewState("open")}
          >
            Open
          </button>
          <button
            className={`button ${reviewState === "closed" ? "button-active" : "button-secondary"}`}
            data-review-state="closed"
            onClick={() => setReviewState("closed")}
          >
            Closed & merged
          </button>
          <button
            className="button button-secondary"
            disabled={state.loading}
            onClick={state.refresh}
          >
            {state.loading && state.value ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </header>
      {state.loading && !state.value && (
        <Notice kind="loading">
          {repositoriesReady
            ? "Loading reviews from the local service…"
            : "Waiting for local repository discovery…"}
        </Notice>
      )}
      {Boolean(state.error) && (
        <Notice kind="error">
          {state.value
            ? "Refresh failed. Showing the previous review list."
            : safeError(state.error)}
        </Notice>
      )}
      {state.value && (
        <ReviewList
          result={state.value}
          navigate={navigate}
          emptyLabel={
            reviewState === "open"
              ? "No open reviews match this repository scope."
              : "No closed or merged reviews match this repository scope."
          }
        />
      )}
    </>
  );
}

export function listDiscoveredReviews(
  bridge: DesktopBridge,
  repositories: readonly { readonly handle: string }[],
  state: "open" | "closed",
): CoordinatedRead<ReviewListResult> {
  const reads = repositories.map((repository) =>
    bridge.listReviews({
      scope: "all_open",
      state,
      repository: repository.handle,
    }),
  );
  return {
    requestTokens: reads.map((read) => read.requestToken),
    result: Promise.all(reads.map((read) => read.result)).then((results) => ({
      items: results
        .flatMap((result) => result.items)
        .sort((left, right) =>
          right.summary.updated_at.localeCompare(left.summary.updated_at),
        ),
      failures: results.flatMap((result) => result.failures),
    })),
  };
}

type FeatureParameters = Parameters<FeatureContribution["render"]>[0];

function ReviewList({
  result,
  navigate,
  emptyLabel,
}: {
  readonly result: ReviewListResult;
  readonly navigate: (route: AppRoute) => void;
  readonly emptyLabel: string;
}): ReactNode {
  const presentation = inboxPresentation(result);
  const moveFocus = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const buttons = [
      ...event.currentTarget.querySelectorAll<HTMLButtonElement>("button"),
    ];
    const current = buttons.indexOf(
      document.activeElement as HTMLButtonElement,
    );
    const direction = event.key === "ArrowDown" ? 1 : -1;
    const target =
      buttons[
        (current < 0 ? 0 : current + direction + buttons.length) %
          buttons.length
      ];
    if (target) {
      event.preventDefault();
      target.focus();
    }
  };
  return (
    <>
      {presentation.partialFailures > 0 && (
        <Notice kind="error">
          {presentation.partialFailures} repository read
          {presentation.partialFailures === 1 ? "" : "s"} failed. Available
          reviews are shown below.
        </Notice>
      )}
      {presentation.empty ? (
        <Notice kind="empty">{emptyLabel}</Notice>
      ) : (
        <div className="review-list" role="list" onKeyDown={moveFocus}>
          {result.items.map((item) => (
            <ReviewCard key={item.handle} item={item} navigate={navigate} />
          ))}
        </div>
      )}
    </>
  );
}

function ReviewCard({
  item,
  navigate,
}: {
  readonly item: ReviewListItemDto;
  readonly navigate: (route: AppRoute) => void;
}): ReactNode {
  return (
    <button
      className="review-card"
      role="listitem"
      data-review-number={item.summary.number}
      onClick={() => navigate({ kind: "review", item, panel: "overview" })}
    >
      <span className={`state state-${item.summary.ci_status}`}>
        {item.summary.ci_status}
      </span>
      <span className="review-number">#{item.summary.number}</span>
      <strong className="review-title">{item.summary.title}</strong>
      <span className="review-meta">
        {item.summary.author.display_name || item.summary.author.username} ·{" "}
        {item.summary.source_branch} → {item.summary.target_branch} ·{" "}
        {formatDate(item.summary.updated_at)}
      </span>
    </button>
  );
}

export function inboxPresentation(result: ReviewListResult): {
  readonly empty: boolean;
  readonly partialFailures: number;
} {
  return Object.freeze({
    empty: result.items.length === 0,
    partialFailures: result.failures.length,
  });
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
