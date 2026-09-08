import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { createDiffFeature } from "../../../desktop/dist/src/renderer/features/diff/index.js";
import { createInboxFeature } from "../../../desktop/dist/src/renderer/features/inbox/index.js";

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
const { cleanup, fireEvent, render, waitFor } = desktopRequire(
  "@testing-library/react",
);
afterEach(cleanup);

test("inbox component renders partial real-shape results and routes the selected review", async () => {
  const navigated = [];
  const item = reviewItem();
  const bridge = baseBridge({
    listReviews: () =>
      read({
        items: [item],
        failures: [
          {
            code: "network",
            message: "redacted",
            retryable: true,
            repository: null,
          },
        ],
      }),
  });
  const feature = createInboxFeature();
  const context = featureContext(bridge, (route) => navigated.push(route));
  const view = render(
    feature.render(context, { kind: "inbox", repository: repository() }),
  );
  await view.findByText("feat: add durable review submission");
  assert.equal(
    view.getByRole("alert").textContent,
    "1 repository read failed. Available reviews are shown below.",
  );
  fireEvent.click(view.getByRole("listitem"));
  assert.equal(navigated[0].kind, "review");
  assert.equal(navigated[0].item.handle, "review-69");
});

test("initial All reviews waits for local discovery before scoped reads", async () => {
  let reads = 0;
  const bridge = baseBridge({
    listReviews: () => {
      reads += 1;
      return read({ items: [], failures: [] });
    },
  });
  const feature = createInboxFeature();
  const waiting = {
    ...featureContext(bridge),
    repositories: [],
    repositoriesReady: false,
    repositoryGeneration: 0,
  };
  const view = render(
    feature.render(waiting, { kind: "inbox", repository: null }),
  );
  assert.equal(reads, 0);
  assert.ok(view.getByText("Waiting for local repository discovery…"));
  const ready = {
    ...waiting,
    repositories: [repository()],
    repositoriesReady: true,
    repositoryGeneration: 1,
  };
  view.rerender(feature.render(ready, { kind: "inbox", repository: null }));
  await waitFor(() => assert.equal(reads, 1));
});

test("diff component changes layout through the typed Python projection request", async () => {
  const layouts = [];
  const bridge = baseBridge({
    openDiff: (params) => {
      layouts.push(params.layout);
      return read(diffPage(params.layout));
    },
    pageDiff: () => {
      throw new Error("no next page");
    },
  });
  const feature = createDiffFeature();
  const view = render(
    feature.render(featureContext(bridge), {
      kind: "review",
      item: reviewItem(),
      panel: "diff",
    }),
  );
  await waitFor(() =>
    assert.equal(
      view.container.querySelector(".diff-unified .line-content")?.textContent,
      "+from typing import TYPE_CHECKING",
    ),
  );
  assert.deepEqual(layouts, ["unified"]);
  fireEvent.click(view.getByText("Split"));
  await waitFor(() => assert.deepEqual(layouts, ["unified", "split"]));
  await waitFor(() =>
    assert.equal(
      view.container.querySelector(".diff-split .line-content")?.textContent,
      "+from typing import TYPE_CHECKING",
    ),
  );
});

function featureContext(bridge, navigate = () => {}) {
  return {
    bridge,
    queries: new QueryCoordinator(bridge),
    repositories: [repository()],
    repositoriesReady: true,
    repositoryGeneration: 1,
    navigate,
  };
}

function repository() {
  return {
    handle: "repo",
    display_name: "andre-motta/tongs",
    forge_type: "github",
  };
}

function reviewItem() {
  return {
    handle: "review-69",
    repository: "repo",
    summary: {
      number: 69,
      title: "feat: add durable review submission",
      author: { username: "andre-motta", display_name: "Andre" },
      state: "merged",
      is_draft: false,
      source_branch: "feat/review",
      target_branch: "feat/desktop-app",
      ci_status: "success",
      created_at: "2026-09-07T00:00:00Z",
      updated_at: "2026-09-07T00:00:00Z",
      web_url: "https://github.com/andre-motta/tongs/pull/69",
      comment_count: 0,
      has_conflicts: false,
      labels: [],
      review_decision: null,
      additions: 57,
      deletions: 1,
    },
  };
}

function diffPage(layout) {
  const file = {
    kind: "file",
    file_index: 0,
    old_path: "src/tongs/services/__init__.py",
    new_path: "src/tongs/services/__init__.py",
    status: "modified",
    additions: 1,
    deletions: 0,
    is_binary: false,
    language: "python",
    is_truncated: false,
    is_empty: false,
    is_mode_only: false,
    is_unavailable: false,
  };
  const hunk = {
    kind: "hunk",
    file_index: 0,
    hunk_index: 0,
    header: "@@ -1 +1,2 @@",
    old_start: 1,
    old_count: 1,
    new_start: 1,
    new_count: 2,
    context_text: "",
  };
  const row =
    layout === "split"
      ? {
          kind: "split",
          file_index: 0,
          hunk_index: 0,
          row_index: 0,
          old: null,
          new: {
            old_line: null,
            new_line: 1,
            content: "from typing import TYPE_CHECKING",
            line_type: "addition",
            anchor_side: "new",
          },
        }
      : {
          kind: "line",
          file_index: 0,
          hunk_index: 0,
          old_line: null,
          new_line: 1,
          content: "from typing import TYPE_CHECKING",
          line_type: "addition",
        };
  return {
    snapshot_id: `snapshot-${layout}`,
    resource: "review-69",
    revision: {
      head_sha: "af469e5b5209803aef09943d989b77b9c7934cdb",
      base_sha: "df2bd3f",
      start_sha: null,
    },
    cursor: 0,
    next_cursor: null,
    entries: [file, hunk, row],
  };
}

function baseBridge(overrides) {
  return {
    cancelRead: async () => true,
    openExternal: async () => true,
    ...overrides,
  };
}

function read(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}
