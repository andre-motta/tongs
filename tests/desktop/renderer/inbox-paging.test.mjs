import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import {
  createInboxFeature,
  inboxFeedPresentation,
  listDiscoveredReviews,
  nextRepositoryToLoad,
  orderedReviewItems,
  orderWatermark,
  resetInboxListSelections,
} from "../../../desktop/dist/src/renderer/features/inbox/index.js";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const { JSDOM } = desktopRequire("jsdom");
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "tongs://app/index.html",
});
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
});
const { cleanup, fireEvent, render, waitFor } = desktopRequire(
  "@testing-library/react",
);
afterEach(() => {
  cleanup();
  resetInboxListSelections();
});

test("the watermark is the newest frontier among repositories with more pages", () => {
  const feeds = [
    feed("repo-a", ["A12", "A11", "A10"], "cursor-a"),
    feed("repo-b", ["B1130", "B09"], null),
    feed("repo-c", ["C1005", "C08"], "cursor-c"),
  ];
  assert.equal(new Date(orderWatermark(feeds)).toISOString(), at("A10"));
  assert.deepEqual(titles(orderedReviewItems(feeds)), [
    "A12",
    "B1130",
    "A11",
    "C1005",
    "A10",
  ]);
  assert.equal(nextRepositoryToLoad(feeds).repository, "repo-a");

  const complete = feeds.map((item) => ({ ...item, cursor: null }));
  assert.equal(orderWatermark(complete), null);
  assert.equal(orderedReviewItems(complete).length, 7);
  assert.equal(nextRepositoryToLoad(complete), null);

  const busy = [{ ...feeds[0], loading: true }, feeds[1], feeds[2]];
  assert.equal(nextRepositoryToLoad(busy).repository, "repo-c");
});

test("other sort orders show every loaded row and still offer more", () => {
  const feeds = [
    feed("repo-a", ["A12", "A10"], "cursor-a"),
    feed("repo-b", ["B09", "B08"], null),
  ];
  const byTitle = inboxFeedPresentation(feeds, "title");
  assert.deepEqual(titles(byTitle.items), ["A10", "A12", "B08", "B09"]);
  assert.equal(byTitle.next, "repo-a");
  assert.equal(byTitle.empty, false);
  const byTime = inboxFeedPresentation(feeds, "updated");
  assert.deepEqual(titles(byTime.items), ["A12", "A10"]);
});

test("3,500 loaded rows merge and order without dropping any", () => {
  const titlesA = Array.from({ length: 3_500 }, (_, index) => minuteTitle("A", index * 2));
  const titlesB = Array.from({ length: 40 }, (_, index) => minuteTitle("B", index * 2 + 1));
  const feeds = [feed("repo-a", titlesA, null), feed("repo-b", titlesB, null)];
  const shown = orderedReviewItems(feeds);
  assert.equal(shown.length, 3_540);
  assert.equal(shown[0].summary.title, "A-0");
  assert.equal(shown[1].summary.title, "B-1");
  assert.equal(shown.at(-1).summary.title, "A-6998");
});

test("each repository's first page renders as it arrives", async () => {
  const bridge = pagingBridge();
  const view = render(
    createInboxFeature().render(
      featureContext(bridge.bridge, [repository("repo-a"), repository("repo-b")]),
      { kind: "inbox" },
    ),
  );
  await waitFor(() => assert.equal(bridge.pending.size, 2));
  bridge.respond("repo-b", page(["B1130", "B09"], null));
  await view.findByText("B1130");
  assert.deepEqual(cardTitles(view), ["B1130", "B09"]);
  assert.equal(
    view.getByText("Loading more from 1 repository").textContent,
    "Loading more from 1 repository",
  );

  bridge.respond("repo-a", page(["A12", "A11", "A10"], "cursor-a"));
  await view.findByText("A12");
  // B09 is older than repo-a's frontier and waits for repo-a's next page.
  assert.deepEqual(cardTitles(view), ["A12", "B1130", "A11", "A10"]);
  assert.equal(view.queryAllByText(/Loading more from/).length, 0);
  assert.equal(view.getAllByRole("button", { name: "Load more" }).length, 1);
});

