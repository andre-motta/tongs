import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { createReviewFeature } from "../../../desktop/dist/src/renderer/features/review/index.js";

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

test("quick comment remains immediate, retains unknown intent, and needs explicit acknowledgment", async () => {
  const operations = [];
  const review = "review-quick";
  let calls = 0;
  const bridge = reviewBridge(review, {
    postReviewComment: async (params) => {
      calls += 1;
      operations.push(params.operation_id);
      if (calls === 1)
        throw {
          code: "mutation_timeout",
          message: "The mutation response timed out.",
          retryable: true,
        };
      return mutation(params.operation_id);
    },
  });
  const view = renderFeature(bridge, review);
  const composer = await view.findByLabelText("Quick comment");
  fireEvent.change(composer, { target: { value: "Possibly delivered" } });
  fireEvent.click(view.getByRole("button", { name: "Quick comment" }));
  await view.findByText(/may have completed remotely/);
  assert.equal(calls, 1);
  assert.equal(view.getByRole("button", { name: /Quick comment/ }).disabled, true);
  fireEvent.click(
    view.getByRole("button", {
      name: "I inspected the forge; acknowledge uncertainty",
    }),
  );
  await view.findByText(/acknowledged without replay/);
  assert.equal(calls, 1);
  fireEvent.change(view.getByLabelText("Quick comment"), {
    target: { value: "Deliberate second intent" },
  });
  fireEvent.click(view.getByRole("button", { name: "Quick comment" }));
  await waitFor(() => assert.equal(calls, 2));
  assert.notEqual(operations[0], operations[1]);
});

test("explicit review recovery avoids duplicate drafts and preserves text on version conflict", async () => {
  const review = "review-draft";
  let creates = 0;
  let saves = 0;
  const bridge = reviewBridge(review, {
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, "server")] }),
    createReviewDraft: async () => {
      creates += 1;
      return draft(review, 1, "created");
    },
    saveReviewDraft: async (params) => {
      saves += 1;
      if (saves === 1)
        throw { code: "conflict", message: "The draft changed.", retryable: true };
      return draft(review, 3, params.content.body);
    },
    getReviewDraft: () => read(draft(review, 2, "TUI edit")),
  });
  const view = renderFeature(bridge, review);
  fireEvent.click(await view.findByRole("button", { name: "Resume review" }));
  await view.findByText("Draft review active");
  assert.equal(creates, 0);
  const body = view.getByLabelText("Review body");
  fireEvent.change(body, { target: { value: "local unsaved text" } });
  fireEvent.click(view.getByRole("button", { name: "Save draft" }));
  await view.findByText(/changed elsewhere/);
  assert.equal(view.getByLabelText("Review body").value, "local unsaved text");
  fireEvent.click(view.getByRole("button", { name: "Keep my text" }));
  fireEvent.click(view.getByRole("button", { name: "Save draft" }));
  await waitFor(() => assert.equal(saves, 2));
  assert.equal(view.getByLabelText("Review body").value, "local unsaved text");
});

test("submission needs confirmation and unknown progress exposes reconciliation without restart", async () => {
  const review = "review-submit";
  let starts = 0;
  let reconciles = 0;
  const bridge = reviewBridge(review, {
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, "ready")] }),
    startReviewSubmission: async () => {
      starts += 1;
      return submission(review, "unknown");
    },
    reconcileReviewSubmission: async (_params) => {
      reconciles += 1;
      return submission(review, "submitted");
    },
  });
  const view = renderFeature(bridge, review);
  fireEvent.click(await view.findByRole("button", { name: "Resume review" }));
  const submit = await view.findByRole("button", { name: "Submit review" });
  fireEvent.click(submit);
  assert.equal(starts, 0);
  fireEvent.click(view.getByRole("button", { name: "Confirm submit review" }));
  await view.findByText(/Remote status is unknown/);
  assert.equal(starts, 1);
  fireEvent.click(view.getByRole("button", { name: "Retry only remaining steps" }));
  await waitFor(() => assert.equal(reconciles, 1));
  assert.equal(starts, 1);
  await view.findByText("Review submitted.");
});

test("discussion controls explain unsupported actions and support keyboard focus", async () => {
  const review = "review-discussion";
  let resolves = 0;
  const bridge = reviewBridge(review, {
    listDiscussions: () => read({ discussions: [discussion()] }),
    getReviewMutationCapabilities: () =>
      read({ review, capabilities: capabilities({ reply: false, resolve: true }) }),
    resolveReviewDiscussion: async (params) => {
      resolves += 1;
      return mutation(params.operation_id);
    },
  });
  const view = renderFeature(bridge, review);
  const reply = await view.findByRole("button", { name: "Reply" });
  const resolve = view.getByRole("button", { name: "Resolve" });
  assert.equal(reply.disabled, true);
  assert.match(reply.title, /unsupported/);
  const list = view.container.querySelector(".review-workflow-thread-list");
  fireEvent.keyDown(list, { key: "ArrowDown" });
  assert.equal(document.activeElement, resolve);
  fireEvent.click(resolve);
  await waitFor(() => assert.equal(resolves, 1));
  await view.findByRole("button", { name: "Reopen thread" });
});

