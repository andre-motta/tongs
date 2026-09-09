import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import {
  allocateDiscussionMarkdown,
  createReviewFeature,
  discussionDiffTarget,
} from "../../../desktop/dist/src/renderer/features/review/index.js";
import { createReviewOverviewFeature } from "../../../desktop/dist/src/renderer/features/review-detail/index.js";
import { cacheWorkflow } from "../../../desktop/dist/src/renderer/features/review/composer.js";
import {
  adoptDraft,
  beginSubmission,
  createReviewWorkflowState,
} from "../../../desktop/dist/src/renderer/features/review/state.js";

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
  const view = renderOverview(bridge, review);
  const composer = await view.findByLabelText("General review comment");
  fireEvent.change(composer, { target: { value: "Possibly delivered" } });
  fireEvent.click(await enabledButton(view, "Add comment now"));
  await view.findByText(/may have completed remotely/);
  assert.equal(calls, 1);
  assert.equal(
    view.getByRole("button", { name: "Add comment now" }).disabled,
    true,
  );
  fireEvent.click(
    view.getByRole("button", {
      name: "I inspected the forge; acknowledge uncertainty",
    }),
  );
  await view.findByText(/acknowledged without replay/);
  assert.equal(calls, 1);
  fireEvent.change(view.getByLabelText("General review comment"), {
    target: { value: "Deliberate second intent" },
  });
  fireEvent.click(await enabledButton(view, "Add comment now"));
  await waitFor(() => assert.equal(calls, 2));
  assert.notEqual(operations[0], operations[1]);
});

function conflictingDraftBridge(review, counters) {
  return reviewBridge(review, {
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, "server")] }),
    createReviewDraft: async () => {
      counters.creates += 1;
      return draft(review, 1, "created");
    },
    saveReviewDraft: async (params) => {
      counters.saves += 1;
      counters.savedParams.push(params);
      if (counters.saves === 1)
        throw { code: "conflict", message: "The draft changed.", retryable: true };
      return draft(review, params.expected_version + 1, params.content.body);
    },
    getReviewDraft: () => read({
      ...draft(review, 2, "TUI edit"),
      comments: [{ id: "33333333-3333-4333-8333-333333333333", kind: "general", body: "TUI comment" }],
    }),
  });
}

