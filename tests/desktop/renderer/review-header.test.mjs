import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { createDiffFeature } from "../../../desktop/dist/src/renderer/features/diff/index.js";
import { createReviewFeature } from "../../../desktop/dist/src/renderer/features/review/index.js";
import { clearPendingEdit } from "../../../desktop/dist/src/renderer/features/review/drawer.js";
import {
  ACTIONS_BLOCKED,
  ACTIONS_LOADING,
} from "../../../desktop/dist/src/renderer/features/review/header.js";
import { createReviewOverviewFeature } from "../../../desktop/dist/src/renderer/features/review-detail/index.js";

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
afterEach(() => {
  cleanup();
  clearPendingEdit();
});

const REVISION = {
  head_sha: "f8bbf4877cd1a58f2a3a9d1a1a86bd2f4b7a1c33",
  base_sha: "df2bd3f",
  start_sha: null,
};

/** The three review tabs the header must be identical on (#228). */
const TABS = ["overview", "diff", "discussions"];

const ACTION_LABELS = ["Merge", "Close", "Reopen", "Remove approval"];

const REJECTION =
  "The forge refused this action: the review changed remotely or its branch conflicts with the target. Refresh, and check for merge conflicts.";

for (const panel of TABS) {
  test(`the ${panel} tab header carries the lifecycle actions and Your review`, async () => {
    const review = `header-layout-${panel}`;
    const view = renderReview(reviewBridge(review), review, panel);
    await waitFor(() => assert.equal(yourReviewButtons(view), 1));
    await settledActions(view);
    // The header's own buttons, in order, as the reader meets them.
    assert.deepEqual(headerButtonTexts(view), [
      "← Reviews",
      ...ACTION_LABELS,
      "Your review0",
      "Open on forge",
    ]);
    // Nothing on the page outside the header offers a lifecycle action.
    assert.equal(
      [...view.container.querySelectorAll("button")].filter(
        (button) =>
          ACTION_LABELS.includes(button.textContent) &&
          button.closest(".review-header") === null,
      ).length,
      0,
    );
  });
}

for (const panel of TABS) {
  test(`the ${panel} tab enables only the actions the review permits`, async () => {
    const review = `header-permissions-${panel}`;
    const view = renderReview(
      reviewBridge(review, {
        getReviewActionCapabilities: () =>
          read({
            review,
            capabilities: {
              merge: true,
              close: false,
              reopen: false,
              unapprove: true,
            },
          }),
      }),
      review,
      panel,
    );
    await waitFor(() =>
      assert.deepEqual(actionStates(view), [
        ["Merge", false],
        ["Close", true],
        ["Reopen", true],
        ["Remove approval", false],
      ]),
    );
    assert.deepEqual(
      actionButtons(view).map((button) => button.title),
      [
        "",
        "Close is unsupported for this review.",
        "Reopen is unsupported for this review.",
        "",
      ],
    );
    // Merge options are offered only because merge is permitted.
    assert.equal(view.queryAllByLabelText("Squash commits").length, 1);
    assert.equal(
      view.queryAllByLabelText("Delete source branch after merge").length,
      1,
    );
  });
}

for (const panel of TABS) {
  test(`the ${panel} tab asks for confirmation before a lifecycle action`, async () => {
    const review = `header-confirm-${panel}`;
    const closes = [];
    const view = renderReview(
      reviewBridge(review, {
        getReviewActionCapabilities: () =>
          read({
            review,
            capabilities: {
              merge: false,
              close: true,
              reopen: false,
              unapprove: false,
            },
          }),
        closeReview: async (params) => {
          closes.push(params);
          return actionReceipt(review, params.operation_id, "close");
        },
      }),
      review,
      panel,
    );
    fireEvent.click(await enabledAction(view, "Close"));
    assert.equal(closes.length, 0);
    assert.equal(view.queryAllByRole("button", { name: "Close" }).length, 0);
    fireEvent.click(view.getByRole("button", { name: "Confirm Close" }));
    await waitFor(() => assert.equal(closes.length, 1));
    assert.equal(closes[0].review, review);
    assert.deepEqual(closes[0].revision, REVISION);
    await waitFor(() =>
      assert.equal(view.queryAllByRole("button", { name: "Close" }).length, 1),
    );
  });
}

