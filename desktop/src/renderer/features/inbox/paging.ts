import type {
  ReviewFailureDto,
  ReviewListItemDto,
} from "../../../shared/bridge.js";

/**
 * What the inbox has loaded from one repository. Pages arrive newest update
 * first, so `items` is the repository's newest reviews down to its frontier,
 * the oldest item loaded so far. `cursor` is the sealed cursor for the next
 * page, or null when the repository has no more pages (or none yet known).
 */
export interface RepositoryFeed {
  readonly repository: string;
  readonly items: readonly ReviewListItemDto[];
  readonly cursor: string | null;
  readonly loading: boolean;
  readonly failures: readonly ReviewFailureDto[];
  /** How many pages have been appended, so a reload can read as deep again. */
  readonly pages: number;
}

export function pendingFeed(repository: string): RepositoryFeed {
  return Object.freeze({
    repository,
    items: Object.freeze([]),
    cursor: null,
    loading: true,
    failures: Object.freeze([]),
    pages: 0,
  });
}

/** Milliseconds of a review's last update, or of creation when unset. */
export function reviewTime(item: ReviewListItemDto): number {
  const parsed = Date.parse(item.summary.updated_at || item.summary.created_at);
  return Number.isFinite(parsed) ? parsed : 0;
}

function frontier(feed: RepositoryFeed): number | null {
  const last = feed.items.at(-1);
  return last === undefined ? null : reviewTime(last);
}

/**
 * The oldest update time down to which the order across repositories is
 * known. A repository with more pages may still hold reviews older than its
 * frontier, so rows below the newest such frontier could later gain a review
 * above them. Rows at or above it are final. Null means every loaded row is
 * final. A repository whose first page has not arrived yet does not hold rows
 * back; its rows are merged in place when they arrive.
 */
export function orderWatermark(
  feeds: readonly RepositoryFeed[],
): number | null {
  let watermark: number | null = null;
  for (const feed of feeds) {
    if (feed.cursor === null) continue;
    const edge = frontier(feed);
    if (edge === null) continue;
    if (watermark === null || edge > watermark) watermark = edge;
  }
  return watermark;
}

/** Every loaded review once, newest update first. */
export function mergedReviewItems(
  feeds: readonly RepositoryFeed[],
): readonly ReviewListItemDto[] {
  const seen = new Set<string>();
  const merged: ReviewListItemDto[] = [];
  for (const feed of feeds)
    for (const item of feed.items) {
      // A review updated between two page reads can appear on both pages.
      if (seen.has(item.handle)) continue;
      seen.add(item.handle);
      merged.push(item);
    }
  merged.sort(
    (left, right) =>
      reviewTime(right) - reviewTime(left) ||
      left.handle.localeCompare(right.handle),
  );
  return Object.freeze(merged);
}

/**
 * Rows the update-time order can show: the merged reviews down to the
 * watermark. Other sort orders show every loaded row instead.
 */
export function orderedReviewItems(
  feeds: readonly RepositoryFeed[],
): readonly ReviewListItemDto[] {
  const merged = mergedReviewItems(feeds);
  const watermark = orderWatermark(feeds);
  if (watermark === null) return merged;
  return Object.freeze(merged.filter((item) => reviewTime(item) >= watermark));
}

/**
 * The repository to read next: the one with more pages whose frontier is the
 * newest, because its next page decides the rows right below the watermark.
 * Null when every repository is complete or a read is already in flight for
 * the candidate.
 */
export function nextRepositoryToLoad(
  feeds: readonly RepositoryFeed[],
): RepositoryFeed | null {
  let best: RepositoryFeed | null = null;
  let bestEdge = -Infinity;
  for (const feed of feeds) {
    if (feed.cursor === null || feed.loading) continue;
    const edge = frontier(feed) ?? Infinity;
    if (best === null || edge > bestEdge) {
      best = feed;
      bestEdge = edge;
    }
  }
  return best;
}

/**
 * Whether a repository's first page has settled. A later page read marks the
 * feed loading again, but its rows stay on screen while it runs.
 */
export function firstPageArrived(feed: RepositoryFeed): boolean {
  return !feed.loading || feed.items.length > 0 || feed.failures.length > 0;
}

export function loadingFeedCount(feeds: readonly RepositoryFeed[]): number {
  return feeds.filter((feed) => feed.loading).length;
}

export function hasMorePages(feeds: readonly RepositoryFeed[]): boolean {
  return feeds.some((feed) => feed.cursor !== null);
}

export function feedFailures(
  feeds: readonly RepositoryFeed[],
): readonly ReviewFailureDto[] {
  return Object.freeze(feeds.flatMap((feed) => feed.failures));
}

/**
 * Append one more page to a repository, keeping its earlier rows. A page read
 * that failed at the forge arrives as a successful response with no rows, a
 * failure and no next cursor; the repository keeps its cursor then, so it
 * still holds the watermark and Load more can read that page again.
 */
export function appendPage(
  feed: RepositoryFeed,
  items: readonly ReviewListItemDto[],
  cursor: string | null,
  failures: readonly ReviewFailureDto[],
): RepositoryFeed {
  const failed = items.length === 0 && failures.length > 0;
  return Object.freeze({
    repository: feed.repository,
    items: Object.freeze([...feed.items, ...items]),
    cursor: cursor ?? (failed ? feed.cursor : null),
    loading: false,
    failures: Object.freeze([...failures]),
    pages: failed ? feed.pages : feed.pages + 1,
  });
}