async function conflictedDraftView(review, counters) {
  const view = renderFeature(conflictingDraftBridge(review, counters), review);
  // Mount adoption binds the one active draft, so nothing has to be resumed.
  await view.findByText("Draft review active");
  await view.findByText("Stored version 1");
  assert.equal(counters.creates, 0);
  fireEvent.click(view.getByRole("button", { name: /Your review/ }));
  fireEvent.change(view.getByLabelText("Summary"), {
    target: { value: "local unsaved text" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  await view.findByText(/now version 2, and you were editing version 1/);
  return view;
}

function counters() {
  return { creates: 0, saves: 0, savedParams: [] };
}

test("a stale save shows both draft versions and neither is silently discarded", async () => {
  const state = counters();
  const view = await conflictedDraftView("review-draft-both", state);
  assert.equal(view.getByLabelText("Summary").value, "local unsaved text");
  assert.match(view.getByLabelText("My unsaved draft text").value, /local unsaved text/);
  const stored = view.getByLabelText("Stored draft text").value;
  assert.match(stored, /TUI edit/);
  assert.match(stored, /TUI comment/);
  assert.equal(
    view.getByRole("button", { name: "Save summary and verdict" }).disabled,
    true,
  );
  assert.equal(
    view.getByRole("button", { name: "Submit review" }).disabled,
    true,
  );
  assert.equal(state.saves, 1);
  assert.equal(state.creates, 0);
});

test("keeping my text re-saves it on top of the stored version", async () => {
  const state = counters();
  const view = await conflictedDraftView("review-draft-keep-mine", state);
  fireEvent.click(
    view.getByRole("button", { name: "Keep my text and save over version 2" }),
  );
  await waitFor(() => assert.equal(state.saves, 2));
  assert.deepEqual(
    {
      expected: state.savedParams[1].expected_version,
      body: state.savedParams[1].content.body,
    },
    { expected: 2, body: "local unsaved text" },
  );
  await view.findByText("Stored version 3");
  assert.equal(view.queryByLabelText("Stored draft text"), null);
  assert.equal(view.getByLabelText("Summary").value, "local unsaved text");
  assert.equal(view.queryByLabelText("My superseded draft text"), null);
  assert.equal(state.creates, 0);
});

test("taking the stored version keeps my text reachable until I dismiss it", async () => {
  const state = counters();
  const view = await conflictedDraftView("review-draft-take-theirs", state);
  fireEvent.click(
    view.getByRole("button", { name: "Take the stored version and keep mine to copy" }),
  );
  await view.findByText("Stored version 2");
  assert.equal(view.queryByLabelText("Stored draft text"), null);
  assert.equal(view.getByLabelText("Summary").value, "TUI edit");
  assert.match(
    view.getByLabelText("My superseded draft text replaced by version 2").value,
    /local unsaved text/,
  );
  assert.equal(state.saves, 1);
  assert.equal(state.creates, 0);
  fireEvent.click(
    view.getByRole("button", { name: "Dismiss my text replaced by version 2" }),
  );
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll(
        '[aria-label="My superseded draft text replaced by version 2"]',
      ).length,
      0,
    ));
  assert.equal(view.getByLabelText("Summary").value, "TUI edit");
});

test("a second take-theirs keeps both retained texts, each dismissed on its own", async () => {
  const review = "review-draft-two-conflicts";
  let saves = 0;
  const remote = [
    { ...draft(review, 2, "stored two"), updated_at: "2026-09-08T01:00:00+00:00" },
    { ...draft(review, 4, "stored four"), updated_at: "2026-09-08T02:00:00+00:00" },
  ];
  const bridge = reviewBridge(review, {
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, "server")] }),
    saveReviewDraft: async () => {
      saves += 1;
      throw { code: "conflict", message: "The draft changed.", retryable: true };
    },
    getReviewDraft: () => read(remote[Math.min(saves, remote.length) - 1]),
  });
  const view = renderFeature(bridge, review);
  // Mount adoption binds the one active draft, so nothing has to be resumed.
  await view.findByText("Draft review active");
  await view.findByText("Stored version 1");
  fireEvent.click(view.getByRole("button", { name: /Your review/ }));

  fireEvent.change(view.getByLabelText("Summary"), {
    target: { value: "first local" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  await view.findByText(/now version 2, and you were editing version 1/);
  fireEvent.click(
    view.getByRole("button", { name: "Take the stored version and keep mine to copy" }),
  );
  await view.findByLabelText("My superseded draft text replaced by version 2");

  fireEvent.change(view.getByLabelText("Summary"), {
    target: { value: "second local" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  await view.findByText(/now version 4, and you were editing version 2/);
  fireEvent.click(
    view.getByRole("button", { name: "Take the stored version and keep mine to copy" }),
  );
  await view.findByLabelText("My superseded draft text replaced by version 4");

  assert.equal(saves, 2);
  assert.match(
    view.getByLabelText("My superseded draft text replaced by version 2").value,
    /first local/,
  );
  assert.match(
    view.getByLabelText("My superseded draft text replaced by version 4").value,
    /second local/,
  );
  assert.equal(view.getByLabelText("Summary").value, "stored four");

  fireEvent.click(
    view.getByRole("button", { name: "Dismiss my text replaced by version 2" }),
  );
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll(
        '[aria-label="My superseded draft text replaced by version 2"]',
      ).length,
      0,
    ));
  assert.match(
    view.getByLabelText("My superseded draft text replaced by version 4").value,
    /second local/,
  );
});

test("keeping my text is refused with its reason when the stored draft was submitted", async () => {
  const review = "review-draft-submitted-conflict";
  let saves = 0;
  const bridge = reviewBridge(review, {
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, "server")] }),
    saveReviewDraft: async () => {
      saves += 1;
      throw { code: "conflict", message: "The draft changed.", retryable: true };
    },
    getReviewDraft: () =>
      read({ ...draft(review, 2, "submitted elsewhere"), state: "submitted" }),
  });
  const view = renderFeature(bridge, review);
  // Mount adoption binds the one active draft, so nothing has to be resumed.
  await view.findByText("Draft review active");
  await view.findByText("Stored version 1");
  fireEvent.click(view.getByRole("button", { name: /Your review/ }));
  fireEvent.change(view.getByLabelText("Summary"), {
    target: { value: "my precious text" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  await view.findByText(/now version 2, and you were editing version 1/);

  fireEvent.click(
    view.getByRole("button", { name: "Keep my text and save over version 2" }),
  );
  await view.findByText(/already submitted/);
  assert.equal(saves, 1);
  assert.match(view.getByLabelText("Stored draft text").value, /submitted elsewhere/);
  assert.match(view.getByLabelText("My unsaved draft text").value, /my precious text/);
  assert.equal(view.getByLabelText("Summary").value, "my precious text");
  assert.equal(
    Boolean(
      view.getByRole("button", {
        name: "Take the stored version and keep mine to copy",
      }),
    ),
    true,
  );

  fireEvent.click(
    view.getByRole("button", { name: "Take the stored version and keep mine to copy" }),
  );
  await view.findByLabelText("My superseded draft text replaced by version 2");
  assert.match(
    view.getByLabelText("My superseded draft text replaced by version 2").value,
    /my precious text/,
  );
  assert.equal(saves, 1);
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
  // Mount adoption binds the one active draft, so nothing has to be resumed.
  await view.findByText("Draft review active");
  await view.findByText("Stored version 1");
  fireEvent.click(view.getByRole("button", { name: /Your review/ }));
  const submit = await view.findByRole("button", { name: "Submit review" });
  fireEvent.click(submit);
  assert.equal(starts, 0);
  fireEvent.click(view.getByRole("button", { name: "Confirm submit review" }));
  await view.findByText(/Remote status is unknown/);
  assert.equal(starts, 1);
  const steps = view.getByRole("list", { name: "Submission steps" });
  assert.match(steps.textContent, /Verdict: unknown/);
  fireEvent.click(view.getByRole("button", { name: "Retry only remaining steps" }));
  assert.match(view.getByText(/can repeat an unconfirmed remote write/).textContent, /Confirmed receipt steps stay excluded/);
  assert.equal(reconciles, 0);
  fireEvent.click(view.getByRole("button", { name: "Confirm possible repeat of remaining writes" }));
  await waitFor(() => assert.equal(reconciles, 1));
  assert.equal(starts, 1);
  await view.findByText("Review submitted.");
});

test("submission timeout recovers durable status without replaying start", async () => {
  const review = "review-timeout-recovery";
  let lists = 0;
  let starts = 0;
  const recovered = submission(review, "unknown");
  const bridge = reviewBridge(review, {
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, "ready")] }),
    listReviewSubmissions: () => {
      lists += 1;
      return read({
        cursor: 0,
        next_cursor: null,
        attempts: lists === 1 ? [] : [recovered],
      });
    },
    startReviewSubmission: async () => {
      starts += 1;
      throw {
        code: "mutation_timeout",
        message: "The submission response timed out.",
        retryable: true,
      };
    },
  });
  const view = renderFeature(bridge, review);
  // Mount adoption binds the one active draft, so nothing has to be resumed.
  await view.findByText("Draft review active");
  await view.findByText("Stored version 1");
  fireEvent.click(view.getByRole("button", { name: /Your review/ }));
  fireEvent.click(await view.findByRole("button", { name: "Submit review" }));
  fireEvent.click(view.getByRole("button", { name: "Confirm submit review" }));
  await view.findByText(/Remote status is unknown/);
  assert.equal(starts, 1);
  assert.equal(lists, 2);
});

