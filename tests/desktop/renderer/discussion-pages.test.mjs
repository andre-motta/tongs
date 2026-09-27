import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { createDiffFeature } from "../../../desktop/dist/src/renderer/features/diff/index.js";
import { readAllDiscussions } from "../../../desktop/dist/src/renderer/features/review/discussions.js";
import { clearPendingEdit } from "../../../desktop/dist/src/renderer/features/review/drawer.js";

// A review with this many threads could not open its diff in 1.0.2 (#292):
// the one discussions frame overflowed the JSON value budget.
const THREAD_COUNT = 1200;
const PAGE_SIZE = 400;

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const { JSDOM } = desktopRequire("jsdom");
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://app.invalid/",
});
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
});
const { cleanup, render, waitFor } = desktopRequire("@testing-library/react");
const React = desktopRequire("react");
afterEach(() => {
  cleanup();
  clearPendingEdit();
});

const REVISION = {
  head_sha: "f8bbf4877cd1a58f2a3a9d1a1a86bd2f4b7a1c33",
  base_sha: "df2bd3f",
  start_sha: null,
};

test("the diff counts every thread of a review read over several pages", async () => {
  const review = "paged-threads";
  const threads = Array.from({ length: THREAD_COUNT }, (_, index) =>
    // Anchored below the fixture hunk, so the header counts them without the
    // window painting a row for each.
    thread(`d-${index}`, { line: 1000 + index, resolved: index % 4 === 0 }),
  );
  const pages = pagedDiscussions(review, threads, PAGE_SIZE);
  const view = renderDiff(
    diffBridge(review, {
      listDiscussions: pages.list,
      pageDiscussions: pages.page,
    }),
    review,
  );

  await waitFor(() =>
    assert.equal(
      fileThreadText(view),
      `${THREAD_COUNT} threads, ${THREAD_COUNT - THREAD_COUNT / 4} unresolved`,
    ),
  );
  assert.equal(pages.calls.list, 1);
  assert.deepEqual(pages.calls.cursors, [400, 800]);
  assert.equal(view.container.querySelectorAll(".notice-error").length, 0);
  assert.equal(
    view.container.textContent.includes("discussions could not be read"),
    false,
  );
});

test("a single-page answer still reads as the whole list", async () => {
  const review = "one-page";
  const threads = [thread("d-1", { line: 1000 }), thread("d-2", { line: 1001 })];
  const pages = pagedDiscussions(review, threads, PAGE_SIZE);
  const read = readAllDiscussions(
    { listDiscussions: pages.list, pageDiscussions: pages.page, cancelRead: async () => true },
    review,
  );

  const result = await read.result;

  assert.equal(result.discussions.length, 2);
  assert.equal(pages.calls.cursors.length, 0);
  assert.equal(read.requestTokens.length, 1);
});

test("the paged read joins its pages in order and tracks each request", async () => {
  const review = "ordered";
  const threads = Array.from({ length: 25 }, (_, index) =>
    thread(`d-${index}`, { line: 1000 + index }),
  );
  const pages = pagedDiscussions(review, threads, 10);
  const read = readAllDiscussions(
    { listDiscussions: pages.list, pageDiscussions: pages.page, cancelRead: async () => true },
    review,
  );

  const result = await read.result;

  assert.equal(
    result.discussions.map((discussion) => discussion.id).join(","),
    threads.map((discussion) => discussion.id).join(","),
  );
  assert.deepEqual(pages.calls.cursors, [10, 20]);
  assert.equal(read.requestTokens.length, 3);
});

test("cancelling a paged read stops it and cancels the page in flight", async () => {
  const review = "cancelled";
  const threads = Array.from({ length: 30 }, (_, index) =>
    thread(`d-${index}`, { line: 1000 + index }),
  );
  const pages = pagedDiscussions(review, threads, 10);
  let release = () => {};
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const cancelled = [];
  const read = readAllDiscussions(
    {
      listDiscussions: pages.list,
      pageDiscussions: (params) => {
        const next = pages.page(params);
        return {
          requestToken: next.requestToken,
          result: gate.then(() => next.result),
        };
      },
      cancelRead: async (token) => {
        cancelled.push(token);
        return true;
      },
    },
    review,
  );
  await waitFor(() => assert.equal(read.requestTokens.length, 2));

  read.cancel();
  release();

  await assert.rejects(read.result, { code: "request_cancelled" });
  assert.equal(cancelled.length, 1);
  assert.equal(cancelled[0], read.requestTokens[1]);
  assert.deepEqual(pages.calls.cursors, [10]);
});

