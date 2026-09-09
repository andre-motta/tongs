import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { createDiffFeature } from "../../../desktop/dist/src/renderer/features/diff/index.js";
import { clearPendingEdit } from "../../../desktop/dist/src/renderer/features/review/drawer.js";

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
const DRAFT_ID = "11111111-1111-4111-8111-111111111111";

/**
 * S54, end to end, on one review: start a review from the diff, add a second
 * entry as a suggestion, write the review's general comment as the drawer
 * Summary, choose the Comment verdict, and submit. Everything the store was
 * asked to hold is asserted as one string so the whole payload is the
 * assertion rather than a field of it.
 */
test("S54 runs from the in-diff composer through the drawer to a submitted comment verdict", async () => {
  const review = "review-drawer-s54";
  const saves = [];
  const submissions = [];
  const bridge = diffBridge(review, {
    saveReviewDraft: async (params) => {
      saves.push(params);
      return draft(review, params.expected_version + 1, params.content);
    },
    startReviewSubmission: async (params) => {
      submissions.push(params);
      return submission(review, "submitted", params.expected_version);
    },
  });
  const view = renderDiff(bridge, review, "gitlab");

  // Start a review from the line the reader clicked.
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitFor(() => assert.equal(saves.length, 1));

  // A suggestion on the same line enters the same pending review.
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  const composer = await view.findByLabelText("Inline review comment");
  fireEvent.change(composer, { target: { value: "Raise the builtin" } });
  fireEvent.click(view.getByRole("button", { name: "Insert suggestion" }));
  await waitFor(() =>
    assert.equal(
      view.getByLabelText("Inline review comment").value.includes("```suggestion"),
      true,
    ),
  );
  fireEvent.click(view.getByRole("button", { name: "Add to review" }));
  await waitFor(() => assert.equal(saves.length, 2));

  // The review's general comment and its verdict are the drawer's.
  fireEvent.click(view.getByRole("button", { name: /Your review/ }));
  fireEvent.change(await view.findByLabelText("Summary"), {
    target: { value: "Two small changes, otherwise good." },
  });
  fireEvent.click(view.getByRole("radio", { name: "Comment" }));
  fireEvent.click(view.getByRole("button", { name: "Submit review" }));
  fireEvent.click(
    await view.findByRole("button", { name: "Confirm submit review" }),
  );
  await waitFor(() => assert.equal(submissions.length, 1));

  // Submit saves the summary and the verdict first, so the submitted version
  // is the one the store confirmed rather than the one on screen.
  assert.equal(saves.length, 3);
  assert.equal(
    transcript(saves.at(-1).content),
    [
      "body=Two small changes, otherwise good.",
      "verdict=comment",
      "inline src/calc.py new:11 stale=false | Guard the zero divisor",
      "inline src/calc.py new:11 stale=false | Raise the builtin",
      "",
      "```suggestion:-0+0",
      "    if b == 0:",
      "```",
    ].join("\n"),
  );
  assert.equal(saves.at(-1).expected_version, 3);
  assert.equal(submissions[0].draft_id, DRAFT_ID);
  assert.equal(submissions[0].expected_version, 4);
  await view.findByText("Review submitted.");
});

/** S45: the identity, the captured revision and the version, in the UI. */
test("S45 the drawer footer names the draft, its revision and its version", async () => {
  const review = "review-drawer-footer";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 7, content({ body: "Held" }))],
        }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByText(`Draft ${DRAFT_ID}`);
  assert.ok(view.getByText("Revision f8bbf48"));
  assert.ok(view.getByText("Version 7"));
  assert.equal(
    view.container.querySelector(".review-drawer-footer [data-head-sha]")
      .getAttribute("data-head-sha"),
    REVISION.head_sha,
  );
});

/**
 * The drawer lists the whole review, including entries the diff never places:
 * a file outside the loaded diff, and a review-level general comment.
 */