test("the Discussions jump list lists unresolved threads first and jumps to the stored target", async () => {
  const review = "review-discussion";
  const navigated = [];
  const resolved = {
    ...discussion(),
    id: "thread-resolved",
    is_resolved: true,
    root_comment: {
      ...discussion().root_comment,
      id: "comment-resolved",
      file_path: "src/other.py",
      new_line: 9,
    },
  };
  const bridge = reviewBridge(review, {
    listDiscussions: () => read({ discussions: [resolved, discussion()] }),
  });
  const view = renderFeature(bridge, review, {
    navigate: (route) => navigated.push(route),
  });

  const rows = await waitFor(() => {
    const found = view.container.querySelectorAll(".review-workflow-thread");
    assert.equal(found.length, 2);
    return found;
  });
  // Unresolved first, whatever order the forge listed them in.
  assert.match(
    rows[0].querySelector(".review-workflow-thread-summary").textContent,
    /unresolved/,
  );
  assert.match(
    rows[0].querySelector(".review-workflow-thread-meta").textContent,
    /src\/example\.py/,
  );
  assert.match(
    rows[1].querySelector(".review-workflow-thread-summary").textContent,
    /resolved/,
  );

  // S37: the jump target triple is the stored coordinate, unchanged.
  const show = view.getAllByRole("button", { name: "Show in diff" })[0];
  fireEvent.click(show);
  assert.deepEqual(navigated[0].diffTarget, {
    discussionId: "thread-1",
    path: "src/example.py",
    side: "new",
    line: 4,
  });
  assert.equal(navigated[0].panel, "diff");

  const list = view.container.querySelector(".review-workflow-thread-list");
  fireEvent.keyDown(list, { key: "ArrowDown" });
  assert.equal(document.activeElement, show);
});

test("the Discussions panel offers no composer, suggestion, reply or resolve control", async () => {
  const review = "review-discussion-no-writes";
  const bridge = reviewBridge(review, {
    listDiscussions: () => read({ discussions: [discussion()] }),
  });
  const view = renderFeature(bridge, review);
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll(".review-workflow-thread").length,
      1,
    ),
  );

  assert.equal(view.container.querySelectorAll("textarea").length, 0);
  assert.equal(
    view.container.querySelectorAll(".review-workflow-composer").length,
    0,
  );
  assert.equal(
    view.container.querySelectorAll(".suggestion-composer").length,
    0,
  );
  assert.equal(
    view.container.querySelectorAll(".review-workflow-verdicts").length,
    0,
  );
  assert.equal(
    view.container.querySelectorAll(".review-workflow-buffered-inline").length,
    0,
  );
  const names = [...view.container.querySelectorAll("button")].map(
    (button) => button.textContent,
  );
  assert.equal(names.filter((name) => name === "Reply").length, 0);
  assert.equal(names.filter((name) => name === "Resolve").length, 0);
  assert.equal(names.filter((name) => name === "Reopen thread").length, 0);
  assert.equal(names.filter((name) => name === "Suggest replacement").length, 0);
  assert.equal(names.filter((name) => name === "Show in diff").length, 1);
});

test("the Discussions route puts one active-draft read and one capabilities read to the sidecar", async () => {
  const review = "review-discussion-reads";
  let drafts = 0;
  let capabilityReads = 0;
  const bridge = reviewBridge(review, {
    listReviewDrafts: () => {
      drafts += 1;
      return read({ cursor: 0, next_cursor: null, drafts: [] });
    },
    getReviewMutationCapabilities: () => {
      capabilityReads += 1;
      return read({ review, capabilities: capabilities() });
    },
  });
  const view = renderFeature(bridge, review);
  await view.findByRole("button", { name: /Your review/ });
  await waitFor(() => assert.equal(drafts, 1));
  // A second read issued after the first settles would be invisible to an
  // assertion in the same tick, so the route is left to settle first.
  await settle();
  assert.equal(drafts, 1);
  assert.equal(capabilityReads, 1);
  // Nothing on this panel starts a review any more; the drawer is the only
  // surface here that asks either question.
  assert.equal(
    [...view.container.querySelectorAll("button")].filter((button) =>
      ["Start review", "Resume review"].includes(button.textContent),
    ).length,
    0,
  );
});