test("a page from another snapshot fails the read", async () => {
  const review = "crossed";
  const threads = Array.from({ length: 15 }, (_, index) =>
    thread(`d-${index}`, { line: 1000 + index }),
  );
  const pages = pagedDiscussions(review, threads, 10);
  const read = readAllDiscussions(
    {
      listDiscussions: pages.list,
      pageDiscussions: (params) => {
        const next = pages.page(params);
        return {
          requestToken: next.requestToken,
          result: next.result.then((page) => ({ ...page, snapshot_id: "other" })),
        };
      },
      cancelRead: async () => true,
    },
    review,
  );

  await assert.rejects(read.result, { code: "discussions_changed" });
  // The one automatic restart ran before the read reported the change.
  assert.equal(pages.calls.list, 2);
});

test("pages that do not add up to the snapshot count restart the read once", async () => {
  const review = "short-count";
  const threads = Array.from({ length: 15 }, (_, index) =>
    thread(`d-${index}`, { line: 1000 + index }),
  );
  const pages = pagedDiscussions(review, threads, 10);
  let short = 1;
  const read = readAllDiscussions(
    {
      listDiscussions: pages.list,
      pageDiscussions: (params) => {
        const next = pages.page(params);
        if (short === 0) return next;
        short -= 1;
        return {
          requestToken: next.requestToken,
          result: next.result.then((page) => ({
            ...page,
            discussions: page.discussions.slice(1),
          })),
        };
      },
      cancelRead: async () => true,
    },
    review,
  );

  const result = await read.result;

  assert.equal(result.discussions.length, 15);
  assert.equal(pages.calls.list, 2);
});

test("a single page that disagrees with its count fails after one restart", async () => {
  const review = "one-page-short";
  const threads = [thread("d-1", { line: 1000 }), thread("d-2", { line: 1001 })];
  const pages = pagedDiscussions(review, threads, PAGE_SIZE);
  const read = readAllDiscussions(
    {
      listDiscussions: () => {
        const first = pages.list();
        return {
          requestToken: first.requestToken,
          result: first.result.then((page) => ({
            ...page,
            revision: { discussion_count: 3 },
          })),
        };
      },
      pageDiscussions: pages.page,
      cancelRead: async () => true,
    },
    review,
  );

  const failure = await read.result.then(
    () => null,
    (error) => ({ code: error.code, retryable: error.retryable }),
  );
  assert.deepEqual(failure, { code: "discussions_changed", retryable: true });
  assert.equal(pages.calls.list, 2);
});

test("a snapshot that expires part way through restarts the read once", async () => {
  const review = "expired";
  const threads = Array.from({ length: 15 }, (_, index) =>
    thread(`d-${index}`, { line: 1000 + index }),
  );
  const pages = pagedDiscussions(review, threads, 10);
  let expiries = 1;
  const read = readAllDiscussions(
    {
      listDiscussions: pages.list,
      pageDiscussions: (params) => {
        if (expiries > 0) {
          expiries -= 1;
          return {
            requestToken: crypto.randomUUID(),
            result: Promise.reject(expired()),
          };
        }
        return pages.page(params);
      },
      cancelRead: async () => true,
    },
    review,
  );

  const result = await read.result;

  assert.equal(result.discussions.length, 15);
  assert.equal(pages.calls.list, 2);
});

test("a snapshot that keeps expiring fails after one restart", async () => {
  const review = "expired-twice";
  const threads = Array.from({ length: 15 }, (_, index) =>
    thread(`d-${index}`, { line: 1000 + index }),
  );
  const pages = pagedDiscussions(review, threads, 10);
  const read = readAllDiscussions(
    {
      listDiscussions: pages.list,
      pageDiscussions: () => ({
        requestToken: crypto.randomUUID(),
        result: Promise.reject(expired()),
      }),
      cancelRead: async () => true,
    },
    review,
  );

  await assert.rejects(read.result, { code: "snapshot_expired" });
  assert.equal(pages.calls.list, 2);
});

function expired() {
  return Object.assign(new Error("The resource snapshot expired."), {
    code: "snapshot_expired",
    retryable: true,
  });
}