test("the drawer groups every pending entry by file with its own count", async () => {
  const review = "review-drawer-groups";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              2,
              content({
                comments: [
                  inlineEntry("a", "First", 11, "src/calc.py"),
                  inlineEntry("b", "Second", 12, "src/calc.py"),
                  inlineEntry("c", "Elsewhere", 3, "docs/guide.md"),
                  { id: "d", kind: "general", body: "Thanks for splitting this out." },
                ],
              }),
            ),
          ],
        }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByLabelText("Pending comments");
  assert.deepEqual(
    [...view.container.querySelectorAll(".review-drawer-file-name")].map(
      (element) => element.textContent,
    ),
    ["src/calc.py2 pending", "docs/guide.md1 pending", "Review-level1 pending"],
  );
  assert.equal(
    view.container.querySelectorAll(".review-drawer-entry").length,
    4,
  );
  assert.equal(view.getByRole("button", { name: /Your review/ }).getAttribute("aria-label"), "Your review, 4 pending");

  // The file outside the loaded diff is listed and readable, and offers no
  // jump, because there is no loaded row to jump to.
  assert.ok(view.getByText("Elsewhere"));
  assert.ok(
    view.getByRole("button", {
      name: "Delete pending comment on docs/guide.md, new line 3",
    }),
  );
});

test("the summary and the verdict are saved on the draft's own version", async () => {
  const review = "review-drawer-save";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 4, content())] }),
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content);
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.change(await view.findByLabelText("Summary"), {
    target: { value: "Looks right to me." },
  });
  fireEvent.click(view.getByRole("radio", { name: "Approve" }));
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(saves[0].expected_version, 4);
  assert.equal(saves[0].draft_id, DRAFT_ID);
  assert.equal(saves[0].content.body, "Looks right to me.");
  assert.equal(saves[0].content.verdict, "approve");
  // The version the store confirmed is the one the footer now names.
  await view.findByText("Version 5");
  assert.equal(view.getByRole("radio", { name: "Approve" }).checked, true);
});

test("a verdict the forge or the permission withholds has no tile at all", async () => {
  const review = "review-drawer-verdict-caps";
  const view = renderDiff(
    diffBridge(review, {
      getReviewMutationCapabilities: () =>
        read({
          review,
          capabilities: capabilities({ approve: false, request_changes: false }),
        }),
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, content())] }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByLabelText("Summary");
  assert.deepEqual(
    view.getAllByRole("radio").map((element) => element.value),
    ["comment"],
  );
  // Nothing selected is the comment outcome, and it says so rather than
  // leaving it to be discovered on submit.
  await view.findByText(
    "No verdict selected. Submitting posts this review as a comment.",
  );
});

test("a forge that records no verdict says what submitting will do instead", async () => {
  const review = "review-drawer-no-verdict";
  const view = renderDiff(
    diffBridge(review, {
      getReviewMutationCapabilities: () =>
        read({
          review,
          capabilities: capabilities({
            approve: false,
            request_changes: false,
            comment_verdict: false,
          }),
        }),
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, content())] }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByText(
    "This review records no verdict on this forge or with this permission. Submitting posts the summary and the pending comments without one.",
  );
  assert.equal(view.container.querySelectorAll('input[type="radio"]').length, 0);
});

test("the drawer shows the engine's step list with each step's own standing", async () => {
  const review = "review-drawer-steps";
  const progress = {
    ...submission(review, "paused", 2),
    steps: [
      { id: "s1", kind: "inline_comment", comment_ids: ["a"] },
      { id: "s2", kind: "body", comment_ids: [] },
      { id: "s3", kind: "verdict", comment_ids: [] },
    ],
    completed_step_ids: ["s1"],
    unknown_step_ids: ["s2"],
  };
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 2, content())] }),
      startReviewSubmission: async () => progress,
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(await view.findByRole("button", { name: "Submit review" }));
  fireEvent.click(
    await view.findByRole("button", { name: "Confirm submit review" }),
  );
  await view.findByLabelText("Submission steps");
  assert.deepEqual(
    [...view.container.querySelectorAll(".review-drawer-steps li")].map(
      (element) => element.textContent,
    ),
    [
      "Inline comment: confirmed",
      "Review body: unknown",
      "Verdict: not started",
    ],
  );
  assert.ok(view.getByRole("button", { name: "Resume confirmed attempt" }));
});

test("a durable attempt is recovered inside the drawer without replaying start", async () => {
  const review = "review-drawer-recovery";
  let starts = 0;
  const attempt = submission(review, "unknown", 3);
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 3, content())] }),
      listReviewSubmissions: () =>
        read({ cursor: 0, next_cursor: null, attempts: [attempt] }),
      startReviewSubmission: async () => {
        starts += 1;
        return attempt;
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(
    await view.findByRole("button", {
      name: "Recover unknown attempt with 0 confirmed step(s)",
    }),
  );
  await view.findByText(/Remote status is unknown/);
  assert.equal(starts, 0);
  assert.ok(view.getByRole("button", { name: "Retry only remaining steps" }));
  assert.ok(view.getByRole("button", { name: "Return draft to editing" }));
});

