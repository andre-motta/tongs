import type {
  DesktopBridge,
  DesktopRead,
  DiscussionDto,
  DiscussionsPage,
  DiscussionsResult,
} from "../../../shared/bridge.js";
import {
  RendererReadError,
  serviceErrorOf,
} from "../../core/presentation.js";
import type { CoordinatedRead } from "../../core/query.js";

export type DiscussionPagesBridge = Pick<
  DesktopBridge,
  "listDiscussions" | "pageDiscussions" | "cancelRead"
>;

/** Bounds on one paged read, matching the sidecar's retained-row limit. */
export const MAX_DISCUSSION_PAGES = 1000;
export const MAX_DISCUSSIONS = 100_000;
/**
 * A change to the review expires its snapshot or changes its thread count, so
 * a read that lands on an expired or changed page starts over this many times
 * before it reports the failure.
 */
const EXPIRED_RESTARTS = 1;
const RESTARTABLE_CODES = new Set(["snapshot_expired", "discussions_changed"]);

/**
 * A read of every page of a review's discussions. `requestTokens` grows as
 * each page is requested, and `cancel` stops the loop and cancels the page in
 * flight, so one handle covers the whole paged read.
 */
export interface DiscussionsRead extends CoordinatedRead<DiscussionsResult> {
  cancel(): void;
}

/**
 * Reads a review's discussions page by page and resolves with all of them.
 *
 * The sidecar keeps each page well inside the JSON value budget, so a review
 * with a thousand or more threads arrives over several pages; every surface
 * that shows threads reads them through here so none of them stops at the
 * first page. When the review changes part way through, the sidecar expires
 * the snapshot and the read starts over once from the first page.
 */
export function readAllDiscussions(
  bridge: DiscussionPagesBridge,
  review: string,
): DiscussionsRead {
  const tokens: string[] = [];
  let inFlight: string | null = null;
  let cancelled = false;
  const track = <T>(read: DesktopRead<T>): Promise<T> => {
    tokens.push(read.requestToken);
    inFlight = read.requestToken;
    return read.result;
  };
  const readOnce = async (): Promise<DiscussionsResult> => {
    const first = await track(bridge.listDiscussions(review));
    const discussions: DiscussionDto[] = [...first.discussions];
    let page: DiscussionsPage = first;
    let pageCount = 1;
    // A page without a cursor is the whole list in one answer.
    while (typeof page.next_cursor === "number") {
      if (cancelled) throw cancelledRead();
      if (page.next_cursor <= page.cursor)
        throw invalidPage("The discussions page cursor did not advance.");
      if (
        pageCount >= MAX_DISCUSSION_PAGES ||
        discussions.length >= MAX_DISCUSSIONS
      )
        throw new RendererReadError(
          "discussions_limit",
          "The review has more discussions than this app can show.",
          false,
        );
      const cursor = page.next_cursor;
      page = await track(
        bridge.pageDiscussions({
          snapshot: first.snapshot_id,
          resource: first.resource,
          cursor,
        }),
      );
      if (
        page.snapshot_id !== first.snapshot_id ||
        page.resource !== first.resource ||
        page.revision.discussion_count !== first.revision.discussion_count
      )
        throw discussionsChanged();
      if (page.cursor !== cursor)
        throw invalidPage("The discussions page cursor did not match the request.");
      discussions.push(...page.discussions);
      pageCount += 1;
    }
    // The pages must add up to the count the snapshot was taken with.
    if (discussions.length !== first.revision.discussion_count)
      throw discussionsChanged();
    return Object.freeze({ discussions: Object.freeze(discussions) });
  };
  const result = (async (): Promise<DiscussionsResult> => {
    for (let restarts = 0; ; restarts += 1) {
      try {
        return await readOnce();
      } catch (error) {
        if (
          cancelled ||
          restarts >= EXPIRED_RESTARTS ||
          !RESTARTABLE_CODES.has(serviceErrorOf(error)?.code ?? "")
        )
          throw error;
      }
    }
  })();
  return {
    requestTokens: tokens,
    result,
    cancel: () => {
      cancelled = true;
      if (inFlight === null) return;
      try {
        void bridge.cancelRead(inFlight).catch(() => false);
      } catch {
        // A cancellation that cannot be delivered must not fail the caller.
      }
    },
  };
}

function discussionsChanged(): RendererReadError {
  return new RendererReadError(
    "discussions_changed",
    "The discussions changed while they were being read.",
    true,
  );
}

function invalidPage(message: string): RendererReadError {
  return new RendererReadError("invalid_discussions_page", message, false);
}

function cancelledRead(): RendererReadError {
  return new RendererReadError(
    "request_cancelled",
    "The discussions read was cancelled.",
    false,
  );
}
