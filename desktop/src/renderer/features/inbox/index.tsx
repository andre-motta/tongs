import {
  memo,
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type {
  DesktopBridge,
  ReviewFailureDto,
  ReviewListItemDto,
  ReviewListResult,
} from "../../../shared/bridge.js";
import type { AppRoute, FeatureContribution } from "../../core/navigation.js";
import {
  formatDate,
  RendererReadError,
  safeError,
  serviceErrorOf,
} from "../../core/presentation.js";
import { StaleQueryError, type CoordinatedRead } from "../../core/query.js";
import { useRetainedRead } from "../../core/use-read.js";
import {
  appendPage,
  feedFailures,
  hasMorePages,
  loadingFeedCount,
  mergedReviewItems,
  nextRepositoryToLoad,
  orderedReviewItems,
  pendingFeed,
  type RepositoryFeed,
} from "./paging.js";

export {
  mergedReviewItems,
  nextRepositoryToLoad,
  orderedReviewItems,
  orderWatermark,
  type RepositoryFeed,
} from "./paging.js";

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

/**
 * Review the list should hand focus back to, recorded only when a card
 * navigates away and consumed by the first restore that finds it. Keeping this
 * apart from `selected` is what makes the restore one-shot: `selected` outlives
 * it for the current-row marking, so a filter change, which remounts the
 * result list, must not be able to pull focus out of the control just used.
 */
const pendingSelectionFocus = new Map<string, string>();

export function inboxListSelection(key: string): InboxListSelection {
  return listSelections.get(key) ?? DEFAULT_LIST_SELECTION;
}

export function inboxPendingSelectionFocus(key: string): string | null {
  return pendingSelectionFocus.get(key) ?? null;
}

export function resetInboxListSelections(): void {
  listSelections.clear();
  pendingSelectionFocus.clear();
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
  const [focusTarget, setFocusTarget] = useState<string | null>(() =>
    inboxPendingSelectionFocus(listKey),
  );
  const retain = (change: Partial<InboxListSelection>): void => {
    const next = Object.freeze({ ...listSelection, ...change });
    listSelections.set(listKey, next);
    setListSelection(next);
  };
  const releaseFocusTarget = (): void => {
    pendingSelectionFocus.delete(listKey);
    setFocusTarget(null);
  };
  const changeFilter = (change: Partial<InboxListSelection>): void => {
    releaseFocusTarget();
    retain(change);
  };
  const openReview = (handle: string): void => {
    pendingSelectionFocus.set(listKey, handle);
    retain({ selected: handle });
  };
  const reviewScope = listSelection.scope;
  const reviewState = listSelection.state;
  const reviewSort = listSelection.sort;
  const selectScope = (next: ReviewScope): void => {
    changeFilter(
      next === "all_open" ? { scope: next } : { scope: next, state: "open" },
    );
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
              onClick={() => changeFilter({ state: "open" })}
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
              onClick={() => changeFilter({ state: "closed" })}
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
              changeFilter({ sort: event.currentTarget.value as ReviewSort })
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
        select={openReview}
        focusTarget={focusTarget}
        releaseFocusTarget={releaseFocusTarget}
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
  focusTarget,
  releaseFocusTarget,
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
  readonly focusTarget: string | null;
  readonly releaseFocusTarget: () => void;
  readonly navigate: (route: AppRoute) => void;
}): ReactNode {
  const readKey = `inbox:${repository?.handle ?? "all"}:${reviewScope}:${reviewState}`;
  // The combined read queues repositories it has not started yet, and the
  // query coordinator can only cancel reads that already have a token, so the
  // queue is stopped through this controller whenever the read is replaced,
  // disabled or unmounted.
  const discoveredRead = useRef<AbortController | null>(null);
  // Bumped by every new combined read, so a page that belongs to an older
  // read (or a later page requested before a refresh) is dropped.
  const readGeneration = useRef(0);
  const moreReads = useRef(new Set<string>());
  // Every page loaded so far, one entry per repository in discovery order.
  const [feeds, setFeeds] = useState<readonly RepositoryFeed[] | null>(null);
  const feedsRef = useRef(feeds);
  feedsRef.current = feeds;
  // The first read fills `feeds` as each repository's first page arrives. A
  // refresh keeps showing the previous pages and swaps them in when complete.
  const liveRead = useRef(false);
  const hasValue = useRef(false);
  const cancelMoreReads = useCallback(() => {
    for (const key of moreReads.current) void queries.cancel(key);
    moreReads.current.clear();
  }, [queries]);
  const begin = useCallback(() => {
    discoveredRead.current?.abort();
    discoveredRead.current = null;
    cancelMoreReads();
    const generation = ++readGeneration.current;
    const controller = new AbortController();
    discoveredRead.current = controller;
    const sources = repository ? [repository] : repositories;
    const live = !hasValue.current;
    liveRead.current = live;
    if (live)
      setFeeds(
        Object.freeze(sources.map((source) => pendingFeed(source.handle))),
      );
    return listDiscoveredReviews(
      bridge,
      sources,
      reviewScope,
      reviewState,
      controller.signal,
      live
        ? (index, feed) => {
            if (generation !== readGeneration.current) return;
            setFeeds((current) =>
              current && current[index]?.repository === feed.repository
                ? Object.freeze(
                    current.map((item, at) => (at === index ? feed : item)),
                  )
                : current,
            );
          }
        : undefined,
    );
  }, [
    bridge,
    cancelMoreReads,
    repositories,
    repository,
    reviewScope,
    reviewState,
  ]);
  const readDependencies = [
    repository?.handle,
    repositoryGeneration,
    reviewScope,
    reviewState,
  ];
  const state = useRetainedRead(
    queries,
    readKey,
    begin,
    readDependencies,
    repositoriesReady,
  );
  useEffect(() => {
    if (!state.value) return;
    hasValue.current = true;
    // A live read already holds these pages and any loaded after them.
    if (!liveRead.current) setFeeds(state.value.feeds);
    liveRead.current = false;
  }, [state.value]);
  useEffect(
    () => () => {
      discoveredRead.current?.abort();
      discoveredRead.current = null;
      cancelMoreReads();
    },
    [...readDependencies, repositoriesReady],
  );

  const loadMore = useCallback(
    (target: string) => {
      const key = `${readKey}:more:${target}`;
      const feed = feedsRef.current?.find((item) => item.repository === target);
      if (
        moreReads.current.has(key) ||
        !feed ||
        feed.cursor === null ||
        feed.loading
      )
        return;
      const generation = readGeneration.current;
      const cursor = feed.cursor;
      moreReads.current.add(key);
      const update = (
        change: (current: RepositoryFeed) => RepositoryFeed,
      ): void =>
        setFeeds((current) => {
          const found = current?.find((item) => item.repository === target);
          return current && found
            ? replaceFeed(current, change(found))
            : current;
        });
      const settle = (
        change: (current: RepositoryFeed) => RepositoryFeed,
      ): void => {
        if (generation !== readGeneration.current) return;
        moreReads.current.delete(key);
        update(change);
      };
      update((current) => ({ ...current, loading: true }));
      void queries
        .run(key, () =>
          bridge.listReviews({
            scope: reviewScope,
            state: reviewState,
            repository: target,
            cursor,
          }),
        )
        .then(
          (page) =>
            settle((current) =>
              appendPage(
                current,
                page.items,
                page.next_cursor ?? null,
                page.failures,
              ),
            ),
          (error: unknown) =>
            settle((current) =>
              error instanceof StaleQueryError
                ? { ...current, loading: false }
                : {
                    ...current,
                    loading: false,
                    failures: [repositoryFailure(target, error)],
                  },
            ),
        );
    },
    [bridge, queries, readKey, reviewScope, reviewState],
  );

  const shown = state.error && !state.value ? null : feeds;
  // With no repositories there is no feed to settle, so the list (and its
  // empty label) is shown once the read itself has finished.
  const listReady =
    shown !== null &&
    (shown.some((feed) => !feed.loading) ||
      (shown.length === 0 && !state.loading && Boolean(state.value)));
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
      {state.loading && !shown?.some((feed) => !feed.loading) && (
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
      {listReady && shown !== null && (
        <ReviewList
          feeds={shown}
          navigate={navigate}
          sort={reviewSort}
          selected={selected}
          select={select}
          focusTarget={focusTarget}
          releaseFocusTarget={releaseFocusTarget}
          emptyLabel={emptyReviewLabel(reviewScope, reviewState)}
          loadMore={loadMore}
        />
      )}
    </>
  );
}

function replaceFeed(
  feeds: readonly RepositoryFeed[],
  next: RepositoryFeed,
): readonly RepositoryFeed[] {
  return Object.freeze(
    feeds.map((feed) =>
      feed.repository === next.repository ? Object.freeze(next) : feed,
    ),
  );
}

/**
 * Most repository reads the combined inbox keeps in flight at once. The main
 * process refuses a read once 64 requests are pending, so fanning out one read
 * per discovered repository at once fails every tab for larger workspaces.
 */
export const DISCOVERED_REVIEW_CONCURRENCY = 8;

/**
 * Read one review list per discovered repository through a small pool and
 * merge them in repository order. A repository whose read is refused or fails
 * becomes one entry in `failures` instead of rejecting the whole list.
 *
 * `requestTokens` grows as reads start, so a coordinator cancelling this read
 * reaches every read in flight at that moment. Aborting `signal` also cancels
 * the reads in flight, starts none of the queued ones, and rejects the result.
 */
export function listDiscoveredReviews(
  bridge: DesktopBridge,
  repositories: readonly { readonly handle: string }[],
  scope: ReviewScope,
  state: "open" | "closed",
  signal?: AbortSignal,
  onRepository?: (index: number, feed: RepositoryFeed) => void,
): CoordinatedRead<DiscoveredReviews> {
  const handles = repositories.map((repository) => repository.handle);
  const requestTokens: string[] = [];
  const inFlight = new Set<string>();
  const outcomes: ReviewListResult[] = [];
  const feeds: RepositoryFeed[] = handles.map((handle) => pendingFeed(handle));
  let nextIndex = 0;
  // Set when a read comes back cancelled without our signal firing, for
  // example when the coordinator cancels every read on a cache clear.
  let stopped = false;
  const aborted = (): boolean => signal?.aborted === true;

  const readRepository = async (index: number): Promise<void> => {
    const handle = handles[index] as string;
    let token: string | null = null;
    try {
      const read = bridge.listReviews({ scope, state, repository: handle });
      token = read.requestToken;
      requestTokens.push(token);
      inFlight.add(token);
      outcomes[index] = await read.result;
    } catch (error) {
      if (serviceErrorOf(error)?.code === "request_cancelled") stopped = true;
      outcomes[index] = { items: [], failures: [repositoryFailure(handle, error)] };
    } finally {
      if (token !== null) inFlight.delete(token);
    }
    const outcome = outcomes[index] as ReviewListResult;
    feeds[index] = appendPage(
      feeds[index] as RepositoryFeed,
      outcome.items,
      outcome.next_cursor ?? null,
      outcome.failures,
    );
    // Each repository's first page is shown as soon as it arrives.
    if (!aborted() && !stopped) onRepository?.(index, feeds[index] as RepositoryFeed);
  };
  const worker = async (): Promise<void> => {
    while (!aborted() && !stopped && nextIndex < handles.length) {
      const index = nextIndex;
      nextIndex += 1;
      await readRepository(index);
    }
  };

  const result = new Promise<DiscoveredReviews>((resolve, reject) => {
    const cancelled = (): RendererReadError =>
      new RendererReadError(
        "request_cancelled",
        "The review list read was cancelled.",
        false,
      );
    if (aborted()) {
      reject(cancelled());
      return;
    }
    const onAbort = (): void => {
      for (const token of inFlight)
        void bridge.cancelRead(token).catch(() => false);
      reject(cancelled());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    const workers = Array.from(
      { length: Math.min(DISCOVERED_REVIEW_CONCURRENCY, handles.length) },
      () => worker(),
    );
    void Promise.all(workers).then(() => {
      signal?.removeEventListener("abort", onAbort);
      if (aborted()) return;
      if (stopped) {
        reject(cancelled());
        return;
      }
      const items = outcomes.flatMap((outcome) => outcome.items);
      const failures = outcomes.flatMap((outcome) => outcome.failures);
      // Every repository failing usually means the service itself is down,
      // so surface the error state rather than an empty list.
      const first = failures[0];
      if (items.length === 0 && first !== undefined && failures.length === handles.length) {
        reject(new RendererReadError(first.code, first.message, first.retryable));
        return;
      }
      resolve({ items, failures, feeds: Object.freeze([...feeds]) });
    });
  });
  return { requestTokens, result };
}

/** A combined read: the flat list plus what each repository has loaded. */
export interface DiscoveredReviews extends ReviewListResult {
  readonly feeds: readonly RepositoryFeed[];
}

function repositoryFailure(
  repository: string,
  error: unknown,
): ReviewFailureDto {
  const failure = serviceErrorOf(error);
  return Object.freeze({
    repository,
    code: failure?.code ?? "read_failed",
    message:
      failure?.message ?? "The local service could not complete this read.",
    retryable: failure?.retryable ?? true,
  });
}

type FeatureParameters = Parameters<FeatureContribution["render"]>[0];

function ReviewList({
  feeds,
  navigate,
  sort,
  selected,
  select,
  focusTarget,
  releaseFocusTarget,
  emptyLabel,
  loadMore,
}: {
  readonly feeds: readonly RepositoryFeed[];
  readonly navigate: (route: AppRoute) => void;
  readonly sort: ReviewSort;
  readonly selected: string | null;
  readonly select: (handle: string) => void;
  readonly focusTarget: string | null;
  readonly releaseFocusTarget: () => void;
  readonly emptyLabel: string;
  readonly loadMore: (repository: string) => void;
}): ReactNode {
  const presentation = inboxFeedPresentation(feeds, sort);
  const items = presentation.items;
  const listRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const [nearEnd, setNearEnd] = useState(false);
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  const selectRef = useRef(select);
  selectRef.current = select;
  const open = useCallback((item: ReviewListItemDto) => {
    selectRef.current(item.handle);
    navigateRef.current({ kind: "review", item, panel: "overview" });
  }, []);
  useEffect(() => {
    if (focusTarget === null) return;
    const target = [
      ...(listRef.current?.querySelectorAll<HTMLButtonElement>("button") ?? []),
    ].find((button) => button.dataset.reviewHandle === focusTarget);
    if (!target) return;
    target.focus();
    if (typeof target.scrollIntoView === "function")
      target.scrollIntoView({ block: "nearest" });
    releaseFocusTarget();
    // Rows can arrive after the list mounts; retry until the target is shown.
  }, [focusTarget, items.length]);
  // Update-time order loads the next page by itself when the end of the list
  // comes into view. Other orders only load when asked through the button.
  const autoLoad = sort === "updated";
  useEffect(() => {
    const sentinel = sentinelRef.current;
    const Observer = globalThis.IntersectionObserver;
    if (!autoLoad || !sentinel || typeof Observer !== "function") return;
    const observer = new Observer(
      (entries) => setNearEnd(entries.some((entry) => entry.isIntersecting)),
      { rootMargin: "0px 0px 600px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [autoLoad, presentation.next !== null]);
  // A repository whose last page read failed is retried only on request, so
  // a persistent failure cannot turn scrolling into a request loop.
  const autoNext =
    presentation.next !== null &&
    feeds.some(
      (feed) =>
        feed.repository === presentation.next && feed.failures.length === 0,
    )
      ? presentation.next
      : null;
  useEffect(() => {
    if (autoLoad && nearEnd && autoNext && presentation.loading === 0)
      loadMore(autoNext);
  }, [autoLoad, nearEnd, autoNext, presentation.loading, loadMore]);
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
          {items.map((item) => (
            <ReviewCard
              key={item.handle}
              item={item}
              selected={item.handle === selected}
              open={open}
            />
          ))}
        </div>
      )}
      <div ref={sentinelRef} className="review-list-end" aria-hidden="true" />
      {(presentation.loading > 0 || presentation.next !== null) && (
        <div className="review-list-more">
          {presentation.loading > 0 && (
            <span role="status" className="review-list-status">
              Loading more from {presentation.loading}{" "}
              {presentation.loading === 1 ? "repository" : "repositories"}
            </span>
          )}
          {presentation.next !== null && (
            <button
              className="button button-secondary"
              data-load-more={presentation.next}
              disabled={presentation.loading > 0}
              onClick={() => {
                if (presentation.next) loadMore(presentation.next);
              }}
            >
              Load more
            </button>
          )}
        </div>
      )}
    </>
  );
}

/**
 * What the list shows for the pages loaded so far: the rows in the chosen
 * order, how many repositories are still reading, and which repository to
 * read next. In update-time order rows stop at the point where the order
 * across repositories is known.
 */
export function inboxFeedPresentation(
  feeds: readonly RepositoryFeed[],
  sort: ReviewSort,
): {
  readonly items: readonly ReviewListItemDto[];
  readonly empty: boolean;
  readonly partialFailures: number;
  readonly loading: number;
  readonly next: string | null;
} {
  const items =
    sort === "updated"
      ? orderedReviewItems(feeds)
      : sortReviewItems(mergedReviewItems(feeds), sort);
  const loading = loadingFeedCount(feeds);
  return Object.freeze({
    items,
    empty: items.length === 0 && loading === 0 && !hasMorePages(feeds),
    partialFailures: feedFailures(feeds).length,
    loading,
    next: nextRepositoryToLoad(feeds)?.repository ?? null,
  });
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

const ReviewCard = memo(function ReviewCard({
  item,
  selected,
  open,
}: {
  readonly item: ReviewListItemDto;
  readonly selected: boolean;
  readonly open: (item: ReviewListItemDto) => void;
}): ReactNode {
  return (
    <button
      className={`review-card${selected ? " review-card-selected" : ""}`}
      role="listitem"
      data-review-number={item.summary.number}
      data-review-handle={item.handle}
      aria-current={selected ? "true" : undefined}
      onClick={() => open(item)}
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
});

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