test("a conflict raised by a drawer save is resolved in the drawer", async () => {
  const review = "review-drawer-conflict";
  const saves = [];
  const stored = draft(review, 5, content({ body: "Written elsewhere" }));
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 3, content())] }),
      getReviewDraft: () => read(stored),
      saveReviewDraft: async (params) => {
        saves.push(params);
        if (params.expected_version === 3)
          throw {
            code: "conflict",
            message: "The stored draft moved on.",
            retryable: false,
          };
        return draft(review, params.expected_version + 1, params.content);
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.change(await view.findByLabelText("Summary"), {
    target: { value: "My text" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Save summary and verdict" }),
  );
  await view.findByText(/The stored draft is now version 5/);
  assert.equal(
    view.getByLabelText("My unsaved draft text").value.includes("My text"),
    true,
  );
  assert.equal(
    view.getByLabelText("Stored draft text").value.includes("Written elsewhere"),
    true,
  );

  // Keeping my text re-saves it over the stored version rather than asserting
  // a version that a stale holder could never satisfy.
  fireEvent.click(
    view.getByRole("button", {
      name: "Keep my text and save over version 5",
    }),
  );
  await waitFor(() => assert.equal(saves.length, 2));
  assert.equal(saves[1].expected_version, 5);
  assert.equal(saves[1].content.body, "My text");
  await view.findByText("Version 6");
  assert.equal(
    view.container.querySelectorAll(".review-workflow-conflict").length,
    0,
  );
});

test("Escape closes the drawer and hands focus back to the button", async () => {
  const review = "review-drawer-escape";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 1, content())] }),
    }),
    review,
  );
  const toggle = await view.findByRole("button", { name: /Your review/ });
  fireEvent.click(toggle);
  await view.findByRole("dialog", { name: "Your review" });
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  fireEvent.keyDown(view.getByRole("dialog", { name: "Your review" }), {
    key: "Escape",
  });
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".review-drawer").length, 0),
  );
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(
    document.activeElement.getAttribute("aria-label"),
    "Your review, 0 pending",
  );
});

test("Edit in the drawer opens the in-diff composer in its edit shape", async () => {
  const review = "review-drawer-edit";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              2,
              content({
                comments: [inlineEntry("a", "Guard this", 11, "src/calc.py")],
              }),
            ),
          ],
        }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(
    await view.findByRole("button", {
      name: "Edit pending comment on src/calc.py, new line 11",
    }),
  );
  // The drawer closes and the composer opens on the entry's own row, in the
  // edit shape rather than as a new comment on the same line.
  await view.findByLabelText("Edit pending comment composer");
  assert.equal(view.container.querySelectorAll(".review-drawer").length, 0);
  assert.equal(view.getByLabelText("Pending review comment").value, "Guard this");
  assert.ok(view.getByText("Editing pending comment"));
  assert.equal(
    view.container.querySelectorAll(".inline-composer").length,
    1,
  );
});

test("Delete in the drawer removes the entry from the store and from the diff", async () => {
  const review = "review-drawer-delete";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              2,
              content({
                comments: [
                  inlineEntry("a", "Guard this", 11, "src/calc.py"),
                  inlineEntry("b", "And this", 12, "src/calc.py"),
                ],
              }),
            ),
          ],
        }),
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content);
      },
    }),
    review,
  );
  // The in-diff card and the drawer read one review, so what one removes the
  // other stops showing without a reload.
  await view.findByLabelText("Pending review comment on new line 11");
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  fireEvent.click(
    await view.findByRole("button", {
      name: "Delete pending comment on src/calc.py, new line 11",
    }),
  );
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(saves[0].expected_version, 2);
  assert.equal(
    transcript(saves[0].content),
    ["body=", "verdict=none", "inline src/calc.py new:12 stale=false | And this"].join(
      "\n",
    ),
  );
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll(".review-drawer-entry").length,
      1,
    ),
  );
  assert.equal(
    view.container.querySelectorAll(
      '.pending-card[aria-label="Pending review comment on new line 11"]',
    ).length,
    0,
  );
});

/**
 * The #190 carry over, F3 and F7: the diff places a stale card on whatever
 * current row now happens to carry the stored line number, and it renders no
 * card at all for an entry whose row is not loaded. The drawer is the listing
 * that neither implies a row nor omits an entry, so it lists the stale one by
 * its stored line and offers no jump to a row it cannot vouch for.
 */
