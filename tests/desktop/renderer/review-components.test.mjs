import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import {
  allocateDiscussionMarkdown,
  createReviewFeature,
  discussionDiffTarget,
} from "../../../desktop/dist/src/renderer/features/review/index.js";

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

test("quick GitHub suggestion preserves its buffer across cancel and unknown delivery", async () => {
  const review = "review-quick-suggestion";
  const writes = [];
  const bridge = reviewBridge(review, {
    postInlineReviewComment: async (params) => {
      writes.push(params);
      throw {
        code: "mutation_timeout",
        message: "The mutation response timed out.",
        retryable: true,
      };
    },
  });
  const selected = inlineSelection(review, "head", 4, [
    sourceLine(3, 3, "  first()"),
    sourceLine(null, 4, "  second()", "addition"),
  ]);
  const view = renderFeature(bridge, review, { inlineAnchor: selected });
  const replacement = await view.findByLabelText("Suggestion replacement code");
  assert.equal(replacement.value, "  first()\n  second()");
  fireEvent.change(view.getByLabelText("Suggestion explanation"), {
    target: { value: "Keep this precise" },
  });
  fireEvent.change(replacement, {
    target: { value: "  use(`value`)  " },
  });
  fireEvent.click(view.getByRole("button", { name: "Cancel and keep text" }));
  fireEvent.click(view.getByRole("button", { name: "Suggest replacement" }));
  assert.equal(view.getByLabelText("Suggestion replacement code").value, "  use(`value`)  ");
  fireEvent.click(view.getByRole("button", { name: "Post quick suggestion" }));
  await view.findByText(/may have completed remotely/);
  assert.equal(writes.length, 1);
  assert.deepEqual(writes[0].anchor, {
    old_path: "src/example.py",
    new_path: "src/example.py",
    line: 4,
    side: "RIGHT",
    start_line: 3,
    start_side: "RIGHT",
  });
  assert.equal(
    writes[0].body,
    "Keep this precise\n\n```suggestion\n  use(`value`)  \n```",
  );
  fireEvent.click(
    view.getByRole("button", {
      name: "I inspected the forge; acknowledge uncertainty",
    }),
  );
  await view.findByLabelText("Suggestion replacement code");
  assert.equal(view.getByLabelText("Suggestion replacement code").value, "  use(`value`)  ");
  assert.equal(writes.length, 1);
});