test("merge options travel with the confirmed merge from the Overview header", async () => {
  const review = "header-merge-overview";
  const merges = [];
  const view = renderReview(
    reviewBridge(review, {
      getReviewActionCapabilities: () =>
        read({
          review,
          capabilities: { merge: true, close: false, reopen: false, unapprove: false },
        }),
      mergeReview: async (params) => {
        merges.push(params);
        return actionReceipt(review, params.operation_id, "merge");
      },
    }),
    review,
    "overview",
  );
  fireEvent.click(await enabledAction(view, "Merge"));
  // Changing an option withdraws the armed confirmation, so what is
  // confirmed is always the command on screen.
  fireEvent.click(view.getByLabelText("Squash commits"));
  assert.equal(view.queryAllByRole("button", { name: "Confirm Merge" }).length, 0);
  fireEvent.click(view.getByLabelText("Delete source branch after merge"));
  fireEvent.click(view.getByRole("button", { name: "Merge" }));
  assert.equal(merges.length, 0);
  fireEvent.click(view.getByRole("button", { name: "Confirm Merge" }));
  await waitFor(() => assert.equal(merges.length, 1));
  assert.equal(merges[0].squash, true);
  assert.deepEqual(merges[0].source_cleanup, { branch: "feature" });
});

for (const panel of TABS) {
  test(`a rejected action on the ${panel} tab is one alert in the header`, async () => {
    const review = `header-rejected-${panel}`;
    const view = renderReview(
      reviewBridge(review, {
        getReviewActionCapabilities: () =>
          read({
            review,
            capabilities: {
              merge: false,
              close: false,
              reopen: true,
              unapprove: false,
            },
          }),
        reopenReview: async () => {
          throw {
            code: "conflict",
            message: "raw backend text that must stay hidden",
            retryable: false,
          };
        },
      }),
      review,
      panel,
    );
    fireEvent.click(await enabledAction(view, "Reopen"));
    fireEvent.click(view.getByRole("button", { name: "Confirm Reopen" }));
    await waitFor(() => assert.equal(view.queryAllByText(REJECTION).length, 1));
    assert.equal(view.getAllByRole("alert").length, 1);
    assert.equal(
      view.container.querySelectorAll('.review-header [role="alert"]').length,
      1,
    );
    assert.equal(view.queryAllByText(/raw backend text/).length, 0);
  });
}

test("the actions say they are loading until the capability read answers", async () => {
  const review = "header-loading";
  const view = renderReview(
    reviewBridge(review, {
      getReviewActionCapabilities: () => read(new Promise(() => {})),
    }),
    review,
    "discussions",
  );
  await waitFor(() => assert.equal(yourReviewButtons(view), 1));
  await settle();
  assert.deepEqual(
    actionButtons(view).map((button) => [button.disabled, button.title]),
    ACTION_LABELS.map(() => [true, ACTIONS_LOADING]),
  );
});

test("a failed capability read disables the actions and says why", async () => {
  const review = "header-read-failed";
  const view = renderReview(
    reviewBridge(review, {
      getReviewActionCapabilities: () =>
        read(Promise.reject({ code: "network", message: "offline", retryable: true })),
    }),
    review,
    "overview",
  );
  await waitFor(() =>
    assert.equal(
      actionButtons(view).filter((button) =>
        button.title.startsWith("Review action support could not be read"),
      ).length,
      4,
    ),
  );
  assert.equal(
    actionButtons(view).filter((button) => button.disabled).length,
    4,
  );
});