/** Serves `threads` the way the sidecar pages a discussions snapshot. */
function pagedDiscussions(review, threads, size) {
  const calls = { list: 0, cursors: [] };
  const pageAt = (cursor) => {
    const end = Math.min(cursor + size, threads.length);
    return {
      snapshot_id: "discussions-snapshot",
      resource: review,
      revision: { discussion_count: threads.length },
      cursor,
      next_cursor: end < threads.length ? end : null,
      discussions: threads.slice(cursor, end),
    };
  };
  return {
    calls,
    list: () => {
      calls.list += 1;
      return read(pageAt(0));
    },
    page: (params) => {
      assert.equal(params.snapshot, "discussions-snapshot");
      assert.equal(params.resource, review);
      calls.cursors.push(params.cursor);
      return read(pageAt(params.cursor));
    },
  };
}

function fileThreadText(view) {
  const counts = [...view.container.querySelectorAll(".file-threads")].map(
    (node) => node.textContent,
  );
  return counts.length === 1 ? counts[0] : `${counts.length} counts`;
}

function renderDiff(bridge, review) {
  const feature = createDiffFeature();
  const route = { kind: "review", item: reviewItem(review), panel: "diff" };
  function Harness() {
    const [inlineAnchor, setInlineAnchor] = React.useState(null);
    const queries = React.useMemo(() => new QueryCoordinator(bridge), []);
    return feature.render(
      {
        bridge,
        queries,
        repositories: [
          { handle: "repo", display_name: "example/repo", forge_type: "gitlab" },
        ],
        repositoriesReady: true,
        repositoryGeneration: 1,
        reviewPanels: [{ id: "diff", label: "Files changed", order: 20 }],
        inlineAnchor,
        selectInlineAnchor: setInlineAnchor,
        navigate: () => {},
      },
      route,
    );
  }
  return render(React.createElement(Harness));
}

function diffBridge(review, changes = {}) {
  return {
    cancelRead: async () => true,
    openExternal: async () => true,
    openDiff: () => read(diffPage(review)),
    pageDiff: () => {
      throw new Error("the fixture diff has one page");
    },
    getReviewMutationCapabilities: () =>
      read({ review, capabilities: capabilities() }),
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [] }),
    ...changes,
  };
}

function read(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}

function capabilities() {
  return {
    general_comment: true,
    inline_comment: true,
    multiline_comment: true,
    reply: true,
    resolve: true,
    approve: true,
    request_changes: true,
    comment_verdict: true,
    atomic_review_batch: false,
  };
}

function thread(id, { line = 11, resolved = false } = {}) {
  return {
    id,
    is_inline: true,
    is_resolved: resolved,
    resolvable: true,
    root_comment: {
      id: `${id}-root`,
      author: { username: "reviewer", display_name: "" },
      body: "Looks good.",
      created_at: "2026-09-09T00:00:00Z",
      file_path: "src/calc.py",
      old_line: null,
      new_line: line,
      is_resolved: resolved,
      replies: [],
    },
  };
}

function reviewItem(handle) {
  return {
    handle,
    repository: "repo",
    summary: {
      number: 46,
      title: "Guard division",
      author: { username: "author", display_name: "Author" },
      state: "open",
      is_draft: false,
      source_branch: "feature",
      target_branch: "main",
      ci_status: "success",
      created_at: "2026-09-09T00:00:00Z",
      updated_at: "2026-09-09T00:00:00Z",
      web_url: "https://gitlab.com/example/repo/-/merge_requests/46",
      comment_count: THREAD_COUNT,
      has_conflicts: false,
      labels: [],
      review_decision: null,
      additions: 1,
      deletions: 1,
    },
  };
}

function diffPage(review) {
  return {
    snapshot_id: "snapshot-unified",
    resource: review,
    revision: REVISION,
    cursor: 0,
    next_cursor: null,
    entries: [
      {
        kind: "file",
        file_index: 0,
        old_path: "src/calc.py",
        new_path: "src/calc.py",
        status: "modified",
        additions: 1,
        deletions: 1,
        is_binary: false,
        language: "python",
        is_truncated: false,
        is_empty: false,
        is_mode_only: false,
        is_rename_only: false,
        is_unavailable: false,
      },
      {
        kind: "hunk",
        file_index: 0,
        hunk_index: 0,
        header: "@@ -10,2 +10,2 @@",
        old_start: 10,
        old_count: 2,
        new_start: 10,
        new_count: 2,
        context_text: "def divide(a, b):",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: 10,
        new_line: 10,
        content: "def divide(a, b):",
        line_type: "context",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: null,
        new_line: 11,
        content: "    if b == 0:",
        line_type: "addition",
      },
    ],
  };
}
