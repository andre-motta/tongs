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
  ReviewListItemDto,
  ReviewListResult,
} from "../../../shared/bridge.js";
import type { AppRoute, FeatureContribution } from "../../core/navigation.js";
import { formatDate, safeError } from "../../core/presentation.js";
import type { CoordinatedRead } from "../../core/query.js";
import { useRetainedRead } from "../../core/use-read.js";

export type ReviewScope = "my_reviews" | "my_mrs" | "all_open";
export type ReviewSort = "updated" | "title" | "ci" | "author";

const REVIEW_SCOPES: readonly {
  readonly value: ReviewScope;
  readonly label: string;
}[] = Object.freeze([
  { value: "my_reviews", label: "My Reviews" },
  { value: "my_mrs", label: "My MRs" },
  { value: "all_open", label: "All Open" },
]);

const REVIEW_SORTS: readonly {
  readonly value: ReviewSort;
  readonly label: string;
}[] = Object.freeze([
  { value: "updated", label: "Updated" },
  { value: "title", label: "Title" },
  { value: "ci", label: "CI status" },
  { value: "author", label: "Author" },
]);

export interface InboxListSelection {
  readonly scope: ReviewScope;
  readonly state: "open" | "closed";
  readonly sort: ReviewSort;
  readonly selected: string | null;
}

const DEFAULT_LIST_SELECTION: InboxListSelection = Object.freeze({
  scope: "all_open",
  state: "open",
  sort: "updated",
  selected: null,
});

/**
 * Session memory of each review list, keyed by repository scope. Opening a
 * review unmounts the list, so without this the chosen scope, state, sort and
 * the review the user was on are all lost on the most repeated navigation in
 * the app. It records only presentation choices; the scope values handed to
 * `listReviews` are unchanged.
 */
const listSelections = new Map<string, InboxListSelection>();

export function inboxListSelection(key: string): InboxListSelection {
  return listSelections.get(key) ?? DEFAULT_LIST_SELECTION;
}

export function resetInboxListSelections(): void {
  listSelections.clear();
}

const CI_PRIORITY: Readonly<Record<string, number>> = Object.freeze({
  failed: 0,
  running: 1,
  pending: 2,
  success: 3,
  canceled: 4,
  skipped: 5,
  unknown: 6,
});

