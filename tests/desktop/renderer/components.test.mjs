import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import test, { afterEach } from "node:test";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { encodeReadFailure } from "../../../desktop/dist/src/shared/bridge.js";
import { createDiffFeature } from "../../../desktop/dist/src/renderer/features/diff/index.js";
import { createInboxFeature } from "../../../desktop/dist/src/renderer/features/inbox/index.js";
import {
  createCommitsFeature,
  createReviewOverviewFeature,
} from "../../../desktop/dist/src/renderer/features/review-detail/index.js";

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
const React = desktopRequire("react");
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
      view.container.querySelector(
        '.diff-split .split-cell[data-anchor-side="new"] .line-content',
      )?.textContent,
      "+from typing import TYPE_CHECKING",
    ),
  );
});

test("every sandbox diff shape renders from the captured wire page", async () => {
  const fixture = JSON.parse(
    readFileSync(
      path.join(import.meta.dirname, "../fixtures/diff-shapes.json"),
      "utf8",
    ),
  );
  const paths = [
    "assets/icon.bin",
    "docs/guide.md",
    "src/calc.py",
    "src/empty_placeholder.py",
    "src/no_newline.txt",
    "src/obsolete.py",
    "src/renamed_module.py",
    "src/tool.sh",
  ];
  const expected = {
    // GitLab reports binary, emptiness and modes, so each shape keeps its badge.
    gitlab_unified_page: {
      "assets/icon.bin": "Binary",
      "src/empty_placeholder.py": "Empty",
      "src/renamed_module.py": "Empty",
      "src/tool.sh": "Mode only",
    },
    // The GitHub files endpoint withholds that metadata, so the four shapes
    // arrive as content the forge did not supply.
    github_unified_page: {
      "assets/icon.bin": "Unavailable",
      "src/empty_placeholder.py": "Unavailable",
      "src/renamed_module.py": "Unavailable",
      "src/tool.sh": "Unavailable",
    },
  };
  for (const [key, badges] of Object.entries(expected)) {
    const bridge = baseBridge({
      openDiff: () => read(fixture[key]),
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
    const files = () => [
      ...view.container.querySelectorAll(
        ".file-list .file-item:not(.hunk-item)",
      ),
    ];
    await waitFor(() => assert.equal(files().length, 8));
    assert.deepEqual(
      files().map((node) => node.querySelector("span")?.textContent),
      paths,
      key,
    );
    assert.equal(view.container.querySelector(".notice-error"), null, key);
    for (const [file, badge] of Object.entries(badges)) {
      fireEvent.click(
        files().find((node) => node.textContent?.startsWith(file)),
      );
      const heading =
        file === "src/renamed_module.py"
          ? "src/legacy_name.py \u2192 src/renamed_module.py"
          : file;
      await waitFor(() => {
        const header = view.container.querySelector(".diff-file");
        assert.equal(header?.querySelector(".file-path")?.textContent, heading);
        assert.deepEqual(
          [...header.querySelectorAll(".badge")].map((node) => node.textContent),
          [badge],
          `${key} ${file}`,
        );
      });
    }
    view.unmount();
  }
});

test("diff anchor selection keeps full immutable identity across layouts", async () => {
  const bridge = baseBridge({
    openDiff: (params) => read(diffPage(params.layout)),
    pageDiff: () => {
      throw new Error("no next page");
    },
  });
  const feature = createDiffFeature();
  const route = {
    kind: "review",
    item: reviewItem(),
    panel: "diff",
  };
  let observed = null;
  function Harness() {
    const [inlineAnchor, setInlineAnchor] = React.useState(null);
    const selectInlineAnchor = (next) => {
      observed = next;
      setInlineAnchor(next);
    };
    return feature.render(
      {
        ...featureContext(bridge),
        inlineAnchor,
        selectInlineAnchor,
      },
      route,
    );
  }
  const view = render(React.createElement(Harness));
  await waitFor(() =>
    assert.ok(
      view.container.querySelector(
        '.diff-unified .line-content[role="button"]',
      ),
    ),
  );
  fireEvent.keyDown(
    view.container.querySelector('.diff-unified .line-content[role="button"]'),
    { key: "Enter" },
  );
  await waitFor(() => assert.equal(observed?.side, "new"));
  assert.deepEqual(observed, {
    review: "review-69",
    snapshotId: "snapshot-unified",
    resource: "review-69",
    revision: {
      head_sha: "af469e5b5209803aef09943d989b77b9c7934cdb",
      base_sha: "df2bd3f",
      start_sha: null,
    },
    fileIndex: 0,
    hunkIndex: 0,
    rowIndex: null,
    oldPath: "src/tongs/services/__init__.py",
    newPath: "src/tongs/services/__init__.py",
    side: "new",
    oldLine: null,
    newLine: 1,
    lineType: "addition",
    contextLines: ["from typing import TYPE_CHECKING"],
    contextComplete: true,
    rangeOriginOldLine: null,
    rangeOriginNewLine: 1,
    selectedLines: [
      {
        oldLine: null,
        newLine: 1,
        lineType: "addition",
        content: "from typing import TYPE_CHECKING",
      },
    ],
  });
  fireEvent.click(view.getByText("Split"));
  await waitFor(() =>
    assert.equal(
      view.container
        .querySelector('.diff-split .split-cell[data-anchor-side="new"]')
        ?.getAttribute("aria-pressed"),
      "true",
    ),
  );
  assert.equal(observed.snapshotId, "snapshot-split");
  assert.equal(
    view.container.querySelector(".line-no_newline[tabindex]"),
    null,
  );
});

test("Shift+Enter extends a keyboard diff selection across new-side source lines", async () => {
  const bridge = baseBridge({
    openDiff: () => read(rangeDiffPage()),
    pageDiff: () => {
      throw new Error("no next page");
    },
  });
  const feature = createDiffFeature();
  let observed = null;
  function Harness() {
    const [inlineAnchor, setInlineAnchor] = React.useState(null);
    const selectInlineAnchor = (next) => {
      observed = next;
      setInlineAnchor(next);
    };
    return feature.render(
      {
        ...featureContext(bridge),
        inlineAnchor,
        selectInlineAnchor,
      },
      { kind: "review", item: reviewItem(), panel: "diff" },
    );
  }
  const view = render(React.createElement(Harness));
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll('.diff-unified .line-content[role="button"]')
        .length,
      3,
    ),
  );
  const lines = view.container.querySelectorAll(
    '.diff-unified .line-content[role="button"]',
  );
  fireEvent.keyDown(lines[0], { key: "Enter" });
  fireEvent.keyDown(lines[2], { key: "Enter", shiftKey: true });
  await waitFor(() => assert.equal(observed?.selectedLines.length, 3));
  assert.deepEqual(
    observed.selectedLines.map((line) => [line.newLine, line.content]),
    [[1, "first"], [2, "second"], [3, "third"]],
  );
});

