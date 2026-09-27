import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import {
  createInboxFeature,
  DISCOVERED_REVIEW_CONCURRENCY,
  listDiscoveredReviews,
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
const { cleanup, render, waitFor } = desktopRequire("@testing-library/react");
afterEach(() => {
  cleanup();
  resetInboxListSelections();
});

// The main process refuses a read once this many requests are pending.
const MAIN_PENDING_LIMIT = 64;

test("All reviews loads every repository when there are more than the pending limit", async () => {
  const bridge = limitedBridge((params) => ({
    items: [reviewItem(`review-${params.repository}`)],
    failures: [],
  }));
  const repositories = handles(100).map((handle) => repository(handle));
  const view = render(
    createInboxFeature().render(featureContext(bridge.bridge, repositories), {
      kind: "inbox",
    }),
  );

  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".review-card").length, 100),
  );
  assert.equal(bridge.calls.length, 100);
  assert.equal(bridge.refused, 0);
  assert.ok(bridge.peak <= DISCOVERED_REVIEW_CONCURRENCY);
  assert.equal(DISCOVERED_REVIEW_CONCURRENCY, 8);
  assert.equal(view.queryAllByText(/repository read/).length, 0);
  assert.equal(view.queryAllByText("Too many desktop requests are pending.").length, 0);
  assert.equal(view.getByText("review-repo-99").textContent, "review-repo-99");
});

test("one failing repository is reported while the others still render", async () => {
  const bridge = limitedBridge((params) => {
    if (params.repository === "repo-2")
      throw new Error("controlled repository failure");
    return {
      items: [reviewItem(`review-${params.repository}`)],
      failures: [],
    };
  });
  const repositories = handles(5).map((handle) => repository(handle));
  const view = render(
    createInboxFeature().render(featureContext(bridge.bridge, repositories), {
      kind: "inbox",
    }),
  );

  await view.findByText("1 repository read failed. Available reviews are shown below.");
  assert.equal(view.container.querySelectorAll(".review-card").length, 4);
  assert.equal(view.queryAllByText("review-repo-2").length, 0);
  assert.equal(view.getByText("review-repo-4").textContent, "review-repo-4");
});

test("a refused or failed repository read becomes one typed failure", async () => {
  const bridge = limitedBridge((params) => {
    if (params.repository === "repo-1")
      throw new Error("controlled repository failure");
    return {
      items: [reviewItem(`review-${params.repository}`)],
      failures: [],
    };
  });
  const combined = listDiscoveredReviews(
    bridge.bridge,
    handles(3).map((handle) => ({ handle })),
    "all_open",
    "open",
  );
  const result = await combined.result;
  assert.deepEqual(
    result.items.map((item) => item.summary.title),
    ["review-repo-0", "review-repo-2"],
  );
  assert.deepEqual(result.failures, [
    {
      repository: "repo-1",
      code: "read_failed",
      message: "The local service could not complete this read.",
      retryable: true,
    },
  ]);

  const refusing = {
    cancelRead: async () => true,
    listReviews: (params) => ({
      requestToken: `token-${params.repository}`,
      result:
        params.repository === "repo-0"
          ? Promise.reject(
              Object.assign(new Error("Too many desktop requests are pending."), {
                code: "too_many_requests",
                retryable: true,
              }),
            )
          : Promise.resolve({ items: [], failures: [] }),
    }),
  };
  const refused = await listDiscoveredReviews(
    refusing,
    handles(2).map((handle) => ({ handle })),
    "my_reviews",
    "open",
  ).result;
  assert.deepEqual(
    refused.failures.map((failure) => [failure.repository, failure.code]),
    [["repo-0", "too_many_requests"]],
  );
});

test("aborting the combined read cancels in-flight reads and starts no queued read", async () => {
  const pending = new Map();
  const cancelled = [];
  const bridge = {
    cancelRead: async (token) => {
      cancelled.push(token);
      return true;
    },
    listReviews: (params) => {
      const read = deferred();
      pending.set(params.repository, read);
      return { requestToken: `token-${params.repository}`, result: read.promise };
    },
  };
  const controller = new AbortController();
  const combined = listDiscoveredReviews(
    bridge,
    handles(20).map((handle) => ({ handle })),
    "all_open",
    "open",
    controller.signal,
  );
  assert.equal(pending.size, 8);
  assert.equal(combined.requestTokens.length, 8);

  pending.get("repo-0").resolve({ items: [], failures: [] });
  await waitFor(() => assert.equal(pending.size, 9));

  controller.abort();
  await assert.rejects(combined.result, (error) => error.code === "request_cancelled");
  assert.deepEqual(
    [...cancelled].sort(),
    handles(9)
      .slice(1)
      .map((handle) => `token-${handle}`)
      .sort(),
  );
  for (const read of pending.values()) read.resolve({ items: [], failures: [] });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(pending.size, 9);
});