export function createInboxFeature(): FeatureContribution {
  return {
    id: "reviews.inbox",
    order: 10,
    matches: (route) => route.kind === "inbox",
    render: (context, route) =>
      route.kind === "inbox" ? (
        <InboxView
          key={route.repository?.handle ?? "all"}
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
  const listKey = repository?.handle ?? "all";
  const [listSelection, setListSelection] = useState<InboxListSelection>(() =>
    inboxListSelection(listKey),
  );
  const retain = (change: Partial<InboxListSelection>): void => {
    const next = Object.freeze({ ...listSelection, ...change });
    listSelections.set(listKey, next);
    setListSelection(next);
  };
  const reviewScope = listSelection.scope;
  const reviewState = listSelection.state;
  const reviewSort = listSelection.sort;
  const selectScope = (next: ReviewScope): void => {
    retain(next === "all_open" ? { scope: next } : { scope: next, state: "open" });
  };
  const queryIdentity = `${repository?.handle ?? "all"}:${reviewScope}:${reviewState}`;
  const title = repository?.display_name ?? "All reviews";
  return (
    <>
      <header className="view-header">
        <div>
          <p className="eyebrow">{repository?.forge_type ?? "Workspace"}</p>
          <h1 className="view-title">{title}</h1>
        </div>
      </header>
      <div className="inbox-controls" aria-label="Review list controls">
        <fieldset className="control-group review-scope-control">
          <legend>Review scope</legend>
          <div className="segmented-control">
            {REVIEW_SCOPES.map((option) => (
              <button
                key={option.value}
                className={`button ${reviewScope === option.value ? "button-active" : "button-secondary"}`}
                data-review-scope={option.value}
                aria-pressed={reviewScope === option.value}
                onClick={() => selectScope(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
        </fieldset>
        <fieldset className="control-group review-state-control">
          <legend>Review state</legend>
          <div className="segmented-control">
            <button
              className={`button ${reviewState === "open" ? "button-active" : "button-secondary"}`}
              data-review-state="open"
              aria-pressed={reviewState === "open"}
              onClick={() => retain({ state: "open" })}
            >
              Open
            </button>
            <button
              className={`button ${reviewState === "closed" ? "button-active" : "button-secondary"}`}
              data-review-state="closed"
              aria-pressed={reviewState === "closed"}
              disabled={reviewScope !== "all_open"}
              title={
                reviewScope === "all_open"
                  ? undefined
                  : "Closed and merged reviews are available in All Open."
              }
              onClick={() => retain({ state: "closed" })}
            >
              Closed &amp; merged
            </button>
          </div>
        </fieldset>
        <label className="control-field">
          <span>Sort reviews</span>
          <select
            value={reviewSort}
            onChange={(event) =>
              retain({ sort: event.currentTarget.value as ReviewSort })
            }
          >
            {REVIEW_SORTS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <InboxResults
        key={queryIdentity}
        bridge={bridge}
        queries={queries}
        repository={repository}
        repositories={repositories}
        repositoriesReady={repositoriesReady}
        repositoryGeneration={repositoryGeneration}
        reviewScope={reviewScope}
        reviewState={reviewState}
        reviewSort={reviewSort}
        selected={listSelection.selected}
        select={(handle) => retain({ selected: handle })}
        navigate={navigate}
      />
    </>
  );
}

function InboxResults({
  bridge,
  queries,
  repository,
  repositories,
  repositoriesReady,
  repositoryGeneration,
  reviewScope,
  reviewState,
  reviewSort,
  selected,
  select,
  navigate,
}: {
  readonly bridge: DesktopBridge;
  readonly queries: FeatureParameters["queries"];
  readonly repository: Extract<AppRoute, { kind: "inbox" }>["repository"];
  readonly repositories: FeatureParameters["repositories"];
  readonly repositoriesReady: boolean;
  readonly repositoryGeneration: number;
  readonly reviewScope: ReviewScope;
  readonly reviewState: "open" | "closed";
  readonly reviewSort: ReviewSort;
  readonly selected: string | null;
  readonly select: (handle: string) => void;
  readonly navigate: (route: AppRoute) => void;
}): ReactNode {
  const begin = useCallback(
    () =>
      repository
        ? bridge.listReviews({
            scope: reviewScope,
            state: reviewState,
            repository: repository.handle,
          })
        : listDiscoveredReviews(
            bridge,
            repositories,
            reviewScope,
            reviewState,
          ),
    [bridge, repositories, repository, reviewScope, reviewState],
  );
  const state = useRetainedRead(
    queries,
    `inbox:${repository?.handle ?? "all"}:${reviewScope}:${reviewState}`,
    begin,
    [repository?.handle, repositoryGeneration, reviewScope, reviewState],
    repositoriesReady,
  );
  return (
    <>
      <div className="view-actions">
        <button
          className="button button-secondary"
          disabled={state.loading}
          onClick={state.refresh}
        >
          {state.loading && state.value ? "Refreshing…" : "Refresh reviews"}
        </button>
      </div>
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
          sort={reviewSort}
          selected={selected}
          select={select}
          emptyLabel={emptyReviewLabel(reviewScope, reviewState)}
        />
      )}
    </>
  );
}

export function listDiscoveredReviews(
  bridge: DesktopBridge,
  repositories: readonly { readonly handle: string }[],
  scope: ReviewScope,
  state: "open" | "closed",
): CoordinatedRead<ReviewListResult> {
  const reads = repositories.map((repository) =>
    bridge.listReviews({
      scope,
      state,
      repository: repository.handle,
    }),
  );
  return {
    requestTokens: reads.map((read) => read.requestToken),
    result: Promise.all(reads.map((read) => read.result)).then((results) => ({
      items: results
        .flatMap((result) => result.items),
      failures: results.flatMap((result) => result.failures),
    })),
  };
}

type FeatureParameters = Parameters<FeatureContribution["render"]>[0];

function ReviewList({
  result,
  navigate,
  sort,
  selected,
  select,
  emptyLabel,
}: {
  readonly result: ReviewListResult;
  readonly navigate: (route: AppRoute) => void;
  readonly sort: ReviewSort;
  readonly selected: string | null;
  readonly select: (handle: string) => void;
  readonly emptyLabel: string;
}): ReactNode {
  const presentation = inboxPresentation(result);
  const listRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (selected === null) return;
    const target = [
      ...(listRef.current?.querySelectorAll<HTMLButtonElement>("button") ?? []),
    ].find((button) => button.dataset.reviewHandle === selected);
    if (!target) return;
    target.focus();
    if (typeof target.scrollIntoView === "function")
      target.scrollIntoView({ block: "nearest" });
  }, [selected]);
  const moveFocus = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (
      event.key !== "ArrowDown" &&
      event.key !== "ArrowUp" &&
      event.key !== "Home" &&
      event.key !== "End"
    )
      return;
    const buttons = [
      ...event.currentTarget.querySelectorAll<HTMLButtonElement>("button"),
    ];
    const current = buttons.indexOf(
      document.activeElement as HTMLButtonElement,
    );
    const direction = event.key === "ArrowDown" ? 1 : -1;
    const target =
      event.key === "Home"
        ? buttons[0]
        : event.key === "End"
          ? buttons.at(-1)
          : buttons[
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
        <div
          className="review-list"
          role="list"
          aria-label="Review results"
          ref={listRef}
          onKeyDown={moveFocus}
        >
          {sortReviewItems(result.items, sort).map((item) => (
            <ReviewCard
              key={item.handle}
              item={item}
              selected={item.handle === selected}
              select={select}
              navigate={navigate}
            />
          ))}
        </div>
      )}
    </>
  );
}

export function sortReviewItems(
  items: readonly ReviewListItemDto[],
  sort: ReviewSort,
): readonly ReviewListItemDto[] {
  const sorted = [...items];
  sorted.sort((left, right) => {
    const leftSummary = left.summary;
    const rightSummary = right.summary;
    let order = 0;
    if (sort === "title")
      order = leftSummary.title.localeCompare(rightSummary.title, undefined, {
        sensitivity: "base",
      });
    else if (sort === "ci")
      order =
        (CI_PRIORITY[leftSummary.ci_status] ?? 9) -
          (CI_PRIORITY[rightSummary.ci_status] ?? 9) ||
        leftSummary.title.localeCompare(rightSummary.title, undefined, {
          sensitivity: "base",
        });
    else if (sort === "author")
      order =
        leftSummary.author.username.localeCompare(
          rightSummary.author.username,
          undefined,
          { sensitivity: "base" },
        ) ||
        leftSummary.title.localeCompare(rightSummary.title, undefined, {
          sensitivity: "base",
        });
    else
      order = (rightSummary.updated_at || rightSummary.created_at).localeCompare(
        leftSummary.updated_at || leftSummary.created_at,
      );
    return order || left.handle.localeCompare(right.handle);
  });
  return Object.freeze(sorted);
}

function emptyReviewLabel(
  scope: ReviewScope,
  state: "open" | "closed",
): string {
  if (scope === "my_reviews") return "No open reviews are waiting for you.";
  if (scope === "my_mrs") return "You have no open merge requests.";
  return state === "open"
    ? "No open reviews match this repository scope."
    : "No closed or merged reviews match this repository scope.";
}

function ReviewCard({
  item,
  selected,
  select,
  navigate,
}: {
  readonly item: ReviewListItemDto;
  readonly selected: boolean;
  readonly select: (handle: string) => void;
  readonly navigate: (route: AppRoute) => void;
}): ReactNode {
  return (
    <button
      className={`review-card${selected ? " review-card-selected" : ""}`}
      role="listitem"
      data-review-number={item.summary.number}
      data-review-handle={item.handle}
      aria-current={selected ? "true" : undefined}
      onClick={() => {
        select(item.handle);
        navigate({ kind: "review", item, panel: "overview" });
      }}
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