test("discussion diff targets use only actual path and side line data", () => {
  const inline = discussion();
  assert.deepEqual(discussionDiffTarget(inline), {
    discussionId: "thread-1",
    path: "src/example.py",
    side: "new",
    line: 4,
  });
  assert.deepEqual(
    discussionDiffTarget({
      ...inline,
      root_comment: { ...inline.root_comment, new_line: null, old_line: 9 },
    }),
    {
      discussionId: "thread-1",
      path: "src/example.py",
      side: "old",
      line: 9,
    },
  );
  assert.equal(
    discussionDiffTarget({
      ...inline,
      root_comment: { ...inline.root_comment, new_line: null, old_line: null },
    }),
    null,
  );
});

test("a review-level note reads on Overview and never in the jump list", async () => {
  const review = "review-unpositioned-note";
  const note = {
    id: "53f505e47aca",
    is_inline: false,
    is_resolved: false,
    resolvable: true,
    root_comment: {
      ...discussion().root_comment,
      id: "note-1",
      file_path: null,
      old_line: null,
      new_line: null,
    },
  };
  assert.equal(discussionDiffTarget(note), null);
  const bridge = reviewBridge(review, {
    listDiscussions: () => read({ discussions: [note, discussion()] }),
  });

  const overview = renderOverview(bridge, review);
  const notes = await waitFor(() => {
    const found = overview.container.querySelectorAll(
      ".review-level-discussions .review-workflow-thread",
    );
    assert.equal(found.length, 1);
    return found;
  });
  assert.equal(
    notes[0].querySelector(".review-workflow-thread-meta").textContent,
    "Reviewer",
  );
  assert.equal(
    [...notes[0].querySelectorAll("button")].filter(
      (button) => button.textContent === "Show in diff",
    ).length,
    0,
  );
  cleanup();

  // The jump list carries only threads that resolve to a diff position.
  const panel = renderFeature(bridge, review);
  const rows = await waitFor(() => {
    const found = panel.container.querySelectorAll(".review-workflow-thread");
    assert.equal(found.length, 1);
    return found;
  });
  assert.match(
    rows[0].querySelector(".review-workflow-thread-meta").textContent,
    /src\/example\.py/,
  );
});

test("discussion roots and replies use shared safe Markdown without changing source", async () => {
  const review = "review-markdown-discussion";
  const opened = [];
  const suggestion = "```suggestion\nreplacement(`value`)\n```";
  const markdownDiscussion = {
    ...discussion(),
    root_comment: {
      ...discussion().root_comment,
      body:
        "## Requested change\n\nUse **care** and [docs](HTTPS://Example.COM/help). ![remote](https://bad.invalid/x.png) <script>bad()</script>",
      replies: [
        {
          id: "reply-1",
          author: { username: "author", display_name: "Author" },
          body: suggestion,
          created_at: "2026-09-08T00:01:00Z",
        },
      ],
    },
  };
  const bridge = reviewBridge(review, {
    listDiscussions: () => read({ discussions: [markdownDiscussion] }),
    openExternal: async (url) => {
      opened.push(url);
      return true;
    },
  });
  const view = renderFeature(bridge, review);

  assert.equal(
    (await view.findByRole("heading", { name: "Requested change" })).tagName,
    "H2",
  );
  assert.equal(view.container.querySelector("strong")?.textContent, "care");
  assert.equal(view.container.querySelector("img, script"), null);
  assert.match(view.container.textContent, /\[Image: remote\]/);
  assert.match(view.container.textContent, /<script>bad\(\)<\/script>/);
  assert.equal(
    view.container.querySelector("blockquote pre code")?.textContent,
    "replacement(`value`)\n",
  );
  assert.equal(suggestion, "```suggestion\nreplacement(`value`)\n```");
  assert.deepEqual(opened, []);
  fireEvent.click(view.getByRole("link", { name: "docs" }));
  await waitFor(() => assert.deepEqual(opened, ["https://example.com/help"]));
});

test("discussion Markdown aggregate allocation is deterministic in source order", () => {
  const expensive = "😀".repeat(4096);
  const item = {
    ...discussion(),
    root_comment: {
      ...discussion().root_comment,
      body: expensive,
      replies: Array.from({ length: 17 }, (_, index) => ({
        id: `reply-${index}`,
        author: { username: "author", display_name: "Author" },
        body: index === 16 ? "small later reply" : expensive,
        created_at: "2026-09-08T00:01:00Z",
      })),
    },
  };

  const allocation = allocateDiscussionMarkdown([item]);

  assert.equal(allocation[0].root, true);
  assert.deepEqual(allocation[0].replies.slice(0, 15), Array(15).fill(true));
  assert.equal(allocation[0].replies[15], false);
  assert.equal(allocation[0].replies[16], false);
  assert.equal(Object.isFrozen(allocation), true);
  assert.equal(Object.isFrozen(allocation[0].replies), true);
});