test("GitLab suggestion enters the durable draft and saves without a direct forge write", async () => {
  const review = "review-draft-suggestion";
  const saves = [];
  let saveAttempts = 0;
  let quickWrites = 0;
  const bridge = reviewBridge(review, {
    postInlineReviewComment: async (params) => {
      quickWrites += 1;
      return mutation(params.operation_id);
    },
    saveReviewDraft: async (params) => {
      saveAttempts += 1;
      saves.push(params);
      if (saveAttempts === 1)
        throw {
          code: "conflict",
          message: "The durable draft changed elsewhere.",
          retryable: true,
        };
      return {
        ...draft(review, params.expected_version + 1, params.content.body),
        comments: params.content.comments.map((comment) => ({
          ...comment,
          anchor:
            comment.kind === "inline"
              ? { ...comment.anchor, stale: false }
              : undefined,
        })),
      };
    },
    getReviewDraft: () => read(draft(review, 2, "remote change")),
  });
  const selected = inlineSelection(review, "head", 8, [
    sourceLine(7, 7, "first"),
    sourceLine(8, 8, "second"),
  ]);
  const view = renderFeature(bridge, review, {
    inlineAnchor: selected,
    repositories: [
      { handle: "repo", display_name: "example/repo", forge_type: "gitlab" },
    ],
  });
  fireEvent.click(await view.findByRole("button", { name: "Start review" }));
  await view.findByText("Draft review active");
  fireEvent.change(view.getByLabelText("Suggestion replacement code"), {
    target: { value: "replacement" },
  });
  fireEvent.click(view.getByRole("button", { name: "Add suggestion to draft" }));
  assert.equal(quickWrites, 0);
  const draftComment = await view.findByLabelText(/Edit draft comment/);
  assert.equal(draftComment.value, "```suggestion:-0+1\nreplacement\n```");
  fireEvent.click(view.getByRole("button", { name: "Save draft" }));
  await view.findByText(/now version 2, and you were editing version 1/);
  assert.equal(draftComment.value, "```suggestion:-0+1\nreplacement\n```");
  fireEvent.click(
    view.getByRole("button", { name: "Keep my text and save over version 2" }),
  );
  await waitFor(() => assert.equal(saves.length, 2));
  assert.equal(saves[1].expected_version, 2);
  assert.equal(saves[1].content.comments.length, 1);
  assert.deepEqual(saves[0].content.comments[0].anchor, {
    revision: { head_sha: "head", base_sha: "base", start_sha: null },
    old_path: "src/example.py",
    new_path: "src/example.py",
    old_line: 7,
    new_line: 7,
    side: "new",
    context_fingerprint: saves[0].content.comments[0].anchor.context_fingerprint,
    start_line: null,
    start_side: null,
  });
  assert.deepEqual(saves[1].content.comments[0], saves[0].content.comments[0]);
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
  fireEvent.click(await view.findByRole("button", { name: "Resume review" }));
  await view.findByText("Stored version 1");
  assert.equal(counters.creates, 0);
  fireEvent.change(view.getByLabelText("Review body"), {
    target: { value: "local unsaved text" },
  });
  fireEvent.click(view.getByRole("button", { name: "Save draft" }));
  await view.findByText(/now version 2, and you were editing version 1/);
  return view;
}

function counters() {
  return { creates: 0, saves: 0, savedParams: [] };
}

test("a stale save shows both draft versions and neither is silently discarded", async () => {
  const state = counters();
  const view = await conflictedDraftView("review-draft-both", state);
  assert.equal(view.getByLabelText("Review body").value, "local unsaved text");
  assert.match(view.getByLabelText("My unsaved draft text").value, /local unsaved text/);
  const stored = view.getByLabelText("Stored draft text").value;
  assert.match(stored, /TUI edit/);
  assert.match(stored, /TUI comment/);
  assert.equal(view.getByRole("button", { name: "Save draft" }).disabled, true);
  assert.equal(view.getByRole("button", { name: "Submit review" }).disabled, true);
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
  assert.equal(view.getByLabelText("Review body").value, "local unsaved text");
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
  assert.equal(view.getByLabelText("Review body").value, "TUI edit");
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
      view.queryByLabelText("My superseded draft text replaced by version 2"),
      null,
    ));
  assert.equal(view.getByLabelText("Review body").value, "TUI edit");
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
  fireEvent.click(await view.findByRole("button", { name: "Resume review" }));
  await view.findByText("Stored version 1");

  fireEvent.change(view.getByLabelText("Review body"), {
    target: { value: "first local" },
  });
  fireEvent.click(view.getByRole("button", { name: "Save draft" }));
  await view.findByText(/now version 2, and you were editing version 1/);
  fireEvent.click(
    view.getByRole("button", { name: "Take the stored version and keep mine to copy" }),
  );
  await view.findByLabelText("My superseded draft text replaced by version 2");

  fireEvent.change(view.getByLabelText("Review body"), {
    target: { value: "second local" },
  });
  fireEvent.click(view.getByRole("button", { name: "Save draft" }));
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
  assert.equal(view.getByLabelText("Review body").value, "stored four");

  fireEvent.click(
    view.getByRole("button", { name: "Dismiss my text replaced by version 2" }),
  );
  await waitFor(() =>
    assert.equal(
      view.queryByLabelText("My superseded draft text replaced by version 2"),
      null,
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
  fireEvent.click(await view.findByRole("button", { name: "Resume review" }));
  await view.findByText("Stored version 1");
  fireEvent.change(view.getByLabelText("Review body"), {
    target: { value: "my precious text" },
  });
  fireEvent.click(view.getByRole("button", { name: "Save draft" }));
  await view.findByText(/now version 2, and you were editing version 1/);

  fireEvent.click(
    view.getByRole("button", { name: "Keep my text and save over version 2" }),
  );
  await view.findByText(/already submitted/);
  assert.equal(saves, 1);
  assert.match(view.getByLabelText("Stored draft text").value, /submitted elsewhere/);
  assert.match(view.getByLabelText("My unsaved draft text").value, /my precious text/);
  assert.equal(view.getByLabelText("Review body").value, "my precious text");
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
  fireEvent.click(await view.findByRole("button", { name: "Resume review" }));
  const submit = await view.findByRole("button", { name: "Submit review" });
  fireEvent.click(submit);
  assert.equal(starts, 0);
  fireEvent.click(view.getByRole("button", { name: "Confirm submit review" }));
  await view.findByText(/Remote status is unknown/);
  assert.equal(starts, 1);
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
  fireEvent.click(await view.findByRole("button", { name: "Resume review" }));
  fireEvent.click(await view.findByRole("button", { name: "Submit review" }));
  fireEvent.click(view.getByRole("button", { name: "Confirm submit review" }));
  await view.findByText(/Remote status is unknown/);
  assert.equal(starts, 1);
  assert.equal(lists, 2);
});

test("discussion controls explain unsupported actions and support keyboard focus", async () => {
  const review = "review-discussion";
  let resolves = 0;
  const navigated = [];
  const bridge = reviewBridge(review, {
    listDiscussions: () => read({ discussions: [discussion()] }),
    getReviewMutationCapabilities: () =>
      read({ review, capabilities: capabilities({ reply: false, resolve: true }) }),
    resolveReviewDiscussion: async (params) => {
      resolves += 1;
      return mutation(params.operation_id);
    },
  });
  const view = renderFeature(bridge, review, {
    navigate: (route) => navigated.push(route),
  });
  const reply = await view.findByRole("button", { name: "Reply" });
  const resolve = view.getByRole("button", { name: "Resolve" });
  const show = view.getByRole("button", { name: "Show in diff" });
  fireEvent.click(show);
  assert.deepEqual(navigated[0].diffTarget, {
    discussionId: "thread-1",
    path: "src/example.py",
    side: "new",
    line: 4,
  });
  assert.equal(reply.disabled, true);
  assert.match(reply.title, /unsupported/);
  const list = view.container.querySelector(".review-workflow-thread-list");
  fireEvent.keyDown(list, { key: "ArrowDown" });
  assert.equal(document.activeElement, show);
  fireEvent.click(resolve);
  await waitFor(() => assert.equal(resolves, 1));
  await view.findByRole("button", { name: "Reopen thread" });
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

test("a review-level note reads with no inline position", async () => {
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

  const view = renderFeature(bridge, review);

  const threads = await waitFor(() => {
    const found = view.container.querySelectorAll(".review-workflow-thread");
    assert.equal(found.length, 2);
    return found;
  });
  const meta = threads[0].querySelector(".review-workflow-thread-meta");
  assert.equal(meta.textContent, "Reviewer");
  assert.match(
    threads[1].querySelector(".review-workflow-thread-meta").textContent,
    /src\/example\.py/,
  );
  assert.equal(
    [...threads[0].querySelectorAll("button")].some(
      (button) => button.textContent === "Show in diff",
    ),
    false,
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
  const bridge = reviewBridge(review, {
    listReviewDrafts: () => read({
      cursor: 0,
      next_cursor: null,
      drafts: [{ ...draft(review, 1, "body"), comments }],
    }),
  });
  const view = renderFeature(bridge, review);
  fireEvent.click(await view.findByRole("button", { name: "Resume review" }));
  await view.findByText(/src\/example.py · new line 4/);
  await view.findByText(/Reply .* to discussion thread-1/);
  const inlineEditor = view.getByLabelText(`Edit draft comment ${comments[1].id}`);
  fireEvent.change(inlineEditor, { target: { value: "" } });
  await view.findByText("Edit or remove empty draft comments before saving.");
  assert.equal(view.getByRole("button", { name: "Save draft" }).disabled, true);
  const removes = view.getAllByRole("button", { name: "Remove draft comment" });
  fireEvent.click(removes[1]);
  assert.equal(view.queryByLabelText(`Edit draft comment ${comments[1].id}`), null);
  assert.equal(view.getByRole("button", { name: "Save draft" }).disabled, false);
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
  fireEvent.click(await view.findByRole("button", { name: "Resume review" }));
  await view.findByText(/remains preserved at revision old-head/);
  assert.equal(view.getByRole("button", { name: "Submit review" }).disabled, true);
  assert.equal(view.getByLabelText("Add selected line to draft").disabled, true);
  fireEvent.click(view.getByRole("button", { name: "Create current-revision draft" }));
  assert.equal(creates.length, 0);
  fireEvent.click(view.getByRole("button", { name: "Confirm create separate current-revision draft" }));
  await waitFor(() => assert.equal(creates.length, 1));
  assert.equal(creates[0].revision.head_sha, "new-head");
  assert.deepEqual(creates[0].content.comments.map((item) => item.kind), ["general"]);
  await view.findByText("Keep this inline text");
  await view.findByText("Keep this reply");
  await view.findByText(/Preserved old draft 11111111/);
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
  fireEvent.click(await view.findByRole("button", {
    name: "Recover unknown attempt with 0 confirmed step(s)",
  }));
  await waitFor(() => assert.equal(loads.length, 1));
  assert.equal(loads[0].draft_id, otherId);
  assert.equal(view.getByLabelText("Review body").value, "matching recovered draft");
  await view.findByText(/Remote status is unknown/);
  assert.equal(view.queryByDisplayValue("first draft"), null);
});

test("composer buffers survive panel navigation and remain bound to review and anchor", async () => {
  const review = "review-navigation-buffer";
  const anchor = inlineSelection(review, "head", 4);
  const bridge = reviewBridge(review);
  let view = renderFeature(bridge, review, { inlineAnchor: anchor });
  const general = await view.findByLabelText("Quick comment");
  const inline = view.getByLabelText("Quick inline comment");
  fireEvent.change(general, { target: { value: "unsent general" } });
  fireEvent.change(inline, { target: { value: "unsent inline" } });
  view.unmount();

  view = renderFeature(bridge, review, { inlineAnchor: anchor });
  assert.equal((await view.findByLabelText("Quick comment")).value, "unsent general");
  assert.equal(view.getByLabelText("Quick inline comment").value, "unsent inline");
  view.unmount();

  const changedAnchor = inlineSelection(review, "new-head", 8);
  view = renderFeature(bridge, review, { inlineAnchor: changedAnchor });
  assert.equal((await view.findByLabelText("Quick inline comment")).value, "");
  await view.findByText("unsent inline");
  view.unmount();

  const otherReview = "review-navigation-other";
  view = renderFeature(reviewBridge(otherReview), otherReview, {
    inlineAnchor: inlineSelection(otherReview, "head", 4),
  });
  assert.equal((await view.findByLabelText("Quick comment")).value, "");
  assert.equal(view.getByLabelText("Quick inline comment").value, "");
});

test("the panel's inline comment carries the range the diff selected", async () => {
  const review = "review-panel-range";
  const posts = [];
  const bridge = reviewBridge(review, {
    postInlineReviewComment: async (params) => {
      posts.push(params);
      return mutation(params.operation_id);
    },
  });
  const range = inlineSelection(review, "head", 6, [
    sourceLine(null, 4, "  first()", "addition"),
    sourceLine(null, 5, "  second()", "addition"),
    sourceLine(null, 6, "  third()", "addition"),
  ]);
  const view = renderFeature(bridge, review, { inlineAnchor: range });
  const inline = await view.findByLabelText("Quick inline comment");
  assert.equal(inline.disabled, false);
  fireEvent.change(inline, { target: { value: "Three statements" } });
  fireEvent.click(view.getByRole("button", { name: "Quick inline comment" }));
  await waitFor(() => assert.equal(posts.length, 1));
  assert.deepEqual(posts[0].anchor, {
    old_path: "src/example.py",
    new_path: "src/example.py",
    line: 6,
    side: "RIGHT",
    start_line: 4,
    start_side: "RIGHT",
  });
});

test("the panel refuses a range when the forge has no multiline capability", async () => {
  const review = "review-panel-no-multiline";
  const posts = [];
  const bridge = reviewBridge(review, {
    getReviewMutationCapabilities: () =>
      read({ review, capabilities: capabilities({ multiline_comment: false }) }),
    postInlineReviewComment: async (params) => {
      posts.push(params);
      return mutation(params.operation_id);
    },
  });
  const range = inlineSelection(review, "head", 5, [
    sourceLine(null, 4, "  first()", "addition"),
    sourceLine(null, 5, "  second()", "addition"),
  ]);
  const view = renderFeature(bridge, review, { inlineAnchor: range });
  const inline = await view.findByLabelText("Quick inline comment");
  await waitFor(() => assert.equal(inline.disabled, true));
  const refusal =
    "Multi-line comments are unsupported for this review. Select a single line.";
  assert.equal(view.getByText(refusal).textContent, refusal);
  assert.equal(posts.length, 0);
});

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

function inlineSelection(
  review,
  head,
  line,
  selectedLines = [sourceLine(null, line, "selected", "addition")],
) {
  const last = selectedLines.at(-1);
  return {
    review,
    snapshotId: "snapshot",
    resource: review,
    revision: { head_sha: head, base_sha: "base", start_sha: null },
    fileIndex: 0,
    hunkIndex: 0,
    rowIndex: null,
    oldPath: "src/example.py",
    newPath: "src/example.py",
    side: "new",
    oldLine: last.oldLine,
    newLine: last.newLine,
    lineType: last.lineType,
    contextLines: ["before", "selected", "after"],
    contextComplete: true,
    rangeOriginOldLine: selectedLines[0].oldLine,
    rangeOriginNewLine: selectedLines[0].newLine,
    selectedLines,
  };
}

function sourceLine(oldLine, newLine, content, lineType = "context") {
  return { oldLine, newLine, content, lineType };
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