test("a stale entry is listed by its stored line and offers no jump or edit", async () => {
  const review = "review-drawer-stale";
  const stale = inlineEntry("a", "Was on the old line", 11, "src/calc.py");
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              3,
              content({
                comments: [
                  {
                    ...stale,
                    anchor: { ...stale.anchor, stale: true },
                  },
                  inlineEntry("b", "Still current", 12, "src/calc.py"),
                ],
              }),
            ),
          ],
        }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByLabelText("Pending comments");
  await view.findByText(
    "Stale, was line 11 at revision f8bbf48. It is listed here by the line it was stored against, and offers no jump, because no row of the current diff is known to be that line.",
  );

  // Nothing is retargeted and nothing is hidden: the entry is listed, its
  // text is readable, and only the two actions that would need a current row
  // are withheld.
  // The diff still shows its own stale card, so the text is on screen twice:
  // once where the diff guesses a row, once where the drawer states the line.
  assert.equal(view.getAllByText("Was on the old line").length, 2);
  assert.equal(
    view.container.querySelectorAll(".review-drawer-entry-stale").length,
    1,
  );
  assert.equal(
    view.container.querySelectorAll(
      '.review-drawer-entry-stale button[aria-label^="Jump"]',
    ).length,
    0,
  );
  assert.equal(
    view.container.querySelectorAll(
      '.review-drawer-entry-stale button[aria-label^="Edit"]',
    ).length,
    0,
  );
  assert.ok(
    view.getByRole("button", {
      name: "Delete pending comment on src/calc.py, new line 11",
    }),
  );

  // A stale entry blocks submission, and the refusal names that cause rather
  // than a reused sentence.
  assert.equal(
    view.getByRole("button", { name: "Submit review" }).getAttribute("title"),
    "This review is bound to an earlier revision. Create a current-revision draft before submitting it.",
  );
});

/**
 * `review.discard_draft` already exists in the protocol and the TUI already
 * binds it, so the drawer offers it here rather than leaving the review with
 * no way out. It is confirmed inline, and the confirmation says exactly what
 * is being destroyed.
 */
test("Discard review is confirmed inline and then drops the pending review", async () => {
  const review = "review-drawer-discard";
  const discards = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(
              review,
              4,
              content({
                body: "Nearly there",
                comments: [inlineEntry("a", "Guard this", 11, "src/calc.py")],
              }),
            ),
          ],
        }),
      discardReviewDraft: async (params) => {
        discards.push(params);
        return { discarded: draft(review, 4, content()) };
      },
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: /Your review/ }));
  await view.findByLabelText("Summary");

  // One press asks, it does not act.
  fireEvent.click(view.getByRole("button", { name: "Discard review" }));
  await view.findByText(
    "Discard 1 pending comment(s) and the summary? This cannot be undone.",
  );
  assert.equal(discards.length, 0);
  fireEvent.click(
    view.getByRole("button", {
      name: "Confirm discard of 1 pending comment(s) and the summary",
    }),
  );
  await waitFor(() => assert.equal(discards.length, 1));
  assert.equal(discards[0].draft_id, DRAFT_ID);
  assert.equal(discards[0].expected_version, 4);

  // The review returns to its no-draft state on both surfaces at once.
  await view.findByText(
    "No pending review is open. Start one from a diff line, or choose a preserved draft below.",
  );
  assert.equal(
    view.getByRole("button", { name: /Your review/ }).getAttribute("aria-label"),
    "Your review, 0 pending",
  );
  assert.equal(view.container.querySelectorAll(".pending-card").length, 0);
});

/**
 * The carry over from #190: mount adoption and the first bind used to ask the
 * sidecar the same question twice. One shared read now answers both, and the
 * drawer's own candidate list.
 */
test("one active-draft read answers mount adoption, the drawer and the first bind", async () => {
  const review = "review-drawer-one-read";
  let reads = 0;
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () => {
        reads += 1;
        return read({ cursor: 0, next_cursor: null, drafts: [] });
      },
    }),
    review,
  );
  await view.findByRole("button", { name: "Comment on new line 11" });
  await view.findByRole("button", { name: /Your review/ });
  await waitFor(() => assert.equal(reads >= 1, true));
  assert.equal(reads, 1);
});