test("discussion callsite stops rendering bodies after its aggregate budget", async () => {
  const review = "review-markdown-budget";
  const expensive = "😀".repeat(4096);
  const discussions = Array.from({ length: 17 }, (_, index) => ({
    ...discussion(),
    id: `thread-${index}`,
    root_comment: {
      ...discussion().root_comment,
      id: `comment-${index}`,
      body: expensive,
      replies: [],
    },
  }));
  const bridge = reviewBridge(review, {
    listDiscussions: () => read({ discussions }),
  });
  const view = renderFeature(bridge, review);

  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll(".safe-markdown-aggregate-omission")
        .length,
      1,
    ),
  );
  assert.equal(
    view.container.querySelectorAll(".safe-markdown-fallback").length,
    16,
  );
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
  const overview = renderOverview(bridge, review);
  fireEvent.click(await enabledButton(overview, "Approve review"));
  assert.equal(verdicts, 0);
  fireEvent.click(
    overview.getByRole("button", { name: "Confirm approve review" }),
  );
  await waitFor(() => assert.equal(verdicts, 1));
  cleanup();

  const view = renderFeature(bridge, review);
  await view.findByRole("button", { name: "Merge" });
  fireEvent.click(view.getByLabelText("Squash commits"));
  fireEvent.click(view.getByLabelText("Delete source branch after merge"));
  fireEvent.click(view.getByRole("button", { name: "Merge" }));
  assert.equal(merges.length, 0);
  fireEvent.click(view.getByRole("button", { name: "Confirm Merge" }));
  await waitFor(() => assert.equal(merges.length, 1));
  assert.equal(merges[0].squash, true);
  assert.deepEqual(merges[0].source_cleanup, { branch: "feature" });
});

test("known quick rejection renders one actionable alert", async () => {
  const review = "review-known-rejection";
  const bridge = reviewBridge(review, {
    getReviewActionCapabilities: () =>
      read({ review, capabilities: { merge: false, close: true, reopen: false, unapprove: false } }),
    closeReview: async () => {
      throw {
        code: "conflict",
        message: "raw backend text that must stay hidden",
        retryable: false,
      };
    },
  });
  const view = renderFeature(bridge, review);
  fireEvent.click(await view.findByRole("button", { name: "Close" }));
  fireEvent.click(view.getByRole("button", { name: "Confirm Close" }));
  const message = "The review changed remotely. Refresh it before choosing another action.";
  assert.equal((await view.findAllByText(message)).length, 1);
  assert.equal(view.getAllByRole("alert").length, 1);
  assert.equal(view.queryByText(/raw backend text/), null);
});

test("draft comments are reviewable, editable, and deliberately removable", async () => {
  const review = "review-draft-comments";
  const comments = [
    { id: "33333333-3333-4333-8333-333333333333", kind: "general", body: "General text" },
    {
      id: "44444444-4444-4444-8444-444444444444",
      kind: "inline",
      body: "Inline text",
      anchor: draftInlineAnchor(false),
    },
    { id: "55555555-5555-4555-8555-555555555555", kind: "reply", body: "Reply text", thread_id: "thread-1" },
  ];
  const saves = [];
  const bridge = reviewBridge(review, {
    listReviewDrafts: () => read({
      cursor: 0,
      next_cursor: null,
      drafts: [{ ...draft(review, 1, "body"), comments }],
    }),
    saveReviewDraft: async (params) => {
      saves.push(params);
      return {
        ...draft(review, params.expected_version + 1, params.content.body),
        comments: params.content.comments,
      };
    },
  });
  const view = renderFeature(bridge, review);
  // Mount adoption binds the one active draft, so nothing has to be resumed.
  await view.findByText("Draft review active");
  await view.findByText("Stored version 1");
  fireEvent.click(view.getByRole("button", { name: /Your review/ }));

  await view.findByText("General text");
  await view.findByText("Inline text");
  await view.findByText("Reply text");
  assert.ok(
    view.getByLabelText("Pending review comment on src/example.py, new line 4"),
  );
  // A pending reply names the discussion it answers, which is the only thing
  // in the drawer that says which thread the reply belongs to.
  assert.ok(view.getByLabelText("Pending review comment on reply to thread-1"));
  const fileHeadings = [
    ...view.container.querySelectorAll(".review-drawer-file-name"),
  ].map((node) => node.textContent);
  assert.equal(fileHeadings.some((text) => text.startsWith("Review-level")), true);
  assert.equal(
    fileHeadings.some((text) => text.startsWith("src/example.py")),
    true,
  );

  // Editable: the general comment (an unanchored entry) edits in place in the
  // drawer. An anchored (inline) entry's Edit instead sends the reader to the
  // diff to edit it there, so it has no in-drawer editor for this test to
  // drive; deleting it below exercises its own control instead.
  fireEvent.click(
    view.getByRole("button", { name: "Edit pending comment on general comment" }),
  );
  const editor = view.getByLabelText("Edit pending general comment");
  assert.equal(editor.value, "General text");
  fireEvent.change(editor, { target: { value: "" } });
  assert.equal(
    view.getByRole("button", { name: "Save pending comment on general comment" })
      .disabled,
    true,
  );
  fireEvent.change(editor, { target: { value: "Updated general text" } });
  fireEvent.click(
    view.getByRole("button", { name: "Save pending comment on general comment" }),
  );
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(saves[0].content.comments.length, 3);
  assert.equal(
    saves[0].content.comments.find((item) => item.id === comments[0].id).body,
    "Updated general text",
  );
  await view.findByText("Updated general text");
  assert.equal(
    view.getByRole("button", { name: "Save summary and verdict" }).disabled,
    true,
  );

  // Deliberately removable: unlike the old local-only "Remove draft comment",
  // Delete in the drawer goes through removeEntry, which saves immediately.
  fireEvent.click(
    view.getByRole("button", {
      name: "Delete pending comment on src/example.py, new line 4",
    }),
  );
  await waitFor(() => assert.equal(saves.length, 2));
  assert.equal(saves[1].content.comments.length, 2);
  assert.equal(
    saves[1].content.comments.some((item) => item.id === comments[1].id),
    false,
  );
  // Counted rather than queried for a null node: a failed absence check must
  // report a number, not serialise the mounted tree into the failure.
  assert.equal(
    [...view.container.querySelectorAll(".review-drawer-entry-body")].filter(
      (element) => element.textContent.includes("Inline text"),
    ).length,
    0,
  );
  assert.equal(
    view.container.querySelectorAll(
      '[aria-label="Pending review comment on src/example.py, new line 4"]',
    ).length,
    0,
  );
});