test("an unknown action result blocks every action on every tab until it is settled", async () => {
  const review = "header-unknown";
  const view = renderReview(
    reviewBridge(review, {
      getReviewActionCapabilities: () =>
        read({
          review,
          capabilities: { merge: true, close: true, reopen: false, unapprove: false },
        }),
      closeReview: async () => {
        throw {
          code: "mutation_timeout",
          message: "The mutation response timed out.",
          retryable: true,
        };
      },
    }),
    review,
    "discussions",
  );
  fireEvent.click(await enabledAction(view, "Close"));
  fireEvent.click(view.getByRole("button", { name: "Confirm Close" }));
  await waitFor(() =>
    assert.equal(
      view.queryAllByRole("button", { name: "Check retained receipt" }).length,
      1,
    ),
  );
  // The standing belongs to the review, not the tab: Changes shows the same
  // refusal and the same way out.
  fireEvent.click(view.getByRole("button", { name: "Files changed" }));
  await waitFor(() =>
    assert.equal(
      view.queryAllByRole("button", { name: "Check retained receipt" }).length,
      1,
    ),
  );
  await waitFor(() =>
    assert.deepEqual(
      actionButtons(view).map((button) => [button.disabled, button.title]),
      [
        [true, ACTIONS_BLOCKED],
        [true, ACTIONS_BLOCKED],
        [true, "Reopen is unsupported for this review."],
        [true, "Remove approval is unsupported for this review."],
      ],
    ),
  );
  fireEvent.click(
    view.getByRole("button", {
      name: "I inspected the forge; acknowledge uncertainty",
    }),
  );
  await waitFor(() =>
    assert.deepEqual(
      actionButtons(view).map((button) => button.disabled),
      [false, false, true, true],
    ),
  );
});

const ACKNOWLEDGE = "I inspected the forge; acknowledge uncertainty";

/**
 * Sends an inline comment from Files changed whose result is unknown, which
 * refuses every further mutation on the review until it is acknowledged.
 */
async function unknownInlineComment(review) {
  const posts = [];
  const view = renderReview(
    reviewBridge(review, {
      getReviewActionCapabilities: () =>
        read({
          review,
          capabilities: { merge: true, close: true, reopen: true, unapprove: true },
        }),
      postInlineReviewComment: async (params) => {
        posts.push(params);
        throw {
          code: "mutation_timeout",
          message: "The mutation response timed out.",
          retryable: true,
        };
      },
    }),
    review,
    "diff",
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Possibly delivered" },
  });
  fireEvent.click(view.getByRole("button", { name: "Add comment now" }));
  await waitFor(() => assert.equal(posts.length, 1));
  return view;
}

function headerText(view) {
  return [...view.container.querySelectorAll(".review-header")]
    .map((header) => header.textContent)
    .join("");
}

function acknowledgeButtons(view) {
  return view.queryAllByRole("button", { name: ACKNOWLEDGE }).length;
}

for (const [label, panel] of [
  ["Files changed", "diff"],
  ["Discussions", "discussions"],
  ["Overview", "overview"],
]) {
  test(`an unknown inline comment is acknowledged from the ${label} tab header`, async () => {
    const review = `header-unknown-inline-${panel}`;
    const view = await unknownInlineComment(review);
    if (panel !== "diff")
      fireEvent.click(view.getByRole("button", { name: label }));
    // One way out on the page, in the header, and no receipt to check: only a
    // lifecycle action retains one.
    await waitFor(() => assert.equal(acknowledgeButtons(view), 1));
    assert.equal(
      [...view.container.querySelectorAll(".review-header button")].filter(
        (button) => button.textContent === ACKNOWLEDGE,
      ).length,
      1,
    );
    assert.equal(
      view.queryAllByRole("button", { name: "Check retained receipt" }).length,
      0,
    );
    assert.equal(headerText(view).includes("may have completed remotely"), true);
    await waitFor(() =>
      assert.deepEqual(
        actionButtons(view).map((button) => button.title),
        ACTION_LABELS.map(() => ACTIONS_BLOCKED),
      ),
    );
    fireEvent.click(view.getByRole("button", { name: ACKNOWLEDGE }));
    await waitFor(() => assert.equal(acknowledgeButtons(view), 0));
    assert.equal(headerText(view).includes("acknowledged without replay"), true);
    assert.equal(view.queryAllByText(/acknowledged without replay/).length, 1);
    assert.equal(headerText(view).includes("may have completed remotely"), false);
    await waitFor(() =>
      assert.equal(
        actionButtons(view).filter((button) => button.title === ACTIONS_BLOCKED)
          .length,
        0,
      ),
    );
  });
}