// The main process rejects a read, and Electron rebuilds that rejection as a
// fresh Error carrying only the message, so the typed object below is the shape
// a renderer-raised failure has and the wrapped Error is the shape every failure
// from main actually has.
const REJECTION_SHAPES = [
  ["typed failure", (failure) => failure],
  [
    "failure from the main process",
    (failure) =>
      new Error(
        `Error invoking remote method 'tongs:diff.open': Error: ${encodeReadFailure(failure).message}`,
      ),
  ],
];

for (const code of ["snapshot_expired", "revision_changed"]) {
  for (const [shape, rejection] of REJECTION_SHAPES) {
  test(`diff ${code} ${shape} clears the app-owned anchor`, async () => {
    let openReads = 0;
    const observed = [];
    const bridge = baseBridge({
      openDiff: () =>
        read(
          openReads++ === 0
            ? diffPage("unified")
            : Promise.reject(
                rejection({
                  code,
                  message: "safe service failure",
                  retryable: true,
                }),
              ),
        ),
    });
    const feature = createDiffFeature();
    const context = featureContext(bridge);
    function Harness() {
      const [inlineAnchor, setInlineAnchor] = React.useState(null);
      const selectInlineAnchor = React.useCallback((next) => {
        observed.push(next);
        setInlineAnchor(next);
      }, []);
      return feature.render(
        {
          ...context,
          inlineAnchor,
          selectInlineAnchor,
        },
        {
          kind: "review",
          item: reviewItem(),
          panel: "diff",
        },
      );
    }
    const view = render(React.createElement(Harness));
    const line = await waitFor(() => {
      const candidate = view.container.querySelector(
        '.diff-unified .line-content[role="button"]',
      );
      assert.ok(candidate);
      return candidate;
    });
    fireEvent.click(line);
    await waitFor(() => assert.equal(observed.at(-1)?.side, "new"));
    fireEvent.click(view.getByText("Refresh"));

    await view.findByRole("alert");
    assert.equal(openReads, 2);
    assert.equal(observed.at(-1), null);
  });
  }
}

test("overview and commits expose refresh and retry after retained failures", async () => {
  let reviewReads = 0;
  let commitReads = 0;
  const bridge = baseBridge({
    getReview: () =>
      read(
        reviewReads++ === 0
          ? reviewSnapshot()
          : Promise.reject(new Error("refresh failed")),
      ),
    listCommits: () =>
      read(
        commitReads++ === 0
          ? { commits: [commit()] }
          : Promise.reject(new Error("refresh failed")),
      ),
  });
  const overview = createReviewOverviewFeature();
  const overviewView = render(
    overview.render(featureContext(bridge), {
      kind: "review",
      item: reviewItem(),
      panel: "overview",
    }),
  );
  await overviewView.findByText("Description");
  fireEvent.click(overviewView.getByText("Refresh review details"));
  await overviewView.findByText(
    "Refresh failed. Showing the previous review details.",
  );
  assert.ok(overviewView.getByText("Refresh again"));
  cleanup();
  const commits = createCommitsFeature();
  const commitView = render(
    commits.render(featureContext(bridge), {
      kind: "review",
      item: reviewItem(),
      panel: "commits",
    }),
  );
  await commitView.findByText("First commit");
  fireEvent.click(commitView.getByText("Refresh commits"));
  await commitView.findByText("Refresh failed. Showing previous commits.");
  assert.ok(commitView.getByText("Refresh again"));
});