test("stale draft migration creates a separate draft and keeps inline and reply text recoverable", async () => {
  const review = "review-stale-migration";
  const oldRevision = { head_sha: "old-head", base_sha: "base", start_sha: null };
  const currentRevision = { head_sha: "new-head", base_sha: "base", start_sha: null };
  const staleComments = [
    { id: "33333333-3333-4333-8333-333333333333", kind: "general", body: "Portable general" },
    {
      id: "44444444-4444-4444-8444-444444444444",
      kind: "inline",
      body: "Keep this inline text",
      anchor: { ...draftInlineAnchor(true), revision: oldRevision },
    },
    { id: "55555555-5555-4555-8555-555555555555", kind: "reply", body: "Keep this reply", thread_id: "thread-old" },
  ];
  const stale = {
    ...draft(review, 3, "Portable body"),
    revision: oldRevision,
    verdict: "approve",
    comments: staleComments,
  };
  const creates = [];
  const bridge = reviewBridge(review, {
    getReview: () => read({ ...snapshot(review), revision: currentRevision }),
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [stale] }),
    createReviewDraft: async (params) => {
      creates.push(params);
      return {
        ...draft(review, 1, params.content.body),
        id: "66666666-6666-4666-8666-666666666666",
        revision: currentRevision,
        verdict: params.content.verdict,
        comments: params.content.comments,
      };
    },
  });
  const view = renderFeature(bridge, review);
  // Mount adoption binds the one active draft, so nothing has to be resumed.
  await view.findByText("Draft review active");
  fireEvent.click(view.getByRole("button", { name: /Your review/ }));
  await view.findByText(/remains preserved at revision old-head/);
  assert.equal(view.getByRole("button", { name: "Submit review" }).disabled, true);
  fireEvent.click(view.getByRole("button", { name: "Create current-revision draft" }));
  assert.equal(creates.length, 0);
  fireEvent.click(view.getByRole("button", { name: "Confirm create separate current-revision draft" }));
  await waitFor(() => assert.equal(creates.length, 1));
  assert.equal(creates[0].revision.head_sha, "new-head");
  assert.deepEqual(creates[0].content.comments.map((item) => item.kind), ["general"]);
  assert.equal(creates[0].content.body, "Portable body");
  assert.equal(creates[0].content.verdict, "approve");
  // The S46 bound the title names: the old draft is preserved, and the inline
  // and reply text it holds stays readable in the drawer so it can be
  // recreated deliberately after its current targets are checked. Nothing was
  // retargeted, so the preserved section names the revision it belongs to.
  await view.findByText(/Preserved old draft 11111111/);
  await view.findByText(/Revision old-head/);
  assert.ok(view.getByText("Keep this inline text"));
  assert.ok(view.getByText("Keep this reply"));
  // The preserved list names each entry the way the base draft editor did:
  // the entry's own id, and for an inline entry its path, side, line and the
  // stale marker, so two entries on one anchor stay distinguishable.
  assert.ok(
    view.getByText(
      "Reply 55555555-5555-4555-8555-555555555555 to discussion thread-old",
    ),
  );
  assert.ok(
    view.getByText(
      "Inline 44444444-4444-4444-8444-444444444444 \u00b7 src/example.py \u00b7 new line 4 \u00b7 stale anchor",
    ),
  );

  // The portable general comment travelled to the new draft, so it is not
  // repeated as preserved text that still needs recreating.
  assert.equal(
    [
      ...view.container.querySelectorAll(
        ".review-workflow-preserved-draft article",
      ),
    ].length,
    2,
  );
});

test("durable recovery loads and binds the attempt draft instead of another candidate", async () => {
  const review = "review-multi-draft-recovery";
  const otherId = "66666666-6666-4666-8666-666666666666";
  const first = draft(review, 3, "first draft");
  const matching = {
    ...draft(review, 2, "matching recovered draft"),
    id: otherId,
    state: "unknown",
  };
  const attempt = {
    ...submission(review, "unknown"),
    draft_id: otherId,
    frozen_version: 1,
  };
  const loads = [];
  const bridge = reviewBridge(review, {
    listReviewDrafts: () => read({
      cursor: 0,
      next_cursor: null,
      drafts: [first, matching],
    }),
    listReviewSubmissions: () => read({
      cursor: 0,
      next_cursor: null,
      attempts: [attempt],
    }),
    getReviewDraft: (params) => {
      loads.push(params);
      return read(matching);
    },
  });
  const view = renderFeature(bridge, review);
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(await view.findByRole("button", {
    name: "Recover unknown attempt with 0 confirmed step(s)",
  }));
  await waitFor(() => assert.equal(loads.length, 1));
  assert.equal(loads[0].draft_id, otherId);
  assert.equal(view.getByLabelText("Summary").value, "matching recovered draft");
  await view.findByText(/Remote status is unknown/);
  assert.equal(view.queryByDisplayValue("first draft"), null);
});