test("the coordinator cancels every read the combined read has started", async () => {
  const pending = new Map();
  const cancelled = [];
  const bridge = {
    cancelRead: async (token) => {
      cancelled.push(token);
      return true;
    },
    listReviews: (params) => {
      const read = deferred();
      pending.set(params.repository, read);
      return { requestToken: `token-${params.repository}`, result: read.promise };
    },
  };
  const coordinator = new QueryCoordinator(bridge);
  const running = coordinator.run("inbox:all", () =>
    listDiscoveredReviews(
      bridge,
      handles(12).map((handle) => ({ handle })),
      "all_open",
      "open",
    ),
  );
  pending.get("repo-0").resolve({ items: [], failures: [] });
  await waitFor(() => assert.equal(pending.size, 9));
  await coordinator.cancel("inbox:all");
  assert.equal(cancelled.includes("token-repo-8"), true);
  assert.equal(cancelled.length, 9);
  for (const read of pending.values()) read.resolve({ items: [], failures: [] });
  void running.catch(() => undefined);
});

test("leaving All reviews stops queued repository reads", async () => {
  const pending = new Map();
  const cancelled = [];
  const bridge = {
    cancelRead: async (token) => {
      cancelled.push(token);
      return true;
    },
    listReviews: (params) => {
      const read = deferred();
      pending.set(params.repository, read);
      return { requestToken: `token-${params.repository}`, result: read.promise };
    },
  };
  const repositories = handles(30).map((handle) => repository(handle));
  const view = render(
    createInboxFeature().render(featureContext(bridge, repositories), {
      kind: "inbox",
    }),
  );
  await waitFor(() => assert.equal(pending.size, 8));
  view.unmount();
  await waitFor(() => assert.ok(cancelled.includes("token-repo-7")));
  for (const read of pending.values()) read.resolve({ items: [], failures: [] });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(pending.size, 8);
});

function limitedBridge(respond) {
  const state = { calls: [], inFlight: 0, peak: 0, refused: 0 };
  state.bridge = {
    cancelRead: async () => true,
    listReviews: (params) => {
      state.calls.push(params);
      const requestToken = `token-${params.repository}`;
      if (state.inFlight >= MAIN_PENDING_LIMIT) {
        state.refused += 1;
        return {
          requestToken,
          result: Promise.reject(new Error("Too many desktop requests are pending.")),
        };
      }
      state.inFlight += 1;
      state.peak = Math.max(state.peak, state.inFlight);
      const result = new Promise((resolve) => setTimeout(resolve, 0)).then(() => {
        state.inFlight -= 1;
        return respond(params);
      });
      return { requestToken, result };
    },
  };
  return state;
}

function handles(count) {
  return Array.from({ length: count }, (_, index) => `repo-${index}`);
}

function repository(handle) {
  return { handle, display_name: handle, forge_type: "github" };
}

function reviewItem(title) {
  return {
    handle: title,
    repository: "repo",
    summary: {
      number: 1,
      title,
      author: { username: "author", display_name: "Author" },
      state: "open",
      is_draft: false,
      source_branch: "feature",
      target_branch: "main",
      ci_status: "success",
      created_at: "2026-09-08T00:00:00Z",
      updated_at: "2026-09-08T00:00:00Z",
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

function deferred() {
  let resolve;
  const promise = new Promise((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

test("a read cancelled from outside stops the queue and rejects as cancelled", async () => {
  const pending = new Map();
  const bridge = {
    cancelRead: async () => true,
    listReviews: (params) => {
      let settle;
      const promise = new Promise((resolve, reject) => {
        settle = { resolve, reject };
      });
      pending.set(params.repository, settle);
      return { requestToken: `token-${params.repository}`, result: promise };
    },
  };
  const combined = listDiscoveredReviews(
    bridge,
    handles(20).map((handle) => ({ handle })),
    "all_open",
    "open",
  );
  const outcome = combined.result.then(
    () => "resolved",
    (error) => error.code,
  );
  await waitFor(() => assert.equal(pending.size, DISCOVERED_REVIEW_CONCURRENCY));
  pending.get("repo-0").reject(
    Object.assign(new Error("The read was cancelled."), {
      code: "request_cancelled",
      retryable: false,
    }),
  );
  for (const [name, read] of pending) {
    if (name !== "repo-0") read.resolve({ items: [], failures: [] });
  }
  assert.equal(await outcome, "request_cancelled");
  assert.equal(pending.size, DISCOVERED_REVIEW_CONCURRENCY);
});

test("every repository failing rejects instead of showing an empty list", async () => {
  const bridge = limitedBridge(() => {
    throw new Error("controlled repository failure");
  });
  const outcome = await listDiscoveredReviews(
    bridge.bridge,
    handles(3).map((handle) => ({ handle })),
    "all_open",
    "open",
  ).result.then(
    () => "resolved",
    (error) => error.code,
  );
  assert.equal(outcome, "read_failed");
});