test("the header tabs switch panels and keep the same controls", async () => {
  const review = "header-tabs";
  const view = renderReview(reviewBridge(review), review, "overview");
  for (const tab of ["Files changed", "Discussions", "Overview"]) {
    fireEvent.click(view.getByRole("button", { name: tab }));
    await waitFor(() => assert.equal(yourReviewButtons(view), 1));
    await waitFor(() => assert.equal(actionButtons(view).length, 4));
    assert.equal(
      view.container.querySelectorAll(".review-header").length,
      1,
    );
  }
});

function headerButtonTexts(view) {
  return [...view.container.querySelectorAll(".review-header button")].map(
    (button) => button.textContent,
  );
}

function actionButtons(view) {
  return [
    ...view.container.querySelectorAll(".review-header [data-review-action]"),
  ];
}

function actionStates(view) {
  return actionButtons(view).map((button) => [
    button.textContent,
    button.disabled,
  ]);
}

function yourReviewButtons(view) {
  return view.queryAllByRole("button", { name: /^Your review/ }).length;
}

async function settledActions(view) {
  await waitFor(() =>
    assert.equal(
      actionButtons(view).filter((button) => button.title === ACTIONS_LOADING)
        .length,
      0,
    ),
  );
}

async function enabledAction(view, name) {
  return waitFor(() => {
    const found = view.getByRole("button", { name });
    assert.equal(found.disabled, false);
    return found;
  });
}

async function settle(rounds = 10) {
  for (let round = 0; round < rounds; round += 1)
    await new Promise((resolve) => setTimeout(resolve, 5));
}

function renderReview(bridge, review, panel) {
  const features = [
    createReviewOverviewFeature(),
    createDiffFeature(),
    createReviewFeature(bridge),
  ];
  const panels = [
    { id: "overview", label: "Overview", order: 10 },
    { id: "diff", label: "Files changed", order: 20 },
    { id: "discussions", label: "Discussions", order: 40 },
  ];
  function Harness() {
    const [inlineAnchor, setInlineAnchor] = React.useState(null);
    const [route, setRoute] = React.useState({
      kind: "review",
      item: reviewItem(review),
      panel,
    });
    const queries = React.useMemo(() => new QueryCoordinator(bridge), []);
    const feature = features.find((candidate) => candidate.matches(route));
    return feature.render(
      {
        bridge,
        queries,
        repositories: [
          { handle: "repo", display_name: "example/repo", forge_type: "github" },
        ],
        repositoriesReady: true,
        repositoryGeneration: 1,
        reviewPanels: panels,
        inlineAnchor,
        selectInlineAnchor: setInlineAnchor,
        navigate: setRoute,
      },
      route,
    );
  }
  return render(React.createElement(Harness));
}

function reviewBridge(review, changes = {}) {
  return {
    cancelRead: async () => true,
    openExternal: async () => true,
    getReview: () => read(snapshot(review)),
    listDiscussions: () => read({ discussions: [] }),
    openDiff: (params) => read(diffPage(review, params.layout ?? "unified")),
    pageDiff: () => {
      throw new Error("the fixture diff has one page");
    },
    getReviewMutationCapabilities: () =>
      read({ review, capabilities: mutationCapabilities() }),
    getReviewActionCapabilities: () =>
      read({
        review,
        capabilities: { merge: false, close: false, reopen: false, unapprove: false },
      }),
    getReviewActionReceipt: () => read({ receipt: null }),
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [] }),
    listReviewSubmissions: () =>
      read({ cursor: 0, next_cursor: null, attempts: [] }),
    mergeReview: async (params) =>
      actionReceipt(review, params.operation_id, "merge"),
    closeReview: async (params) =>
      actionReceipt(review, params.operation_id, "close"),
    reopenReview: async (params) =>
      actionReceipt(review, params.operation_id, "reopen"),
    unapproveReview: async (params) =>
      actionReceipt(review, params.operation_id, "unapprove"),
    ...changes,
  };
}