test("a failed review-level discussions read is stated, not reported as an absence", async () => {
  const review = "review-notes-failure";
  let attempts = 0;
  const bridge = reviewBridge(review, {
    listDiscussions: () => {
      attempts += 1;
      return read(Promise.reject(new Error("controlled discussions failure")));
    },
  });
  const view = renderOverview(bridge, review);

  const refusal =
    "The published discussions could not be read, so this review shows no review-level notes. Retry, or open the review on the forge.";
  await view.findByText(refusal);
  const section = view.container.querySelector(".review-level-discussions");
  assert.equal(section.querySelectorAll(".notice-error").length, 1);
  assert.equal(section.querySelectorAll(".notice-empty").length, 0);
  assert.equal(section.textContent.includes("No review-level discussions yet."), false);

  const retry = view.getByRole("button", { name: "Retry" });
  fireEvent.click(retry);
  await waitFor(() => assert.equal(attempts, 2));
});

test("review-level discussions still being read say so instead of saying there are none", async () => {
  const review = "review-notes-loading";
  const bridge = reviewBridge(review, {
    listDiscussions: () => ({
      requestToken: crypto.randomUUID(),
      result: new Promise(() => {}),
    }),
  });
  const view = renderOverview(bridge, review);

  await waitFor(() => {
    const section = view.container.querySelector(".review-level-discussions");
    assert.equal(section.querySelectorAll(".notice-loading").length, 1);
  });
  await settle();
  const section = view.container.querySelector(".review-level-discussions");
  assert.equal(section.querySelectorAll(".notice-loading").length, 1);
  assert.equal(section.querySelectorAll(".notice-empty").length, 0);
  assert.equal(section.querySelectorAll(".notice-error").length, 0);
  assert.equal(
    section.textContent.includes("No review-level discussions yet."),
    false,
  );
});

test("an established absence of review-level notes is still said plainly", async () => {
  const review = "review-notes-empty";
  const view = renderOverview(reviewBridge(review), review);
  await view.findByText("No review-level discussions yet.");
  const section = view.container.querySelector(".review-level-discussions");
  assert.equal(section.querySelectorAll(".notice-empty").length, 1);
  assert.equal(section.querySelectorAll(".notice-error").length, 0);
});

test("a general entry is refused while the review's submission is in flight", async () => {
  const review = "review-general-during-submission";
  const saves = [];
  const stored = draft(review, 3, "");
  const bridge = reviewBridge(review, {
    listReviewDrafts: () =>
      read({ cursor: 0, next_cursor: null, drafts: [stored] }),
    saveReviewDraft: async (params) => {
      saves.push(params);
      return draft(review, params.expected_version + 1, params.content.body);
    },
  });

  // The state a Submit review press leaves behind: the draft is bound, the
  // attempt has frozen its version, and no progress has come back yet. This is
  // the state `canCaptureDraftInline` refuses for the in-diff composer.
  cacheWorkflow(
    review,
    beginSubmission(
      adoptDraft(
        createReviewWorkflowState(review, stored.revision),
        stored,
      ),
      "start",
    ),
  );

  const view = renderOverview(bridge, review);
  const composer = await view.findByLabelText("General review comment");
  fireEvent.change(composer, { target: { value: "note during submission" } });
  const primary = view.getByRole("button", { name: "Add to review" });
  await waitFor(() => assert.equal(primary.disabled, true));
  assert.equal(
    primary.title,
    "The pending review is bound to a durable submission attempt. Settle it in Your review before adding a general comment.",
  );
  fireEvent.click(primary);
  await settle(4);
  assert.equal(saves.length, 0);
});

test("the general composer buffer survives navigation and stays bound to its review", async () => {
  const review = "review-navigation-buffer";
  const bridge = reviewBridge(review);
  let view = renderOverview(bridge, review);
  fireEvent.change(await view.findByLabelText("General review comment"), {
    target: { value: "unsent general" },
  });
  view.unmount();

  view = renderOverview(bridge, review);
  assert.equal(
    (await view.findByLabelText("General review comment")).value,
    "unsent general",
  );
  view.unmount();

  // S43: the text returns only to the review it was typed on.
  const otherReview = "review-navigation-other";
  view = renderOverview(reviewBridge(otherReview), otherReview);
  assert.equal((await view.findByLabelText("General review comment")).value, "");
});