function transcript(content) {
  return [
    `body=${content.body}`,
    `verdict=${content.verdict ?? "none"}`,
    ...content.comments.map((comment) =>
      comment.kind === "inline"
        ? `inline ${comment.anchor.new_path} ${comment.anchor.side}:${comment.anchor.new_line} stale=${comment.anchor.stale === true} | ${comment.body}`
        : `${comment.kind} | ${comment.body}`,
    ),
  ].join("\n");
}

function renderDiff(bridge, review, forge = "github") {
  const feature = createDiffFeature();
  const route = { kind: "review", item: reviewItem(review), panel: "diff" };
  function Harness() {
    const [inlineAnchor, setInlineAnchor] = React.useState(null);
    const [current, setCurrent] = React.useState(route);
    const queries = React.useMemo(() => new QueryCoordinator(bridge), []);
    return feature.render(
      {
        bridge,
        queries,
        repositories: [
          { handle: "repo", display_name: "example/repo", forge_type: forge },
        ],
        repositoriesReady: true,
        repositoryGeneration: 1,
        reviewPanels: [{ id: "diff", label: "Files changed", order: 20 }],
        inlineAnchor,
        selectInlineAnchor: setInlineAnchor,
        navigate: setCurrent,
      },
      current,
    );
  }
  return render(React.createElement(Harness));
}

function diffBridge(review, changes = {}) {
  return {
    cancelRead: async () => true,
    openExternal: async () => true,
    openDiff: (params) => read(diffPage(review, params.layout ?? "unified")),
    pageDiff: () => {
      throw new Error("the fixture diff has one page");
    },
    getReviewMutationCapabilities: () =>
      read({ review, capabilities: capabilities() }),
    listDiscussions: () => read({ discussions: [] }),
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [] }),
    listReviewSubmissions: () =>
      read({ cursor: 0, next_cursor: null, attempts: [] }),
    getReviewSubmission: () => read(submission(review, "unknown", 1)),
    getReviewDraft: () => read(draft(review, 1, content())),
    createReviewDraft: async () => draft(review, 1, content()),
    saveReviewDraft: async (params) =>
      draft(review, params.expected_version + 1, params.content),
    discardReviewDraft: async () => ({ discarded: draft(review, 1, content()) }),
    startReviewSubmission: async (params) =>
      submission(review, "submitted", params.expected_version),
    resumeReviewSubmission: async () => submission(review, "submitted", 1),
    reconcileReviewSubmission: async () => submission(review, "submitted", 1),
    postInlineReviewComment: async (params) => mutation(params.operation_id),
    ...changes,
  };
}

function read(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
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

function content(changes = {}) {
  return { body: "", verdict: null, comments: [], ...changes };
}

function inlineEntry(id, body, newLine, path) {
  return {
    id,
    kind: "inline",
    body,
    anchor: {
      revision: REVISION,
      old_path: path,
      new_path: path,
      old_line: null,
      new_line: newLine,
      side: "new",
      context_fingerprint: "f".repeat(64),
      start_line: null,
      start_side: null,
      stale: false,
    },
  };
}

function draft(review, version, body) {
  return {
    id: DRAFT_ID,
    review,
    revision: REVISION,
    version,
    body: body.body ?? "",
    verdict: body.verdict ?? null,
    comments: body.comments ?? [],
    state: "editable",
    created_at: "2026-09-09T00:00:00+00:00",
    updated_at: "2026-09-09T00:00:00+00:00",
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

function submission(review, outcome, frozenVersion) {
  return {
    attempt_id: "22222222-2222-4222-8222-222222222222",
    draft_id: DRAFT_ID,
    review,
    frozen_version: frozenVersion,
    state: outcome === "submitted" ? "submitted" : outcome,
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
    deletions: 1,
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
    header: "@@ -10,3 +10,3 @@",
    old_start: 10,
    old_count: 3,
    new_start: 10,
    new_count: 3,
    context_text: "def divide(a, b):",
  };
  const sources = [
    { old_line: 10, new_line: 10, content: "def divide(a, b):", line_type: "context" },
    { old_line: null, new_line: 11, content: "    if b == 0:", line_type: "addition" },
    { old_line: null, new_line: 12, content: "        raise ValueError", line_type: "addition" },
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
    snapshot_id: `snapshot-${layout}`,
    resource: review,
    revision: REVISION,
    cursor: 0,
    next_cursor: null,
    entries: [file, hunk, ...entries],
  };
}