function read(value) {
  return {
    requestToken: crypto.randomUUID(),
    result: value instanceof Promise ? value : Promise.resolve(value),
  };
}

function mutationCapabilities() {
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

function snapshot(review) {
  return {
    handle: review,
    repository: "repo",
    detail: reviewItem(review).summary,
    capabilities: {
      batched_review: true,
      thread_resolution: true,
      draft_notes: true,
      unapprove: false,
      job_cancel: false,
    },
    revision: REVISION,
    revision_error: null,
  };
}

function actionReceipt(review, operationId, action) {
  return {
    operation_id: operationId,
    action,
    review,
    revision: REVISION,
    expected_state: "open",
    outcome: "known",
    remote_id: "remote",
    merge_sha: action === "merge" ? "merge-sha" : null,
    source_cleanup: action === "merge" ? "confirmed" : "not_requested",
    error: null,
    resync_required: true,
  };
}

function reviewItem(handle) {
  return {
    handle,
    repository: "repo",
    summary: {
      number: 46,
      title: "Desktop review",
      author: { username: "author", display_name: "Author" },
      state: "open",
      is_draft: false,
      source_branch: "feature",
      target_branch: "main",
      ci_status: "success",
      created_at: "2026-09-09T00:00:00Z",
      updated_at: "2026-09-09T00:00:00Z",
      web_url: "https://github.com/example/repo/pull/46",
      comment_count: 0,
      has_conflicts: false,
      labels: [],
      review_decision: null,
      additions: 1,
      deletions: 1,
      description: "",
      merge_status: "can_be_merged",
      approvals: [],
      reviewers: [],
      assignees: [],
      changes_count: 2,
      detailed_merge_status: null,
      draft_notes_count: 0,
      status_check_rollup: "success",
    },
  };
}

function diffPage(review, layout) {
  const file = {
    kind: "file",
    file_index: 0,
    old_path: "src/calc.py",
    new_path: "src/calc.py",
    status: "modified",
    additions: 1,
    deletions: 0,
    is_binary: false,
    language: "python",
    is_truncated: false,
    is_empty: false,
    is_mode_only: false,
    is_rename_only: false,
    is_unavailable: false,
  };
  const hunk = {
    kind: "hunk",
    file_index: 0,
    hunk_index: 0,
    header: "@@ -10,1 +10,2 @@",
    old_start: 10,
    old_count: 1,
    new_start: 10,
    new_count: 2,
    context_text: "def divide(a, b):",
  };
  const sources = [
    { old_line: 10, new_line: 10, content: "def divide(a, b):", line_type: "context" },
    { old_line: null, new_line: 11, content: "    if b == 0:", line_type: "addition" },
  ];
  const entries =
    layout === "split"
      ? sources.map((source, index) => ({
          kind: "split",
          file_index: 0,
          hunk_index: 0,
          row_index: index,
          old: source.old_line === null ? null : { ...source, anchor_side: "old" },
          new: source.new_line === null ? null : { ...source, anchor_side: "new" },
        }))
      : sources.map((source) => ({
          kind: "line",
          file_index: 0,
          hunk_index: 0,
          ...source,
        }));
  return {
    snapshot_id: `snapshot-${layout}-${review}`,
    resource: review,
    revision: REVISION,
    cursor: 0,
    next_cursor: null,
    entries: [file, hunk, ...entries],
  };
}