test("both Overview buttons write a general entry, never an inline one and never the Summary", async () => {
  const review = "review-general-entry";
  const saves = [];
  const creates = [];
  const posts = [];
  let version = 1;
  const bridge = reviewBridge(review, {
    createReviewDraft: async (params) => {
      creates.push(params);
      return draft(review, version, "");
    },
    saveReviewDraft: async (params) => {
      saves.push(params);
      version = params.expected_version + 1;
      return {
        ...draft(review, version, params.content.body),
        comments: params.content.comments,
        verdict: params.content.verdict,
      };
    },
    postReviewComment: async (params) => {
      posts.push(params);
      return mutation(params.operation_id);
    },
  });
  const view = renderOverview(bridge, review);

  // Start a review: the first press creates the draft and saves the entry.
  const composer = await view.findByLabelText("General review comment");
  fireEvent.change(composer, { target: { value: "First general note" } });
  fireEvent.click(await enabledButton(view, "Start a review"));
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(creates.length, 1);
  assert.equal(posts.length, 0);

  // Add to review: the primary changes only once a review is pending.
  await view.findByRole("button", { name: "Add to review" });
  fireEvent.change(view.getByLabelText("General review comment"), {
    target: { value: "Second general note" },
  });
  fireEvent.click(await enabledButton(view, "Add to review"));
  await waitFor(() => assert.equal(saves.length, 2));
  assert.equal(creates.length, 1);
  assert.equal(posts.length, 0);

  // S43: a general entry is not an inline entry and is not the Summary.
  const wire = JSON.stringify(saves[1].content);
  assert.equal(wire.includes('"kind":"general"'), true);
  assert.equal(wire.includes('"kind":"inline"'), false);
  assert.equal(wire.includes('"anchor"'), false);
  assert.equal(saves[1].content.body, "");
  assert.equal(saves[1].content.verdict, null);
  assert.deepEqual(
    saves[1].content.comments.map((entry) => entry.kind),
    ["general", "general"],
  );
  assert.deepEqual(
    saves[1].content.comments.map((entry) => entry.body),
    ["First general note", "Second general note"],
  );
  assert.equal(saves[1].expected_version, 2);
  await waitFor(() =>
    assert.equal(view.getByLabelText("General review comment").value, ""),
  );
  await view.findByText("Review in progress, 2 pending");
});

test("Add comment now posts a general comment immediately and writes no draft", async () => {
  const review = "review-general-quick";
  const posts = [];
  let drafts = 0;
  let saves = 0;
  const bridge = reviewBridge(review, {
    createReviewDraft: async () => {
      drafts += 1;
      return draft(review, 1, "");
    },
    saveReviewDraft: async (params) => {
      saves += 1;
      return draft(review, params.expected_version + 1, params.content.body);
    },
    postReviewComment: async (params) => {
      posts.push(params);
      return mutation(params.operation_id);
    },
  });
  const view = renderOverview(bridge, review);
  const composer = await view.findByLabelText("General review comment");
  fireEvent.change(composer, { target: { value: "Immediate general note" } });
  fireEvent.click(await enabledButton(view, "Add comment now"));
  await waitFor(() => assert.equal(posts.length, 1));
  assert.equal(posts[0].body, "Immediate general note");
  assert.equal(Object.hasOwn(posts[0], "anchor"), false);
  assert.equal(Object.hasOwn(posts[0], "revision"), false);
  assert.equal(drafts, 0);
  assert.equal(saves, 0);
  await waitFor(() =>
    assert.equal(view.getByLabelText("General review comment").value, ""),
  );
  // The primary stays the start of a review, because none was started.
  assert.equal(
    view.getByRole("button", { name: "Start a review" }).textContent,
    "Start a review",
  );
});

/**
 * The button under `name`, once the capability read behind it has answered. A
 * capability-gated control renders disabled while the read is in flight, and a
 * press on it does nothing, so waiting for the element alone is a race.
 */
/** Lets every already-dispatched read and its follow-on effects settle. */
async function settle(rounds = 20) {
  for (let round = 0; round < rounds; round += 1)
    await new Promise((resolve) => setTimeout(resolve, 5));
}

async function enabledButton(view, name) {
  return waitFor(() => {
    const found = view.getByRole("button", { name });
    assert.equal(found.disabled, false);
    return found;
  });
}

function renderFeature(bridge, review, contextChanges = {}) {
  const feature = createReviewFeature(bridge);
  const item = reviewItem(review);
  return render(
    feature.render(
      {
        bridge,
        queries: new QueryCoordinator(bridge),
        repositories: [
          { handle: "repo", display_name: "example/repo", forge_type: "github" },
        ],
        repositoriesReady: true,
        repositoryGeneration: 1,
        reviewPanels: [{ id: "discussions", label: "Discussions", order: 40 }],
        inlineAnchor: null,
        selectInlineAnchor: () => {},
        navigate: () => {},
        ...contextChanges,
      },
      { kind: "review", item, panel: "discussions" },
    ),
  );
}

function renderOverview(bridge, review, contextChanges = {}) {
  const feature = createReviewOverviewFeature();
  const item = reviewItem(review);
  return render(
    feature.render(
      {
        bridge,
        queries: new QueryCoordinator(bridge),
        repositories: [
          { handle: "repo", display_name: "example/repo", forge_type: "github" },
        ],
        repositoriesReady: true,
        repositoryGeneration: 1,
        reviewPanels: [{ id: "overview", label: "Overview", order: 10 }],
        inlineAnchor: null,
        selectInlineAnchor: () => {},
        navigate: () => {},
        ...contextChanges,
      },
      { kind: "review", item, panel: "overview" },
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
    getReviewSubmission: () => read(submission(review, "unknown")),
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
    openExternal: async () => true,
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

function draftInlineAnchor(stale) {
  return {
    revision: { head_sha: "head", base_sha: "base", start_sha: null },
    old_path: "src/example.py",
    new_path: "src/example.py",
    old_line: null,
    new_line: 4,
    side: "new",
    context_fingerprint: "a".repeat(64),
    start_line: null,
    start_side: null,
    stale,
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