test("load more reads the next page of the newest frontier and merges by time", async () => {
  const bridge = pagingBridge();
  const view = render(
    createInboxFeature().render(
      featureContext(bridge.bridge, [
        repository("repo-a"),
        repository("repo-b"),
        repository("repo-c"),
      ]),
      { kind: "inbox" },
    ),
  );
  await waitFor(() => assert.equal(bridge.pending.size, 3));
  bridge.respond("repo-a", page(["A12", "A10"], "cursor-a1"));
  bridge.respond("repo-b", page(["B1130", "B11"], "cursor-b1"));
  bridge.respond("repo-c", page(["C0930"], null));
  await view.findByText("A12");
  assert.deepEqual(cardTitles(view), ["A12", "B1130", "B11"]);

  fireEvent.click(view.getByRole("button", { name: "Load more" }));
  await waitFor(() => assert.equal(bridge.pending.size, 1));
  assert.deepEqual(bridge.calls.at(-1), {
    scope: "all_open",
    state: "open",
    repository: "repo-b",
    cursor: "cursor-b1",
  });
  assert.equal(
    view.getByText("Loading more from 1 repository").textContent,
    "Loading more from 1 repository",
  );
  bridge.respond("repo-b", page(["B1030", "B08"], null));
  await view.findByText("B1030");
  // Only repo-a has more pages now, so its frontier sets the watermark.
  assert.deepEqual(cardTitles(view), ["A12", "B1130", "B11", "B1030", "A10"]);

  fireEvent.click(view.getByRole("button", { name: "Load more" }));
  await waitFor(() => assert.equal(bridge.pending.size, 1));
  assert.deepEqual(bridge.calls.at(-1), {
    scope: "all_open",
    state: "open",
    repository: "repo-a",
    cursor: "cursor-a1",
  });
  bridge.respond("repo-a", page(["A0945"], null));
  await view.findByText("A0945");
  assert.deepEqual(cardTitles(view), [
    "A12",
    "B1130",
    "B11",
    "B1030",
    "A10",
    "A0945",
    "C0930",
    "B08",
  ]);
  assert.equal(view.queryAllByRole("button", { name: "Load more" }).length, 0);
});

test("a failed next page keeps the loaded rows and reports the repository", async () => {
  const bridge = pagingBridge();
  const view = render(
    createInboxFeature().render(
      featureContext(bridge.bridge, [repository("repo-a")]),
      { kind: "inbox" },
    ),
  );
  await waitFor(() => assert.equal(bridge.pending.size, 1));
  bridge.respond("repo-a", page(["A12", "A11"], "cursor-a1"));
  await view.findByText("A12");
  fireEvent.click(view.getByRole("button", { name: "Load more" }));
  await waitFor(() => assert.equal(bridge.pending.size, 1));
  bridge.fail("repo-a", new Error("controlled page failure"));
  await view.findByText("1 repository read failed. Available reviews are shown below.");
  assert.deepEqual(cardTitles(view), ["A12", "A11"]);
  // The cursor is kept, so the page can be retried.
  assert.equal(view.getAllByRole("button", { name: "Load more" }).length, 1);
});

test("a single repository tab pages the same way", async () => {
  const bridge = pagingBridge();
  const view = render(
    createInboxFeature().render(featureContext(bridge.bridge, []), {
      kind: "inbox",
      repository: repository("repo-a"),
    }),
  );
  await waitFor(() => assert.equal(bridge.pending.size, 1));
  assert.deepEqual(bridge.calls, [
    { scope: "all_open", state: "open", repository: "repo-a" },
  ]);
  bridge.respond("repo-a", page(["A12", "A11"], "cursor-a1"));
  await view.findByText("A12");
  const loadMore = view.getByRole("button", { name: "Load more" });
  loadMore.focus();
  assert.equal(document.activeElement?.textContent, "Load more");
  fireEvent.click(loadMore);
  await waitFor(() => assert.equal(bridge.pending.size, 1));
  assert.equal(bridge.calls.at(-1).cursor, "cursor-a1");
  bridge.respond("repo-a", page(["A10"], null));
  await view.findByText("A10");
  assert.deepEqual(cardTitles(view), ["A12", "A11", "A10"]);
  assert.equal(view.queryAllByRole("button", { name: "Load more" }).length, 0);
});

test("reaching the end of the list loads the next page in update-time order", async () => {
  const observed = [];
  globalThis.IntersectionObserver = class {
    constructor(callback) {
      this.callback = callback;
    }
    observe() {
      observed.push("observe");
      queueMicrotask(() => this.callback([{ isIntersecting: true }]));
    }
    disconnect() {}
  };
  try {
    const bridge = pagingBridge();
    const view = render(
      createInboxFeature().render(
        featureContext(bridge.bridge, [repository("repo-a"), repository("repo-b")]),
        { kind: "inbox" },
      ),
    );
    await waitFor(() => assert.equal(bridge.pending.size, 2));
    bridge.respond("repo-a", page(["A12", "A10"], "cursor-a1"));
    bridge.respond("repo-b", page(["B11", "B09"], "cursor-b1"));
    // repo-a's frontier (10:00) is newer than repo-b's (09:00).
    await waitFor(() => assert.equal(bridge.calls.length, 3));
    assert.equal(bridge.calls[2].repository, "repo-a");
    assert.equal(bridge.calls[2].cursor, "cursor-a1");
    bridge.respond("repo-a", page(["A0930"], null));
    await waitFor(() => assert.equal(bridge.calls.length, 4));
    assert.equal(bridge.calls[3].repository, "repo-b");
    assert.equal(bridge.calls[3].cursor, "cursor-b1");
    bridge.respond("repo-b", page(["B08"], null));
    await view.findByText("B08");
    assert.deepEqual(cardTitles(view), ["A12", "B11", "A10", "A0930", "B09", "B08"]);
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(bridge.calls.length, 4);
    assert.ok(observed.length >= 1);
  } finally {
    delete globalThis.IntersectionObserver;
  }
});