test("quick verdict and merge cleanup require explicit confirmation with immutable command options", async () => {
  const review = "review-actions";
  let verdicts = 0;
  const merges = [];
  const bridge = reviewBridge(review, {
    getReviewActionCapabilities: () =>
      read({ review, capabilities: { merge: true, close: false, reopen: false, unapprove: false } }),
    submitReviewVerdict: async (params) => {
      verdicts += 1;
      return mutation(params.operation_id);
    },
    mergeReview: async (params) => {
      merges.push(params);
      return actionReceipt(review, params.operation_id, "merge");
    },
  });
  const view = renderFeature(bridge, review);
  const approve = await view.findByRole("button", { name: "Approve review" });
  fireEvent.click(approve);
  assert.equal(verdicts, 0);
  fireEvent.click(view.getByRole("button", { name: "Confirm approve review" }));
  await waitFor(() => assert.equal(verdicts, 1));

  fireEvent.click(view.getByLabelText("Squash commits"));
  fireEvent.click(view.getByLabelText("Delete source branch after merge"));
  fireEvent.click(view.getByRole("button", { name: "Merge" }));
  assert.equal(merges.length, 0);
  fireEvent.click(view.getByRole("button", { name: "Confirm Merge" }));
  await waitFor(() => assert.equal(merges.length, 1));
  assert.equal(merges[0].squash, true);
  assert.deepEqual(merges[0].source_cleanup, { branch: "feature" });
});

function renderFeature(bridge, review) {
  const feature = createReviewFeature(bridge);
  const item = reviewItem(review);
  return render(
    feature.render(
      {
        bridge,
        queries: new QueryCoordinator(bridge),
        repositories: [],
        repositoriesReady: true,
        repositoryGeneration: 1,
        reviewPanels: [{ id: "discussions", label: "Discussions", order: 40 }],
        inlineAnchor: null,
        selectInlineAnchor: () => {},
        navigate: () => {},
      },
      { kind: "review", item, panel: "discussions" },
    ),
  );
}

function reviewBridge(review, changes = {}) {
  return {
    getReview: () => read(snapshot(review)),
    listDiscussions: () => read({ discussions: [] }),
    getReviewMutationCapabilities: () =>
      read({ review, capabilities: capabilities() }),
    getReviewActionCapabilities: () =>
      read({ review, capabilities: { merge: false, close: false, reopen: false, unapprove: false } }),
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [] }),
    listReviewSubmissions: () => read({ cursor: 0, next_cursor: null, attempts: [] }),
    createReviewDraft: async () => draft(review, 1, ""),
    getReviewDraft: () => read(draft(review, 1, "")),
    saveReviewDraft: async (params) => draft(review, params.expected_version + 1, params.content.body),
    postReviewComment: async (params) => mutation(params.operation_id),
    postInlineReviewComment: async (params) => mutation(params.operation_id),
    submitReviewVerdict: async (params) => mutation(params.operation_id),
    replyReviewDiscussion: async (params) => mutation(params.operation_id),
    resolveReviewDiscussion: async (params) => mutation(params.operation_id),
    startReviewSubmission: async () => submission(review, "submitted"),
    resumeReviewSubmission: async () => submission(review, "submitted"),
    reconcileReviewSubmission: async () => submission(review, "submitted"),
    getReviewActionReceipt: () => read({ receipt: null }),
    cancelRead: async () => true,
    ...changes,
  };
}

function read(value) {
  return {
    requestToken: crypto.randomUUID(),
    result: value instanceof Promise ? value : Promise.resolve(value),
  };
}

function capabilities(changes = {}) {
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
    ...changes,
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
    revision: { head_sha: "head", base_sha: "base", start_sha: null },
    revision_error: null,
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
      created_at: "2026-09-08T00:00:00Z",
      updated_at: "2026-09-08T00:00:00Z",
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

function draft(review, version, body) {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    review,
    revision: { head_sha: "head", base_sha: "base", start_sha: null },
    version,
    body,
    verdict: null,
    comments: [],
    state: "editable",
    created_at: "2026-09-08T00:00:00+00:00",
    updated_at: "2026-09-08T00:00:00+00:00",
  };
}

function mutation(operationId) {
  return {
    operation_id: operationId,
    outcome: "known",
    receipt: { remote_id: "remote", comment_id: "comment", discussion_id: null },
    reason: null,
    resync_required: false,
  };
}

function submission(review, outcome) {
  return {
    attempt_id: "22222222-2222-4222-8222-222222222222",
    draft_id: "11111111-1111-4111-8111-111111111111",
    review,
    frozen_version: 1,
    state: outcome === "unknown" ? "unknown" : "submitted",
    outcome,
    steps: [{ id: "verdict", kind: "verdict", comment_ids: [] }],
    receipts: [],
    completed_step_ids: [],
    unknown_step_ids: outcome === "unknown" ? ["verdict"] : [],
    atomic: false,
    resync_required: outcome === "unknown",
    failure: null,
    plan_available: true,
  };
}

function actionReceipt(review, operationId, action) {
  return {
    operation_id: operationId,
    action,
    review,
    revision: { head_sha: "head", base_sha: "base", start_sha: null },
    expected_state: "open",
    outcome: "known",
    remote_id: "remote",
    merge_sha: action === "merge" ? "merge-sha" : null,
    source_cleanup: action === "merge" ? "confirmed" : "not_requested",
    error: null,
    resync_required: true,
  };
}

function discussion() {
  return {
    id: "thread-1",
    is_inline: true,
    is_resolved: false,
    resolvable: true,
    root_comment: {
      id: "comment-1",
      author: { username: "reviewer", display_name: "Reviewer" },
      body: "Please adjust this.",
      created_at: "2026-09-08T00:00:00Z",
      file_path: "src/example.py",
      old_line: null,
      new_line: 4,
      is_resolved: false,
      replies: [],
    },
  };
}