test("overview renders its real description through restricted Markdown", async () => {
  const opened = [];
  const bridge = baseBridge({
    getReview: () =>
      read({
        ...reviewSnapshot(),
        detail: {
          ...reviewSnapshot().detail,
          description:
            "## Rendered description\n\n| State | Value |\n| - | - |\n| Safe | **yes** |\n\n[Docs](HTTPS://Example.COM/docs) ![remote](https://bad.invalid/x.png)",
        },
      }),
    openExternal: async (url) => {
      opened.push(url);
      return true;
    },
  });
  const overview = createReviewOverviewFeature();
  const view = render(
    overview.render(featureContext(bridge), {
      kind: "review",
      item: reviewItem(),
      panel: "overview",
    }),
  );

  assert.equal(
    (await view.findByRole("heading", { level: 2, name: "Rendered description" }))
      .textContent,
    "Rendered description",
  );
  assert.equal(view.container.querySelector("table strong")?.textContent, "yes");
  assert.equal(view.container.querySelector("img"), null);
  assert.match(view.container.textContent, /\[Image: remote\]/);
  assert.deepEqual(opened, []);
  fireEvent.click(view.getByRole("link", { name: "Docs" }));
  await waitFor(() => assert.deepEqual(opened, ["https://example.com/docs"]));
});

function featureContext(bridge, navigate = () => {}) {
  return {
    bridge,
    queries: new QueryCoordinator(bridge),
    repositories: [repository()],
    repositoriesReady: true,
    repositoryGeneration: 1,
    reviewPanels: [
      { id: "overview", label: "Overview", order: 10 },
      { id: "diff", label: "Files changed", order: 20 },
      { id: "commits", label: "Commits", order: 30 },
    ],
    inlineAnchor: null,
    selectInlineAnchor: () => {},
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
  const marker =
    layout === "split"
      ? {
          kind: "split",
          file_index: 0,
          hunk_index: 0,
          row_index: 1,
          old: {
            old_line: null,
            new_line: null,
            content: "No newline at end of file",
            line_type: "no_newline",
            anchor_side: null,
          },
          new: null,
        }
      : {
          kind: "line",
          file_index: 0,
          hunk_index: 0,
          old_line: null,
          new_line: null,
          content: "No newline at end of file",
          line_type: "no_newline",
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
    entries: [file, hunk, row, marker],
  };
}

function rangeDiffPage() {
  const page = diffPage("unified");
  return {
    ...page,
    snapshot_id: "snapshot-range",
    entries: [
      page.entries[0],
      page.entries[1],
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: 1,
        new_line: 1,
        content: "first",
        line_type: "context",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: null,
        new_line: 2,
        content: "second",
        line_type: "addition",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: 2,
        new_line: 3,
        content: "third",
        line_type: "context",
      },
    ],
  };
}

function reviewSnapshot() {
  return {
    review: "review-69",
    repository: "repo",
    detail: {
      ...reviewItem().summary,
      description: "A real description",
      merge_status: "merged",
    },
    revision: {
      head_sha: "af469e5b5209803aef09943d989b77b9c7934cdb",
      base_sha: "df2bd3f",
      start_sha: null,
    },
    revision_error: null,
    capabilities: {
      batched_review: true,
      thread_resolution: true,
      draft_notes: false,
      unapprove: false,
      job_cancel: true,
    },
  };
}

function commit() {
  return {
    sha: "abc",
    short_sha: "abc",
    title: "First commit",
    message: "First commit",
    author: { username: "andre", display_name: "Andre" },
    created_at: "2026-09-07T00:00:00Z",
    web_url: "https://example.invalid/commit/abc",
  };
}

function baseBridge(overrides) {
  return {
    cancelRead: async () => true,
    openExternal: async () => true,
    // The diff surface reads inline comment support to decide what its gutter
    // composer may offer, so every diff bridge answers that read.
    getReviewMutationCapabilities: (review) =>
      read({
        review,
        capabilities: {
          general_comment: true,
          inline_comment: true,
          multiline_comment: true,
          reply: true,
          resolve: true,
          approve: true,
          request_changes: true,
          comment_verdict: true,
          atomic_review_batch: false,
        },
      }),
    ...overrides,
  };
}

function read(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}