test("a failed page is not read again until asked", async () => {
  globalThis.IntersectionObserver = class {
    constructor(callback) {
      this.callback = callback;
    }
    observe() {
      queueMicrotask(() => this.callback([{ isIntersecting: true }]));
    }
    disconnect() {}
  };
  try {
    const bridge = pagingBridge();
    const view = render(
      createInboxFeature().render(
        featureContext(bridge.bridge, [repository("repo-a")]),
        { kind: "inbox" },
      ),
    );
    await waitFor(() => assert.equal(bridge.pending.size, 1));
    bridge.respond("repo-a", page(["A12"], "cursor-a1"));
    await waitFor(() => assert.equal(bridge.calls.length, 2));
    bridge.fail(
      "repo-a",
      Object.assign(new Error("The review list cursor is invalid."), {
        code: "invalid_params",
        retryable: false,
      }),
    );
    await view.findByText("1 repository read failed. Available reviews are shown below.");
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(bridge.calls.length, 2);
    fireEvent.click(view.getByRole("button", { name: "Load more" }));
    await waitFor(() => assert.equal(bridge.calls.length, 3));
    assert.equal(bridge.calls[2].cursor, "cursor-a1");
  } finally {
    delete globalThis.IntersectionObserver;
  }
});

test("the combined read reports each repository's cursor", async () => {
  const bridge = pagingBridge();
  const arrivals = [];
  const combined = listDiscoveredReviews(
    bridge.bridge,
    [{ handle: "repo-a" }, { handle: "repo-b" }],
    "all_open",
    "open",
    undefined,
    (index, arrived) => arrivals.push([index, arrived.repository, arrived.cursor]),
  );
  await waitFor(() => assert.equal(bridge.pending.size, 2));
  bridge.respond("repo-b", page(["B1"], null));
  bridge.respond("repo-a", page(["A1"], "cursor-a"));
  const result = await combined.result;
  assert.deepEqual(arrivals, [
    [1, "repo-b", null],
    [0, "repo-a", "cursor-a"],
  ]);
  assert.deepEqual(
    result.feeds.map((item) => [item.repository, item.cursor, item.loading]),
    [
      ["repo-a", "cursor-a", false],
      ["repo-b", null, false],
    ],
  );
  assert.equal(result.items.length, 2);
});

// Titles name their update time: "A1130" is repo A at 11:30, "A12" at 12:00.
function at(title) {
  if (title.includes("-")) {
    const minutes = Number(title.split("-")[1]);
    return new Date(Date.UTC(2026, 8, 20) - minutes * 60_000).toISOString();
  }
  const digits = title.slice(1);
  const hour = digits.slice(0, 2);
  const minute = digits.length > 2 ? digits.slice(2) : "00";
  return `2026-09-08T${hour}:${minute}:00.000Z`;
}

function minuteTitle(prefix, minutes) {
  return `${prefix}-${minutes}`;
}

function reviewItem(title) {
  return {
    handle: `review-${title}`,
    repository: `repo-${title[0].toLowerCase()}`,
    summary: {
      number: 1,
      title,
      author: { username: "author", display_name: "Author" },
      state: "open",
      is_draft: false,
      source_branch: "feature",
      target_branch: "main",
      ci_status: "success",
      created_at: "2026-09-01T00:00:00Z",
      updated_at: at(title),
      web_url: "https://example.invalid/review/1",
      comment_count: 0,
      has_conflicts: false,
      labels: [],
      review_decision: null,
      additions: 1,
      deletions: 0,
    },
  };
}

function feed(repositoryHandle, itemTitles, cursor) {
  return {
    repository: repositoryHandle,
    items: itemTitles.map(reviewItem),
    cursor,
    loading: false,
    failures: [],
  };
}

function page(itemTitles, nextCursor) {
  return { items: itemTitles.map(reviewItem), failures: [], next_cursor: nextCursor };
}

function titles(items) {
  return items.map((item) => item.summary.title);
}

function cardTitles(view) {
  return [...view.container.querySelectorAll(".review-card .review-title")].map(
    (node) => node.textContent,
  );
}

function pagingBridge() {
  const state = { calls: [], pending: new Map() };
  state.bridge = {
    cancelRead: async () => true,
    listReviews: (params) => {
      state.calls.push(params);
      let settle;
      const result = new Promise((resolve, reject) => {
        settle = { resolve, reject };
      });
      state.pending.set(params.repository, settle);
      return { requestToken: `token-${state.calls.length}`, result };
    },
  };
  state.respond = (handle, value) => {
    const settle = state.pending.get(handle);
    state.pending.delete(handle);
    settle.resolve(value);
  };
  state.fail = (handle, error) => {
    const settle = state.pending.get(handle);
    state.pending.delete(handle);
    settle.reject(error);
  };
  return state;
}

function repository(handle) {
  return { handle, display_name: handle, forge_type: "github" };
}

function featureContext(bridge, repositories) {
  return {
    bridge,
    queries: new QueryCoordinator(bridge),
    repositories,
    repositoriesReady: true,
    repositoryGeneration: 1,
    reviewPanels: [],
    inlineAnchor: null,
    selectInlineAnchor: () => {},
    navigate: () => {},
  };
}
