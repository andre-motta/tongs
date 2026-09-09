import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { createDiffFeature } from "../../../desktop/dist/src/renderer/features/diff/index.js";

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

const REVISION = {
  head_sha: "f8bbf4877cd1a58f2a3a9d1a1a86bd2f4b7a1c33",
  base_sha: "df2bd3f",
  start_sha: null,
};

test("the gutter affordance opens one in-diff composer under its own row", async () => {
  const review = "review-composer-open";
  const view = renderDiff(diffBridge(review), review);
  await view.findByRole("button", { name: "Comment on new line 11" });
  assert.equal(view.queryByLabelText("Inline comment composer"), null);
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  await view.findByLabelText("Inline comment composer");
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 1);
  assert.equal(
    view.getByText("src/calc.py, new line 11").tagName.toLowerCase(),
    "strong",
  );

  // The composer is a row of the diff, so the rows below it are pushed down
  // rather than covered.
  assert.deepEqual(
    [...view.container.querySelectorAll(".windowed-items > *")].map((element) =>
      element.className.split(" ")[0],
    ),
    [
      "diff-file",
      "diff-hunk",
      "diff-line",
      "diff-line",
      "inline-composer-row",
      "diff-line",
    ],
  );

  // Exactly two fixed actions plus Cancel, and no relabelling in place.
  assert.equal(view.getByRole("button", { name: "Start a review" }).disabled, true);
  assert.equal(
    view.getByRole("button", { name: "Add comment now" }).disabled,
    true,
  );
  assert.equal(view.queryByRole("button", { name: "Add to review" }), null);
  assert.equal(view.queryByText(/Review in progress/), null);
  fireEvent.change(view.getByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  assert.equal(
    view.getByRole("button", { name: "Start a review" }).disabled,
    false,
  );
  assert.equal(
    view.getByRole("button", { name: "Add comment now" }).disabled,
    false,
  );
  assert.equal(view.getByRole("button", { name: "Cancel" }).disabled, false);
});

test("the keyboard opens the composer, takes focus, closes on Escape, and fires the primary action", async () => {
  const review = "review-composer-keyboard";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content.comments);
      },
    }),
    review,
  );
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll('.line-content[role="button"]').length,
      3,
    ),
  );
  const lines = view.container.querySelectorAll('.line-content[role="button"]');
  fireEvent.keyDown(lines[1], { key: "c" });
  await view.findByLabelText("Inline comment composer");
  assert.equal(
    view.getByText("src/calc.py, new line 11").textContent,
    "src/calc.py, new line 11",
  );

  // The composer takes focus, so Escape and Ctrl+Enter reach it without a Tab.
  assert.equal(
    document.activeElement.getAttribute("aria-label"),
    "Inline review comment",
  );
  fireEvent.keyDown(document.activeElement, { key: "Escape" });
  assert.equal(view.queryByLabelText("Inline comment composer"), null);

  // Escape also closes it from the row that owns it.
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  await view.findByLabelText("Inline comment composer");
  assert.equal(
    document.activeElement.getAttribute("aria-label"),
    "Inline review comment",
  );
  lines[1].focus();
  assert.equal(
    document.activeElement.getAttribute("aria-label"),
    "Select new line 11",
  );
  fireEvent.keyDown(document.activeElement, { key: "Escape" });
  assert.equal(view.queryByLabelText("Inline comment composer"), null);

  fireEvent.keyDown(lines[1], { key: "c" });
  const editor = await view.findByLabelText("Inline review comment");
  fireEvent.change(editor, { target: { value: "Opened from the keyboard" } });
  fireEvent.keyDown(editor, { key: "Enter", ctrlKey: true });
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(saves[0].content.comments[0].body, "Opened from the keyboard");
  assert.equal(saves[0].content.comments[0].anchor.new_line, 11);
});

test("Start a review saves an inline entry for the row and then offers Add to review", async () => {
  const review = "review-composer-draft";
  const creates = [];
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      createReviewDraft: async (params) => {
        creates.push(params);
        return draft(review, 1, []);
      },
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content.comments);
      },
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(creates.length, 1);
  assert.equal(creates[0].revision.head_sha, REVISION.head_sha);
  assert.equal(saves[0].expected_version, 1);
  assert.equal(saves[0].content.comments.length, 1);
  const first = saves[0].content.comments[0];
  assert.equal(first.kind, "inline");
  assert.equal(first.body, "Guard the zero divisor");
  assert.equal(first.anchor.revision.head_sha, REVISION.head_sha);
  assert.equal(first.anchor.old_path, "src/calc.py");
  assert.equal(first.anchor.new_path, "src/calc.py");
  assert.equal(first.anchor.old_line, null);
  assert.equal(first.anchor.new_line, 11);
  assert.equal(first.anchor.side, "new");
  assert.equal(first.anchor.context_fingerprint.length, 64);
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".inline-composer").length, 0),
  );

  // A second anchor keeps its own entry, so no inline target is retargeted.
  fireEvent.click(view.getByRole("button", { name: "Comment on old line 12" }));
  await view.findByLabelText("Inline comment composer");
  assert.equal(view.getByText("Review in progress, 1 pending").tagName.toLowerCase(), "span");
  assert.equal(view.queryByRole("button", { name: "Start a review" }), null);
  assert.equal(
    view.getByRole("button", { name: "Add to review" }).disabled,
    true,
  );
  fireEvent.change(view.getByLabelText("Inline review comment"), {
    target: { value: "This branch is unreachable" },
  });
  fireEvent.click(view.getByRole("button", { name: "Add to review" }));
  await waitFor(() => assert.equal(saves.length, 2));
  assert.equal(creates.length, 1);
  assert.equal(saves[1].expected_version, 2);
  assert.deepEqual(
    saves[1].content.comments.map((comment) => comment.body),
    ["Guard the zero divisor", "This branch is unreachable"],
  );
  const second = saves[1].content.comments[1];
  assert.equal(second.anchor.old_line, 12);
  assert.equal(second.anchor.new_line, null);
  assert.equal(second.anchor.side, "old");
});

test("a retry after a failed save writes the entry once", async () => {
  const review = "review-composer-retry";
  const creates = [];
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      createReviewDraft: async (params) => {
        creates.push(params);
        return draft(review, 1, []);
      },
      saveReviewDraft: async (params) => {
        saves.push(params);
        if (saves.length === 1)
          throw {
            code: "network",
            message: "the save did not reach the service",
            retryable: true,
          };
        return draft(review, params.expected_version + 1, params.content.comments);
      },
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitFor(() => assert.equal(saves.length, 1));

  // The composer stays open with the text, so pressing the primary again is
  // the obvious next move.
  assert.equal(
    view.getByLabelText("Inline review comment").value,
    "Guard the zero divisor",
  );
  await waitFor(() =>
    assert.equal(
      view.getByRole("button", { name: "Add to review" }).disabled,
      false,
    ),
  );
  fireEvent.click(view.getByRole("button", { name: "Add to review" }));
  await waitFor(() => assert.equal(saves.length, 2));
  assert.equal(creates.length, 1);
  assert.equal(saves[1].expected_version, 1);
  assert.equal(
    saves[1].content.comments.map((comment) => comment.body).join(" | "),
    "Guard the zero divisor",
  );
});

test("a draft conflict refuses the primary action instead of appending again", async () => {
  const review = "review-composer-conflict";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      saveReviewDraft: async (params) => {
        saves.push(params);
        throw {
          code: "conflict",
          message: "the stored draft advanced",
          retryable: false,
        };
      },
      getReviewDraft: () => read(draft(review, 5, [])),
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitFor(() => assert.equal(saves.length, 1));
  await view.findByText(
    "This pending review was changed elsewhere. Resolve the conflict in the review workflow before adding inline feedback.",
  );
  assert.equal(
    view.getByRole("button", { name: "Add to review" }).disabled,
    true,
  );
  fireEvent.click(view.getByRole("button", { name: "Add to review" }));
  assert.equal(saves.length, 1);
  assert.equal(
    view.getByLabelText("Inline review comment").value,
    "Guard the zero divisor",
  );
});

test("a durable pending review is visible before the first press and is adopted, not duplicated", async () => {
  const review = "review-composer-adopt";
  const creates = [];
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 3, [inlineEntry("entry-a", "Written in the TUI", 11)])],
        }),
      createReviewDraft: async (params) => {
        creates.push(params);
        return draft(review, 1, []);
      },
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content.comments);
      },
    }),
    review,
  );
  // The draft was written elsewhere, so the state has to be visible before the
  // reader types anything: the card, the header count, the chip and the label.
  await view.findByLabelText("Pending review comment on new line 11");
  assert.equal(view.getByText("Written in the TUI").tagName.toLowerCase(), "p");
  assert.equal(view.getByText("1 pending").textContent, "1 pending");
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 10" }),
  );
  await view.findByLabelText("Inline comment composer");
  assert.equal(
    view.getByText("Review in progress, 1 pending").textContent,
    "Review in progress, 1 pending",
  );
  assert.equal(view.queryByRole("button", { name: "Start a review" }), null);
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  fireEvent.click(view.getByRole("button", { name: "Add to review" }));
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(creates.length, 0);
  assert.equal(saves[0].expected_version, 3);
  assert.equal(saves[0].content.comments.length, 2);
  assert.equal(saves[0].content.comments[0].body, "Written in the TUI");
  assert.equal(saves[0].content.comments[1].body, "Guard the zero divisor");
  // Add to review leaves its own pending card behind, next to the one that
  // came from the store.
  await view.findByLabelText("Pending review comment on new line 10");
  assert.equal(view.container.querySelectorAll(".pending-card").length, 2);
  assert.equal(view.getByText("2 pending").textContent, "2 pending");
});

test("several recovered pending reviews refuse in their own words", async () => {
  const review = "review-composer-several";
  const creates = [];
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(review, 1, []),
            { ...draft(review, 2, []), id: "22222222-2222-4222-8222-222222222222" },
          ],
        }),
      createReviewDraft: async (params) => {
        creates.push(params);
        return draft(review, 1, []);
      },
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content.comments);
      },
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await view.findByText(
    "Several pending reviews were recovered. Resume one in the review workflow before commenting.",
  );
  assert.equal(creates.length, 0);
  assert.equal(saves.length, 0);
});

test("a pending review bound to an earlier revision refuses in its own words", async () => {
  const review = "review-composer-stale";
  const saves = [];
  const stale = {
    ...draft(review, 2, []),
    revision: { head_sha: "old-head", base_sha: "df2bd3f", start_sha: null },
  };
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [stale] }),
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content.comments);
      },
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  // The mount read adopts the durable draft, so the refusal stands before the
  // press rather than after it, and the primary write is already disabled.
  await view.findByText(
    "The pending review is bound to an earlier revision. Migrate it in the review workflow before adding inline feedback.",
  );
  assert.equal(view.queryByRole("button", { name: "Start a review" }), null);
  assert.equal(
    view.getByRole("button", { name: "Add to review" }).disabled,
    true,
  );
  assert.equal(saves.length, 0);
});

test("a partial diff context refuses the draft path in its own words", async () => {
  const review = "review-composer-partial";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      openDiff: (params) => read(truncatedDiffPage(review, params.layout ?? "unified")),
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, params.content.comments);
      },
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  await view.findByText(
    "The selected context is partial. Refresh the complete diff before drafting.",
  );
  fireEvent.change(view.getByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  assert.equal(
    view.getByRole("button", { name: "Start a review" }).disabled,
    true,
  );
  assert.equal(
    view.getByRole("button", { name: "Add comment now" }).disabled,
    false,
  );
  assert.equal(saves.length, 0);
});

test("the gutter keeps an existing multi-line selection and the composer claims it", async () => {
  const review = "review-composer-range";
  const view = renderDiff(diffBridge(review), review);
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll('.line-content[role="button"]').length,
      3,
    ),
  );
  const lines = view.container.querySelectorAll('.line-content[role="button"]');
  fireEvent.click(lines[0]);
  fireEvent.click(lines[1], { shiftKey: true });
  assert.equal(view.container.querySelectorAll(".line-selected").length, 2);
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  await view.findByLabelText("Inline comment composer");
  assert.equal(
    view.getByText("src/calc.py, Lines 10 to 11 (new)").textContent,
    "src/calc.py, Lines 10 to 11 (new)",
  );
  assert.equal(view.container.querySelectorAll(".line-selected").length, 2);
});

test("Add comment now posts the immediate inline mutation and writes no draft", async () => {
  const review = "review-composer-quick";
  const posts = [];
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      postInlineReviewComment: async (params) => {
        posts.push(params);
        return mutation(params.operation_id);
      },
      saveReviewDraft: async (params) => {
        saves.push(params);
        return draft(review, params.expected_version + 1, []);
      },
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Immediate note" },
  });
  fireEvent.click(view.getByRole("button", { name: "Add comment now" }));
  await waitFor(() => assert.equal(posts.length, 1));
  assert.equal(saves.length, 0);
  assert.equal(posts[0].body, "Immediate note");
  assert.equal(posts[0].revision.head_sha, REVISION.head_sha);
  assert.deepEqual(posts[0].anchor, {
    old_path: "src/calc.py",
    new_path: "src/calc.py",
    line: 11,
    side: "RIGHT",
  });
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".inline-composer").length, 0),
  );
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  assert.equal((await view.findByLabelText("Inline review comment")).value, "");
});

test("Cancel and Esc close the composer and keep the text on that anchor alone", async () => {
  const review = "review-composer-cancel";
  const view = renderDiff(diffBridge(review), review);
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Kept through cancel" },
  });
  fireEvent.click(view.getByRole("button", { name: "Cancel" }));
  assert.equal(view.queryByLabelText("Inline comment composer"), null);

  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  assert.equal(
    (await view.findByLabelText("Inline review comment")).value,
    "Kept through cancel",
  );
  fireEvent.keyDown(view.getByLabelText("Inline review comment"), {
    key: "Escape",
  });
  assert.equal(view.queryByLabelText("Inline comment composer"), null);

  fireEvent.click(view.getByRole("button", { name: "Comment on old line 12" }));
  assert.equal((await view.findByLabelText("Inline review comment")).value, "");
  fireEvent.click(view.getByRole("button", { name: "Cancel" }));
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  assert.equal(
    (await view.findByLabelText("Inline review comment")).value,
    "Kept through cancel",
  );
});

test("the split layout keeps one composer and aligns the other pane with a spacer", async () => {
  const review = "review-composer-split";
  const view = renderDiff(diffBridge(review), review);
  fireEvent.click(await view.findByText("Split"));
  await waitFor(() =>
    assert.ok(
      view.container.querySelector('.split-cell[data-anchor-side="new"]'),
    ),
  );
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  await view.findByLabelText("Inline comment composer");
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 1);
  assert.equal(
    view.container.querySelectorAll(".inline-composer-mirror").length,
    1,
  );
  assert.equal(
    view.container
      .querySelector(".split-pane-old .inline-composer-mirror")
      ?.getAttribute("aria-hidden"),
    "true",
  );
  assert.ok(
    view.container.querySelector(".split-pane-new .inline-composer-row"),
  );
});

test("a drag over the line numbers selects the range and the composer claims it", async () => {
  const review = "review-composer-drag";
  const saves = [];
  const view = renderDiff(wideBridge(review, { saveReviewDraft: recorder(review, saves) }), review);
  await waitFor(() => assert.equal(lineAnchors(view).length, 6));
  fireEvent.mouseDown(lineAnchor(view, "new", 11));
  fireEvent.mouseOver(lineAnchor(view, "new", 13), { buttons: 1 });
  fireEvent.mouseUp(document);
  assert.equal(view.container.querySelectorAll(".line-selected").length, 3);

  // The gutter of any row inside the range opens one composer on the whole
  // span, under the last line of it, and leaves the selection alone.
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 12" }));
  await view.findByLabelText("Inline comment composer");
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 1);
  assert.equal(view.container.querySelectorAll(".line-selected").length, 3);
  assert.equal(
    view.getByText("src/calc.py, Lines 11 to 13 (new)").textContent,
    "src/calc.py, Lines 11 to 13 (new)",
  );
  assert.deepEqual(
    [...view.container.querySelectorAll(".windowed-items > *")].map(
      (element) => element.className.split(" ")[0],
    ),
    [
      "diff-file",
      "diff-hunk",
      "diff-line",
      "diff-line",
      "diff-line",
      "diff-line",
      "inline-composer-row",
      "diff-line",
    ],
  );

  fireEvent.change(view.getByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitFor(() => assert.equal(saves.length, 1));
  const anchor = saves[0].content.comments[0].anchor;
  assert.equal(anchor.side, "new");
  assert.equal(anchor.new_line, 13);
  assert.equal(anchor.old_line, null);
  assert.equal(anchor.start_line, 11);
  assert.equal(anchor.start_side, "new");
  assert.equal(anchor.context_fingerprint.length, 64);
});

test("a Shift-click range built upward still records the start before the end", async () => {
  const review = "review-composer-shift-range";
  const saves = [];
  const view = renderDiff(wideBridge(review, { saveReviewDraft: recorder(review, saves) }), review);
  await waitFor(() => assert.equal(lineAnchors(view).length, 6));
  fireEvent.click(lineAnchor(view, "new", 13));
  fireEvent.click(lineAnchor(view, "new", 11), { shiftKey: true });
  assert.equal(view.container.querySelectorAll(".line-selected").length, 3);
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  await view.findByLabelText("Inline comment composer");
  assert.equal(
    view.getByText("src/calc.py, Lines 11 to 13 (new)").textContent,
    "src/calc.py, Lines 11 to 13 (new)",
  );
  fireEvent.change(view.getByLabelText("Inline review comment"), {
    target: { value: "Reads as three statements" },
  });
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitFor(() => assert.equal(saves.length, 1));
  const anchor = saves[0].content.comments[0].anchor;
  assert.equal(anchor.new_line, 13);
  assert.equal(anchor.start_line, 11);
  assert.equal(anchor.start_side, "new");
});

test("a range refuses both writes when the forge has no multiline capability", async () => {
  const review = "review-composer-no-multiline";
  const saves = [];
  const posts = [];
  const view = renderDiff(
    wideBridge(review, {
      getReviewMutationCapabilities: () =>
        read({ review, capabilities: capabilities({ multiline_comment: false }) }),
      saveReviewDraft: recorder(review, saves),
      postInlineReviewComment: async (params) => {
        posts.push(params);
        return mutation(params.operation_id);
      },
    }),
    review,
  );
  await waitFor(() => assert.equal(lineAnchors(view).length, 6));
  fireEvent.mouseDown(lineAnchor(view, "new", 11));
  fireEvent.mouseOver(lineAnchor(view, "new", 12), { buttons: 1 });
  fireEvent.mouseUp(document);
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 12" }));
  await view.findByLabelText("Inline comment composer");
  fireEvent.change(view.getByLabelText("Inline review comment"), {
    target: { value: "Two lines at once" },
  });
  const refusal =
    "Multi-line comments are unsupported for this review. Select a single line.";
  assert.equal(view.getByText(refusal).textContent, refusal);
  assert.equal(
    view.getByRole("button", { name: "Start a review" }).disabled,
    true,
  );
  assert.equal(
    view.getByRole("button", { name: "Add comment now" }).disabled,
    true,
  );
  assert.equal(
    view.getByRole("button", { name: "Insert suggestion" }).disabled,
    true,
  );
  assert.equal(saves.length, 0);
  assert.equal(posts.length, 0);

  // A single line on the same forge is unaffected.
  fireEvent.click(view.getByRole("button", { name: "Cancel" }));
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 13" }));
  await view.findByLabelText("Inline comment composer");
  fireEvent.change(view.getByLabelText("Inline review comment"), {
    target: { value: "One line" },
  });
  assert.equal(view.queryByText(refusal), null);
  assert.equal(
    view.getByRole("button", { name: "Start a review" }).disabled,
    false,
  );
});

test("Insert suggestion pre-fills the GitHub block and the entry keeps the range", async () => {
  const review = "review-composer-suggest-github";
  const saves = [];
  const view = renderDiff(wideBridge(review, { saveReviewDraft: recorder(review, saves) }), review);
  await waitFor(() => assert.equal(lineAnchors(view).length, 6));
  fireEvent.mouseDown(lineAnchor(view, "new", 11));
  fireEvent.mouseOver(lineAnchor(view, "new", 13), { buttons: 1 });
  fireEvent.mouseUp(document);
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 13" }));
  await view.findByLabelText("Inline comment composer");
  fireEvent.change(view.getByLabelText("Inline review comment"), {
    target: { value: "Raise the builtin instead" },
  });
  assert.equal(
    view.getByRole("button", { name: "Insert suggestion" }).disabled,
    false,
  );
  fireEvent.click(view.getByRole("button", { name: "Insert suggestion" }));
  const expected = [
    "Raise the builtin instead",
    "",
    "```suggestion",
    "    if b == 0:",
    '        raise ValueError("b")',
    "    return a / b",
    "```",
  ].join("\n");
  assert.equal(view.getByLabelText("Inline review comment").value, expected);

  // A body that already carries a block refuses a second one.
  const second = view.getByRole("button", { name: "Insert suggestion" });
  assert.equal(second.disabled, true);
  assert.equal(
    second.getAttribute("title"),
    "This comment already carries a suggestion block.",
  );

  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitFor(() => assert.equal(saves.length, 1));
  const comment = saves[0].content.comments[0];
  assert.equal(comment.kind, "inline");
  assert.equal(comment.body, expected);
  assert.equal(comment.anchor.new_line, 13);
  assert.equal(comment.anchor.start_line, 11);
  assert.equal(comment.anchor.start_side, "new");
});

test("Insert suggestion writes the GitLab directive and anchors the first line", async () => {
  const review = "review-composer-suggest-gitlab";
  const saves = [];
  const posts = [];
  const view = renderDiff(
    wideBridge(review, {
      saveReviewDraft: recorder(review, saves),
      postInlineReviewComment: async (params) => {
        posts.push(params);
        return mutation(params.operation_id);
      },
    }),
    review,
    "gitlab",
  );
  await waitFor(() => assert.equal(lineAnchors(view).length, 6));
  fireEvent.mouseDown(lineAnchor(view, "new", 11));
  fireEvent.mouseOver(lineAnchor(view, "new", 13), { buttons: 1 });
  fireEvent.mouseUp(document);
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 13" }));
  await view.findByLabelText("Inline comment composer");
  fireEvent.click(view.getByRole("button", { name: "Insert suggestion" }));
  const expected = [
    "```suggestion:-0+2",
    "    if b == 0:",
    '        raise ValueError("b")',
    "    return a / b",
    "```",
  ].join("\n");
  assert.equal(view.getByLabelText("Inline review comment").value, expected);

  // GitLab encodes the span in the fence, so the note itself anchors the
  // first line of the range and carries no range anchor.
  fireEvent.click(view.getByRole("button", { name: "Add comment now" }));
  await waitFor(() => assert.equal(posts.length, 1));
  assert.equal(posts[0].body, expected);
  assert.deepEqual(posts[0].anchor, {
    old_path: "src/calc.py",
    new_path: "src/calc.py",
    line: 11,
    side: "RIGHT",
  });
  assert.equal(saves.length, 0);
});

test("an old-side selection cannot produce a suggestion", async () => {
  const review = "review-composer-suggest-old-side";
  const view = renderDiff(wideBridge(review), review);
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on old line 11" }),
  );
  await view.findByLabelText("Inline comment composer");
  const oldSide = view.getByRole("button", { name: "Insert suggestion" });
  assert.equal(oldSide.disabled, true);
  assert.equal(
    oldSide.getAttribute("title"),
    "Suggestions can replace lines on the new side only.",
  );
});

test("a partial diff cannot produce a suggestion", async () => {
  const review = "review-composer-suggest-partial";
  const view = renderDiff(
    wideBridge(review, {
      openDiff: (params) =>
        read(truncatedWidePage(review, params.layout ?? "unified")),
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 12" }),
  );
  await view.findByLabelText("Inline comment composer");
  const incomplete = view.getByRole("button", { name: "Insert suggestion" });
  assert.equal(incomplete.disabled, true);
  assert.equal(
    incomplete.getAttribute("title"),
    "The selected diff is partial. Refresh the complete diff before suggesting.",
  );
});

test("the Preview toggle renders the typed Markdown and hands focus back", async () => {
  const review = "review-composer-preview";
  const saves = [];
  const view = renderDiff(
    wideBridge(review, { saveReviewDraft: recorder(review, saves) }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 12" }),
  );
  await view.findByLabelText("Inline comment composer");
  const preview = view.getByRole("button", { name: "Preview" });
  assert.equal(preview.getAttribute("aria-pressed"), "false");

  fireEvent.click(preview);
  assert.equal(
    view.getByRole("button", { name: "Preview" }).getAttribute("aria-pressed"),
    "true",
  );
  assert.equal(view.queryByLabelText("Inline review comment"), null);
  assert.equal(
    view.getByLabelText("Comment preview").textContent,
    "Nothing to preview yet.",
  );

  fireEvent.click(view.getByRole("button", { name: "Preview" }));
  const editor = view.getByLabelText("Inline review comment");
  assert.equal(
    document.activeElement.getAttribute("aria-label"),
    "Inline review comment",
  );
  fireEvent.change(editor, { target: { value: "**Guard** the divisor" } });
  fireEvent.click(view.getByRole("button", { name: "Preview" }));
  const rendered = view.getByLabelText("Comment preview");
  assert.equal(rendered.textContent, "Guard the divisor");
  assert.equal(rendered.querySelectorAll("strong").length, 1);
  assert.equal(rendered.querySelector("strong").textContent, "Guard");

  // The primary action belongs to the composer, so the key still fires while
  // the preview stands in for the editor.
  fireEvent.keyDown(view.getByLabelText("Comment preview"), {
    key: "Enter",
    ctrlKey: true,
  });
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(saves[0].content.comments[0].body, "**Guard** the divisor");
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".inline-composer").length, 0),
  );

  // The typed text is cleared with the entry, and reopening starts empty.
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 12" }));
  assert.equal((await view.findByLabelText("Inline review comment")).value, "");
});

test("a drag over the split line numbers selects the range and the composer claims it", async () => {
  const review = "review-composer-split-drag";
  const saves = [];
  const view = renderDiff(
    wideBridge(review, { saveReviewDraft: recorder(review, saves) }),
    review,
  );
  fireEvent.click(await view.findByText("Split"));
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll('.split-cell[data-anchor-side="new"]')
        .length,
      4,
    ),
  );
  fireEvent.mouseDown(splitNumber(view, "new", 11));
  fireEvent.mouseOver(splitNumber(view, "new", 13), { buttons: 1 });
  fireEvent.mouseUp(document);
  assert.equal(
    view.container.querySelectorAll(".split-cell.line-selected").length,
    3,
  );

  fireEvent.click(view.getByRole("button", { name: "Comment on new line 12" }));
  await view.findByLabelText("Inline comment composer");
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 1);
  assert.equal(
    view.container.querySelectorAll(".inline-composer-mirror").length,
    1,
  );
  assert.equal(
    view.container.querySelectorAll(".split-cell.line-selected").length,
    3,
  );
  assert.equal(
    view.getByText("src/calc.py, Lines 11 to 13 (new)").textContent,
    "src/calc.py, Lines 11 to 13 (new)",
  );
  fireEvent.change(view.getByLabelText("Inline review comment"), {
    target: { value: "Three statements at once" },
  });
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitFor(() => assert.equal(saves.length, 1));
  const anchor = saves[0].content.comments[0].anchor;
  assert.equal(anchor.side, "new");
  assert.equal(anchor.new_line, 13);
  assert.equal(anchor.start_line, 11);
  assert.equal(anchor.start_side, "new");
});

test("a split Shift-click range built upward records the start before the end", async () => {
  const review = "review-composer-split-shift";
  const saves = [];
  const view = renderDiff(
    wideBridge(review, { saveReviewDraft: recorder(review, saves) }),
    review,
  );
  fireEvent.click(await view.findByText("Split"));
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll('.split-cell[data-anchor-side="new"]')
        .length,
      4,
    ),
  );
  fireEvent.click(splitCell(view, "new", 13));
  fireEvent.click(splitCell(view, "new", 11), { shiftKey: true });
  assert.equal(
    view.container.querySelectorAll(".split-cell.line-selected").length,
    3,
  );
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  await view.findByLabelText("Inline comment composer");
  assert.equal(
    view.getByText("src/calc.py, Lines 11 to 13 (new)").textContent,
    "src/calc.py, Lines 11 to 13 (new)",
  );
  fireEvent.change(view.getByLabelText("Inline review comment"), {
    target: { value: "Built from the bottom up" },
  });
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitFor(() => assert.equal(saves.length, 1));
  const anchor = saves[0].content.comments[0].anchor;
  assert.equal(anchor.new_line, 13);
  assert.equal(anchor.start_line, 11);
  assert.equal(anchor.start_side, "new");
});

test("the drag affordance is the line number, so diff code stays selectable", async () => {
  const review = "review-composer-selectable";
  const view = renderDiff(wideBridge(review), review);
  await waitFor(() => assert.equal(lineAnchors(view).length, 6));

  // fireEvent returns false when a handler called preventDefault, which is
  // what suppresses the browser's own text selection.
  assert.equal(
    fireEvent.mouseDown(
      view.container.querySelector('.line-content[role="button"]'),
    ),
    true,
  );
  assert.equal(fireEvent.mouseDown(lineAnchor(view, "new", 12)), false);

  fireEvent.click(view.getByText("Split"));
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll('.split-cell[data-anchor-side="new"]')
        .length,
      4,
    ),
  );
  assert.equal(
    fireEvent.mouseDown(
      splitCell(view, "new", 12).querySelector(".line-content"),
    ),
    true,
  );
  assert.equal(fireEvent.mouseDown(splitNumber(view, "new", 12)), false);
  assert.equal(
    view.container.querySelectorAll(".split-cell.line-selected").length,
    1,
  );
});

test("a drag that overshoots into the next hunk stops at the boundary", async () => {
  const review = "review-composer-hunk-boundary";
  const view = renderDiff(
    wideBridge(review, {
      openDiff: (params) => read(twoHunkPage(review, params.layout ?? "unified")),
    }),
    review,
  );
  await waitFor(() => assert.equal(lineAnchors(view).length, 9));
  fireEvent.mouseDown(lineAnchor(view, "new", 11));
  fireEvent.mouseOver(lineAnchor(view, "new", 13), { buttons: 1 });
  assert.equal(view.container.querySelectorAll(".line-selected").length, 3);

  // The second hunk cannot join the range, and the range the reader built is
  // not discarded either.
  fireEvent.mouseOver(lineAnchor(view, "new", 31), { buttons: 1 });
  assert.equal(view.container.querySelectorAll(".line-selected").length, 3);
  assert.equal(view.container.querySelectorAll(".notice-error").length, 0);
  fireEvent.mouseUp(document);

  fireEvent.click(view.getByRole("button", { name: "Comment on new line 12" }));
  await view.findByLabelText("Inline comment composer");
  assert.equal(
    view.getByText("src/calc.py, Lines 11 to 13 (new)").textContent,
    "src/calc.py, Lines 11 to 13 (new)",
  );
});

test("a saved entry becomes a pending card under its own row", async () => {
  const review = "review-pending-card";
  const saves = [];
  const view = renderDiff(diffBridge(review, { saveReviewDraft: recorder(review, saves) }), review);
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the `zero` divisor" },
  });
  assert.equal(view.container.querySelectorAll(".pending-card").length, 0);
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await waitFor(() => assert.equal(saves.length, 1));

  const card = await view.findByLabelText("Pending review comment on new line 11");
  assert.equal(view.container.querySelectorAll(".pending-card").length, 1);
  assert.equal(card.querySelector(".pending-badge").textContent, "Pending");
  assert.equal(card.querySelector(".pending-card-author").textContent, "You");
  assert.equal(card.querySelector(".pending-card-anchor").textContent, "new line 11");
  // The body is safe Markdown, so the backticks became a code span.
  assert.equal(card.querySelector(".pending-card-body code").textContent, "zero");
  assert.equal(card.querySelectorAll(".pending-card-ribbon").length, 0);
  assert.ok(view.getByRole("button", { name: "Edit pending comment on new line 11" }));
  assert.ok(view.getByRole("button", { name: "Delete pending comment on new line 11" }));

  // The card is a row of the diff under the line its anchor names, and the
  // composer closed on the successful write.
  assert.deepEqual(
    [...view.container.querySelectorAll(".windowed-items > *")].map((element) =>
      element.className.split(" ")[0],
    ),
    ["diff-file", "diff-hunk", "diff-line", "diff-line", "pending-card-row", "diff-line"],
  );
  assert.equal(view.getByText("1 pending").textContent, "1 pending");
});

test("Edit round-trips the body and replaces the entry instead of appending", async () => {
  const review = "review-pending-edit";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 4, [inlineEntry("entry-a", "Guard the zero divisor", 11)])],
        }),
      saveReviewDraft: recorder(review, saves),
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Edit pending comment on new line 11" }),
  );
  const editor = await view.findByLabelText("Pending review comment");
  assert.equal(editor.value, "Guard the zero divisor");
  assert.ok(view.getByLabelText("Edit pending comment composer"));
  assert.equal(
    view.getByText("src/calc.py, new line 11").textContent,
    "src/calc.py, new line 11",
  );
  // The edit replaces a body; it never recaptures an anchor, so the suggestion
  // rules and the quick path are not offered here.
  assert.equal(view.queryByRole("button", { name: "Insert suggestion" }), null);
  assert.equal(view.queryByRole("button", { name: "Add comment now" }), null);
  // The card gives way to the composer rather than being shown twice.
  assert.equal(view.container.querySelectorAll(".pending-card").length, 0);

  fireEvent.change(editor, { target: { value: "Raise ZeroDivisionError instead" } });
  fireEvent.click(view.getByRole("button", { name: "Save changes" }));
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(saves[0].expected_version, 4);
  assert.deepEqual(
    saves[0].content.comments.map((comment) => `${comment.id}:${comment.body}`),
    ["entry-a:Raise ZeroDivisionError instead"],
  );
  assert.equal(saves[0].content.comments[0].anchor.new_line, 11);

  const card = await view.findByLabelText("Pending review comment on new line 11");
  assert.equal(card.querySelector(".pending-card-body p").textContent, "Raise ZeroDivisionError instead");
  assert.equal(view.container.querySelectorAll(".pending-card").length, 1);
  assert.equal(view.getByText("1 pending").textContent, "1 pending");
});

test("Delete saves the draft without the entry", async () => {
  const review = "review-pending-delete";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(review, 6, [
              inlineEntry("entry-a", "First note", 11),
              inlineEntry("entry-b", "Second note", 10),
            ]),
          ],
        }),
      saveReviewDraft: recorder(review, saves),
    }),
    review,
  );
  await view.findByLabelText("Pending review comment on new line 11");
  assert.equal(view.container.querySelectorAll(".pending-card").length, 2);
  assert.equal(view.getByText("2 pending").textContent, "2 pending");

  fireEvent.click(
    view.getByRole("button", { name: "Delete pending comment on new line 11" }),
  );
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(saves[0].expected_version, 6);
  assert.deepEqual(
    saves[0].content.comments.map((comment) => `${comment.id}:${comment.body}`),
    ["entry-b:Second note"],
  );
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".pending-card").length, 1),
  );
  assert.equal(view.queryByLabelText("Pending review comment on new line 11"), null);
  assert.equal(view.getByText("1 pending").textContent, "1 pending");
});

test("a stale entry keeps its ribbon and offers no Edit", async () => {
  const review = "review-pending-stale";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(review, 2, [
              inlineEntry("entry-a", "Written against an older head", 11, {
                stale: true,
                revision: { ...REVISION, head_sha: "abc1234deadbeef" },
              }),
            ]),
          ],
        }),
      saveReviewDraft: recorder(review, saves),
    }),
    review,
  );
  const card = await view.findByLabelText("Pending review comment on new line 11");
  assert.equal(
    card.querySelector(".pending-card-ribbon").textContent,
    "Stale, was line 11 at revision abc1234",
  );
  assert.equal(card.className.includes("pending-card-stale"), true);
  assert.equal(
    view.queryByRole("button", { name: "Edit pending comment on new line 11" }),
    null,
  );
  assert.ok(view.getByRole("button", { name: "Delete pending comment on new line 11" }));
  // Nothing is written just by rendering a stale entry.
  assert.equal(saves.length, 0);
});

test("a range entry renders one card under the row that closes the range", async () => {
  const review = "review-pending-range";
  const view = renderDiff(
    wideBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(review, 3, [
              inlineEntry("entry-a", "Guard the whole block", 13, {
                start_line: 11,
                start_side: "new",
              }),
            ]),
          ],
        }),
    }),
    review,
  );
  const card = await view.findByLabelText(
    "Pending review comment on Lines 11 to 13 (new)",
  );
  assert.equal(
    card.querySelector(".pending-card-anchor").textContent,
    "Lines 11 to 13 (new)",
  );
  assert.equal(view.container.querySelectorAll(".pending-card").length, 1);
  // One card, under line 13, which is where the composer that wrote it stood.
  assert.deepEqual(
    [...view.container.querySelectorAll(".windowed-items > *")].map((element) =>
      element.className.split(" ")[0],
    ),
    [
      "diff-file",
      "diff-hunk",
      "diff-line",
      "diff-line",
      "diff-line",
      "diff-line",
      "pending-card-row",
      "diff-line",
    ],
  );
});

test("the split layout renders the card in its own pane and mirrors the other", async () => {
  const review = "review-pending-split";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      openDiff: (params) => read(diffPage(review, params.layout ?? "unified")),
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 2, [inlineEntry("entry-a", "New side note", 11)])],
        }),
      saveReviewDraft: recorder(review, saves),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: "Split" }));
  await view.findByLabelText("Pending review comment on new line 11");
  assert.equal(
    view.container.querySelectorAll(".split-pane-new .pending-card-row").length,
    1,
  );
  assert.equal(
    view.container.querySelectorAll(".split-pane-old .pending-card-row").length,
    0,
  );
  assert.equal(
    view.container.querySelectorAll(".split-pane-old .pending-card-mirror").length,
    1,
  );
  assert.equal(view.getByText("1 pending").textContent, "1 pending");

  // Edit opens the composer in the pane that owns the anchor side, and the
  // other pane keeps its spacer.
  fireEvent.click(
    view.getByRole("button", { name: "Edit pending comment on new line 11" }),
  );
  await view.findByLabelText("Edit pending comment composer");
  assert.equal(
    view.container.querySelectorAll(".split-pane-new .inline-composer-row").length,
    1,
  );
  assert.equal(
    view.container.querySelectorAll(".split-pane-old .inline-composer-mirror").length,
    1,
  );
  assert.equal(view.container.querySelectorAll(".pending-card").length, 0);
});

test("a failed entry delete restores the pending card and reports the failure", async () => {
  const review = "review-pending-delete-failure";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 5, [inlineEntry("entry-a", "First note", 11)])],
        }),
      saveReviewDraft: async (params) => {
        saves.push(params);
        throw { code: "write_failed", message: "the sidecar refused" };
      },
    }),
    review,
  );
  await view.findByLabelText("Pending review comment on new line 11");
  fireEvent.click(
    view.getByRole("button", { name: "Delete pending comment on new line 11" }),
  );
  await waitFor(() => assert.equal(saves.length, 1));
  // The entry is put back exactly as it was, so the obvious retry sends the
  // same removal once rather than saving a review the store never lost.
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".pending-card").length, 1),
  );
  assert.equal(view.getByText("1 pending").textContent, "1 pending");
  fireEvent.click(
    view.getByRole("button", { name: "Delete pending comment on new line 11" }),
  );
  await waitFor(() => assert.equal(saves.length, 2));
  assert.equal(saves[1].expected_version, 5);
  assert.deepEqual(
    saves[1].content.comments.map((comment) => comment.id),
    [],
  );
});

test("switching a row's composer from a new comment to Edit keeps the two texts apart", async () => {
  const review = "review-pending-new-then-edit";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 4, [inlineEntry("entry-a", "Guard the zero divisor", 11)])],
        }),
      saveReviewDraft: recorder(review, saves),
    }),
    review,
  );
  // The card and the gutter share one row, so the composer changes shape in
  // place. Its text must change with it.
  await view.findByLabelText("Pending review comment on new line 11");
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "UNSENT NEW COMMENT" },
  });
  fireEvent.click(
    view.getByRole("button", { name: "Edit pending comment on new line 11" }),
  );
  const editor = await view.findByLabelText("Pending review comment");
  assert.equal(editor.value, "Guard the zero divisor");
  assert.equal(view.container.querySelectorAll(".inline-composer-text").length, 1);

  fireEvent.change(editor, { target: { value: "Raise ZeroDivisionError instead" } });
  fireEvent.click(view.getByRole("button", { name: "Save changes" }));
  await waitFor(() => assert.equal(saves.length, 1));
  assert.deepEqual(
    saves[0].content.comments.map((comment) => `${comment.id}:${comment.body}`),
    ["entry-a:Raise ZeroDivisionError instead"],
  );

  // The new comment's text is still on its own anchor, unsent.
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  assert.equal(
    (await view.findByLabelText("Inline review comment")).value,
    "UNSENT NEW COMMENT",
  );
});

test("switching a row's composer from Edit to a new comment keeps the two texts apart", async () => {
  const review = "review-pending-edit-then-new";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 2, [inlineEntry("entry-a", "Guard the zero divisor", 11)])],
        }),
      saveReviewDraft: recorder(review, saves),
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Edit pending comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Pending review comment"), {
    target: { value: "UNSENT EDIT TEXT" },
  });
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  const composer = await view.findByLabelText("Inline review comment");
  assert.equal(composer.value, "");
  assert.equal(view.container.querySelectorAll(".inline-composer-text").length, 1);

  fireEvent.change(composer, { target: { value: "A second note on this line" } });
  fireEvent.click(view.getByRole("button", { name: "Add to review" }));
  await waitFor(() => assert.equal(saves.length, 1));
  // The abandoned edit is not appended, and the stored body is untouched.
  assert.deepEqual(
    saves[0].content.comments.map((comment) => comment.body),
    ["Guard the zero divisor", "A second note on this line"],
  );
  assert.equal(saves[0].content.comments[0].id, "entry-a");
  assert.notEqual(saves[0].content.comments[1].id, "entry-a");
});

test("saving a pending comment that was not changed writes no new version", async () => {
  const review = "review-pending-noop-save";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 9, [inlineEntry("entry-a", "Guard the zero divisor", 11)])],
        }),
      saveReviewDraft: recorder(review, saves),
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Edit pending comment on new line 11" }),
  );
  await view.findByLabelText("Edit pending comment composer");
  fireEvent.click(view.getByRole("button", { name: "Save changes" }));
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".inline-composer").length, 0),
  );
  // No version was burned, and the entry is back as a card unchanged.
  assert.equal(saves.length, 0);
  assert.equal(view.container.querySelectorAll(".pending-card").length, 1);
  assert.equal(
    view.container.querySelector(".pending-card-body p").textContent,
    "Guard the zero divisor",
  );
});

test("editing one entry then another on the same row never writes one body into the other", async () => {
  const review = "review-pending-a-then-b";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            draft(review, 3, [
              inlineEntry("entry-a", "AAA body", 11),
              inlineEntry("entry-b", "BBB body", 11),
            ]),
          ],
        }),
      saveReviewDraft: recorder(review, saves),
    }),
    review,
  );
  await view.findAllByLabelText("Pending review comment on new line 11");
  assert.equal(view.container.querySelectorAll(".pending-card").length, 2);
  assert.equal(view.getByText("2 pending").textContent, "2 pending");

  fireEvent.click(
    view.getAllByRole("button", { name: "Edit pending comment on new line 11" })[0],
  );
  assert.equal(
    (await view.findByLabelText("Pending review comment")).value,
    "AAA body",
  );
  // Only the other entry's card is left, so this Edit is unambiguous.
  fireEvent.click(
    view.getByRole("button", { name: "Edit pending comment on new line 11" }),
  );
  const editor = await view.findByLabelText("Pending review comment");
  assert.equal(editor.value, "BBB body");

  fireEvent.change(editor, { target: { value: "BBB edited" } });
  fireEvent.click(view.getByRole("button", { name: "Save changes" }));
  await waitFor(() => assert.equal(saves.length, 1));
  assert.deepEqual(
    saves[0].content.comments.map((comment) => `${comment.id}:${comment.body}`),
    ["entry-a:AAA body", "entry-b:BBB edited"],
  );
});

test("a read-only pending review explains itself once per file, in the words of the card", async () => {
  const review = "review-pending-readonly";
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [
            {
              ...draft(review, 2, [
                inlineEntry("entry-a", "First note", 11),
                inlineEntry("entry-b", "Second note", 10),
                inlineEntry("entry-c", "Third note", 12, { side: "old", old_line: 12, new_line: null }),
              ]),
              revision: { head_sha: "old-head", base_sha: "df2bd3f", start_sha: null },
            },
          ],
        }),
      saveReviewDraft: recorder(review, saves),
    }),
    review,
  );
  await view.findByLabelText("Pending review comment on new line 11");
  assert.equal(view.container.querySelectorAll(".pending-card").length, 3);
  // One sentence for the file, not one per card.
  assert.equal(view.container.querySelectorAll(".pending-card small").length, 1);
  const refusal =
    "The pending review is bound to an earlier revision. Migrate it in the review workflow before changing pending comments.";
  assert.equal(view.container.querySelector(".pending-card small").textContent, refusal);
  const edit = view.getByRole("button", { name: "Edit pending comment on new line 11" });
  assert.equal(edit.disabled, true);
  assert.equal(edit.title, refusal);
  const remove = view.getByRole("button", { name: "Delete pending comment on new line 11" });
  assert.equal(remove.disabled, true);
  assert.equal(remove.title, refusal);
  fireEvent.click(remove);
  assert.equal(saves.length, 0);
});

test("a published thread renders as one collapsed row under the line it is anchored to", async () => {
  const review = "review-thread-collapsed";
  const view = renderDiff(
    threadBridge(review, [[thread("d1", { replies: ["Yes", "Filed as #12"] })]], []),
    review,
  );
  await view.findByLabelText("Discussion thread on new line 11");
  assert.equal(view.container.querySelectorAll(".diff-thread").length, 1);
  assert.equal(
    view.container.querySelector(".diff-thread-summary").textContent,
    "2 replies, unresolved, last by @author",
  );
  // Collapsed means no body: the thread costs the window one row.
  assert.equal(view.container.querySelectorAll(".diff-thread-body").length, 0);
  assert.equal(
    view.container.querySelector(".diff-thread-summary").getAttribute("aria-expanded"),
    "false",
  );
  assert.deepEqual(
    [...view.container.querySelectorAll(".windowed-items > *")].map((element) =>
      element.className.split(" ")[0],
    ),
    ["diff-file", "diff-hunk", "diff-line", "diff-line", "thread-row", "diff-line"],
  );
  // The file header states the totals over the whole file.
  assert.equal(
    view.container.querySelector(".file-threads").textContent,
    "1 thread, 1 unresolved",
  );
});

test("the file header counts every anchored thread and how many are unresolved", async () => {
  const review = "review-thread-counts";
  const view = renderDiff(
    threadBridge(
      review,
      [
        [
          thread("d1", { line: 11 }),
          thread("d2", { line: 11, resolved: true }),
          thread("d3", { line: 12, side: "old" }),
          // A review-level discussion stays in Overview and is never counted here.
          thread("d4", { inline: false }),
        ],
      ],
      [],
    ),
    review,
  );
  await view.findByLabelText("Discussion thread on old line 12");
  assert.equal(
    view.container.querySelector(".file-threads").textContent,
    "3 threads, 2 unresolved",
  );
  assert.equal(view.container.querySelectorAll(".diff-thread").length, 3);
  assert.equal(
    view.container.querySelector(".file-threads").getAttribute("aria-label"),
    "3 threads, 2 unresolved on this file",
  );
});

test("expanding a thread renders its bodies in place and collapsing takes them away", async () => {
  const review = "review-thread-expand";
  const view = renderDiff(
    threadBridge(
      review,
      [[thread("d1", { body: "Should this log?", replies: ["It should"] })]],
      [],
    ),
    review,
  );
  const summary = await view.findByLabelText(
    "Summary of the discussion on new line 11",
  );
  fireEvent.click(summary);
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".diff-thread-body").length, 1),
  );
  assert.equal(summary.getAttribute("aria-expanded"), "true");
  assert.equal(
    view.container.querySelector(".diff-thread-body p").textContent,
    "Should this log?",
  );
  assert.equal(
    view.container.querySelector(".diff-thread-body blockquote p").textContent,
    "It should",
  );
  // The expanded row declares the taller of the two row heights.
  assert.equal(
    view.container.querySelectorAll(".thread-row.thread-row-expanded").length,
    1,
  );
  fireEvent.click(summary);
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".diff-thread-body").length, 0),
  );
  assert.equal(
    view.container.querySelectorAll(".thread-row.thread-row-expanded").length,
    0,
  );
});

test("a thread body renders remote Markdown through the safe renderer only", async () => {
  const review = "review-thread-markdown";
  const hostile = [
    "<script>globalThis.__pwned = true;</script>",
    '<img src="x" onerror="globalThis.__pwned = true">',
    "[click](javascript:globalThis.__pwned=true)",
    '<iframe src="https://evil.invalid"></iframe>',
    '<div onmouseover="globalThis.__pwned = true">hover</div>',
  ].join("\n\n");
  const view = renderDiff(
    threadBridge(review, [[thread("d1", { body: hostile })]], []),
    review,
  );
  fireEvent.click(
    await view.findByLabelText("Summary of the discussion on new line 11"),
  );
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".diff-thread-body").length, 1),
  );
  assert.equal(
    view.container.querySelectorAll(
      ".diff-thread-body script, .diff-thread-body iframe, .diff-thread-body object, .diff-thread-body embed, .diff-thread-body img, .diff-thread-body svg, .diff-thread-body style, .diff-thread-body form",
    ).length,
    0,
  );
  assert.deepEqual(
    [...view.container.querySelectorAll(".diff-thread-body a")].map((node) =>
      node.getAttribute("href"),
    ),
    [],
  );
  assert.equal(globalThis.__pwned, undefined);
});

test("Reply publishes exactly one reply, without a resolution, and keeps its text on Escape", async () => {
  const review = "review-thread-reply";
  const calls = [];
  const view = renderDiff(
    threadBridge(
      review,
      [
        [thread("d1")],
        [thread("d1", { replies: ["Guarded now"], replyAuthor: "you" })],
      ],
      calls,
    ),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Reply to the discussion on new line 11" }),
  );
  const editor = await view.findByLabelText(
    "Reply body for the discussion on new line 11",
  );
  // The composer takes focus, so Escape and Ctrl+Enter reach it without a Tab.
  assert.equal(
    document.activeElement.getAttribute("aria-label"),
    "Reply body for the discussion on new line 11",
  );
  fireEvent.change(editor, { target: { value: "Guarded now" } });
  assert.equal(
    view.getByRole("button", { name: "Reply now" }).disabled,
    false,
  );

  // Escape closes the composer and keeps the text on this thread.
  fireEvent.keyDown(document.activeElement, { key: "Escape" });
  assert.equal(view.container.querySelectorAll(".diff-thread-composer").length, 0);
  assert.equal(calls.length, 0);
  fireEvent.click(
    view.getByRole("button", { name: "Reply to the discussion on new line 11" }),
  );
  assert.equal(
    (await view.findByLabelText("Reply body for the discussion on new line 11")).value,
    "Guarded now",
  );

  fireEvent.click(view.getByRole("button", { name: "Reply now" }));
  await waitFor(() => assert.equal(calls.length, 1));
  assert.deepEqual(calls, [["reply", "d1", "Guarded now"]]);
  await waitFor(() =>
    assert.equal(
      view.container.querySelector(".diff-thread-summary").textContent,
      "1 reply, unresolved, last by @you",
    ),
  );
  // One press is one mutation, and no resolution was written.
  assert.equal(calls.length, 1);
  assert.equal(view.container.querySelectorAll(".diff-thread-composer").length, 0);
});

test("the Resolve thread toggle resolves the thread with the same press that replies", async () => {
  const review = "review-thread-reply-resolve";
  const calls = [];
  const view = renderDiff(
    threadBridge(
      review,
      [
        [thread("d1")],
        [
          thread("d1", {
            replies: ["Guarded now"],
            replyAuthor: "you",
            resolved: true,
          }),
        ],
      ],
      calls,
    ),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Reply to the discussion on new line 11" }),
  );
  const editor = await view.findByLabelText(
    "Reply body for the discussion on new line 11",
  );
  const toggle = view.getByLabelText("Resolve thread");
  assert.equal(toggle.disabled, false);
  assert.equal(toggle.checked, false);
  fireEvent.click(toggle);
  assert.equal(toggle.checked, true);
  fireEvent.change(editor, { target: { value: "Guarded now" } });
  fireEvent.keyDown(editor, { key: "Enter", ctrlKey: true });
  await waitFor(() => assert.equal(calls.length, 2));
  assert.deepEqual(calls, [
    ["reply", "d1", "Guarded now"],
    ["resolve", "d1", true],
  ]);
  await waitFor(() =>
    assert.equal(
      view.container.querySelector(".diff-thread-summary").textContent,
      "1 reply, resolved, last by @you",
    ),
  );
  // Exactly one mutation per action: no second reply and no second resolution.
  assert.equal(calls.length, 2);
});

test("a resolved thread offers Reopen thread and reopens it with the reply", async () => {
  const review = "review-thread-reopen";
  const calls = [];
  const view = renderDiff(
    threadBridge(
      review,
      [
        [thread("d1", { resolved: true, replies: ["Fixed"] })],
        [
          thread("d1", {
            replies: ["Fixed", "Still wrong"],
            replyAuthor: "you",
          }),
        ],
      ],
      calls,
    ),
    review,
  );
  await waitFor(() =>
    assert.equal(
      view.container.querySelector(".diff-thread-summary").textContent,
      "1 reply, resolved, last by @author",
    ),
  );
  fireEvent.click(
    view.getByRole("button", { name: "Reply to the discussion on new line 11" }),
  );
  const editor = await view.findByLabelText(
    "Reply body for the discussion on new line 11",
  );
  assert.equal(view.container.querySelectorAll(".diff-thread-resolve").length, 1);
  const toggle = view.getByLabelText("Reopen thread");
  fireEvent.click(toggle);
  fireEvent.change(editor, { target: { value: "Still wrong" } });
  fireEvent.click(view.getByRole("button", { name: "Reply now" }));
  await waitFor(() => assert.equal(calls.length, 2));
  assert.deepEqual(calls, [
    ["reply", "d1", "Still wrong"],
    ["resolve", "d1", false],
  ]);
  await waitFor(() =>
    assert.equal(
      view.container.querySelector(".diff-thread-summary").textContent,
      "2 replies, unresolved, last by @you",
    ),
  );
});

test("a second press while a reply is in flight writes one mutation, not two", async () => {
  const review = "review-thread-double-submit";
  const calls = [];
  let release = () => {};
  const view = renderDiff(
    threadBridge(review, [[thread("d1")]], calls, {
      replyReviewDiscussion: async (params) => {
        calls.push(["reply", params.discussion_id, params.body]);
        await new Promise((resolve) => {
          release = resolve;
        });
        return mutation(params.operation_id);
      },
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Reply to the discussion on new line 11" }),
  );
  const editor = await view.findByLabelText(
    "Reply body for the discussion on new line 11",
  );
  fireEvent.change(editor, { target: { value: "Guarded now" } });
  const button = view.getByRole("button", { name: "Reply now" });
  fireEvent.click(button);
  fireEvent.click(button);
  fireEvent.keyDown(editor, { key: "Enter", ctrlKey: true });
  assert.equal(calls.length, 1);
  release();
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".diff-thread-composer").length, 0),
  );
  assert.equal(calls.length, 1);
});

test("the resolve toggle is refused in the forge's own words when the capability is absent", async () => {
  const review = "review-thread-no-resolve";
  const calls = [];
  const view = renderDiff(
    threadBridge(review, [[thread("d1")]], calls, {
      getReviewMutationCapabilities: () =>
        read({ review, capabilities: capabilities({ resolve: false }) }),
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Reply to the discussion on new line 11" }),
  );
  const editor = await view.findByLabelText(
    "Reply body for the discussion on new line 11",
  );
  const refusal = "Resolution is unsupported for this review.";
  const toggle = view.getByLabelText("Resolve thread");
  assert.equal(toggle.disabled, true);
  assert.equal(toggle.title, refusal);
  assert.equal(
    view.container.querySelector(".diff-thread-composer small").textContent,
    refusal,
  );

  // The reply still works, and it writes no resolution.
  fireEvent.change(editor, { target: { value: "Guarded now" } });
  fireEvent.click(view.getByRole("button", { name: "Reply now" }));
  await waitFor(() => assert.equal(calls.length, 1));
  assert.deepEqual(calls, [["reply", "d1", "Guarded now"]]);
});

test("a thread the forge cannot resolve says so about the thread, not the review", async () => {
  const review = "review-thread-not-resolvable";
  const view = renderDiff(
    threadBridge(review, [[thread("d1", { resolvable: false })]], []),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Reply to the discussion on new line 11" }),
  );
  await view.findByLabelText("Reply body for the discussion on new line 11");
  const refusal = "Resolution is unsupported for this discussion.";
  assert.equal(view.getByLabelText("Resolve thread").disabled, true);
  assert.equal(view.getByLabelText("Resolve thread").title, refusal);
  assert.equal(
    view.container.querySelector(".diff-thread-composer small").textContent,
    refusal,
  );
});

test("a review without the reply capability refuses Reply in its own words", async () => {
  const review = "review-thread-no-reply";
  const view = renderDiff(
    threadBridge(review, [[thread("d1")]], [], {
      getReviewMutationCapabilities: () =>
        read({ review, capabilities: capabilities({ reply: false }) }),
    }),
    review,
  );
  const button = await view.findByRole("button", {
    name: "Reply to the discussion on new line 11",
  });
  await waitFor(() =>
    assert.equal(button.title, "Replies are unsupported for this review."),
  );
  assert.equal(button.disabled, true);
  fireEvent.click(button);
  assert.equal(view.container.querySelectorAll(".diff-thread-composer").length, 0);
});

test("a thread and a pending card on one line render as thread first, card under it", async () => {
  const review = "review-thread-with-pending";
  const view = renderDiff(
    threadBridge(review, [[thread("d1")]], [], {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 2, [inlineEntry("entry-a", "Still pending", 11)])],
        }),
    }),
    review,
  );
  await view.findByLabelText("Pending review comment on new line 11");
  await view.findByLabelText("Discussion thread on new line 11");
  assert.deepEqual(
    [...view.container.querySelectorAll(".windowed-items > *")].map((element) =>
      element.className.split(" ")[0],
    ),
    [
      "diff-file",
      "diff-hunk",
      "diff-line",
      "diff-line",
      "thread-row",
      "pending-card-row",
      "diff-line",
    ],
  );
  assert.equal(view.container.querySelector(".file-pending").textContent, "1 pending");
  assert.equal(
    view.container.querySelector(".file-threads").textContent,
    "1 thread, 1 unresolved",
  );
});

test("the split layout renders a thread in its own pane and mirrors the other", async () => {
  const review = "review-thread-split";
  const view = renderDiff(
    threadBridge(review, [[thread("d1")]], [], {
      openDiff: (params) => read(diffPage(review, params.layout ?? "unified")),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: "Split" }));
  await view.findByLabelText("Discussion thread on new line 11");
  assert.equal(
    view.container.querySelectorAll(".split-pane-new .thread-row").length,
    1,
  );
  assert.equal(
    view.container.querySelectorAll(".split-pane-old .thread-row").length,
    0,
  );
  assert.equal(
    view.container.querySelectorAll(".split-pane-old .thread-row-mirror").length,
    1,
  );
  assert.equal(
    view.container.querySelectorAll(".split-pane-new .file-threads").length,
    1,
  );

  // Expanding the thread takes the taller height in both panes at once.
  fireEvent.click(
    view.getByLabelText("Summary of the discussion on new line 11"),
  );
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll(".split-pane-new .thread-row-expanded").length,
      1,
    ),
  );
  assert.equal(
    view.container.querySelectorAll(
      ".split-pane-old .thread-row-mirror.thread-row-mirror-expanded",
    ).length,
    1,
  );
});

test("a review with two hundred threads renders only the threads its window holds", async () => {
  const review = "review-thread-windowed";
  const discussions = [];
  for (let line = 1; line <= 200; line += 1)
    discussions.push(thread(`d${line}`, { line }));
  const view = renderDiff(
    threadBridge(review, [discussions], [], {
      openDiff: (params) =>
        read(manyLinesPage(review, params.layout ?? "unified", 250)),
    }),
    review,
  );
  await view.findByLabelText("Discussion thread on new line 1");
  // 252 rows in one file, 200 rows to a window: the first window holds the
  // file header, the hunk header and new lines 1 to 198.
  assert.equal(
    view.container.querySelector(".window-position").textContent,
    "1-200 of 252",
  );
  assert.equal(view.container.querySelectorAll(".diff-thread").length, 198);
  assert.ok(view.container.querySelectorAll(".diff-thread").length <= 200);
  // Every thread is collapsed, so none of the 200 has a body on screen.
  assert.equal(view.container.querySelectorAll(".diff-thread-body").length, 0);
  // The header still counts all of them, which is what says there is more.
  assert.equal(
    view.container.querySelector(".file-threads").textContent,
    "200 threads, 200 unresolved",
  );

  fireEvent.click(view.getByRole("button", { name: "Next rows" }));
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".diff-thread").length, 2),
  );
  assert.equal(
    view.container.querySelector(".window-position").textContent,
    "201-252 of 252",
  );
});

test("threads that could not be read are named as missing rather than shown as none", async () => {
  const review = "review-thread-read-failure";
  const view = renderDiff(
    diffBridge(review, {
      listDiscussions: () => ({
        requestToken: crypto.randomUUID(),
        result: Promise.reject(new Error("sidecar closed")),
      }),
    }),
    review,
  );
  await view.findByText(
    "The published discussions could not be read, so this diff shows no threads. Refresh the diff or open the review on the forge.",
  );
  assert.equal(view.container.querySelectorAll(".diff-thread").length, 0);
  assert.equal(view.container.querySelectorAll(".notice-error").length, 0);
});

test("a resolution refused after the reply is said on the thread the reply was written on", async () => {
  const review = "review-thread-resolve-refused";
  const calls = [];
  const view = renderDiff(
    threadBridge(
      review,
      [
        [thread("d1")],
        [thread("d1", { replies: ["Guarded now"], replyAuthor: "you" })],
      ],
      calls,
      {
        resolveReviewDiscussion: async (params) => {
          calls.push(["resolve", params.discussion_id, params.resolved]);
          throw { code: "forbidden", message: "not allowed" };
        },
      },
    ),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Reply to the discussion on new line 11" }),
  );
  const editor = await view.findByLabelText(
    "Reply body for the discussion on new line 11",
  );
  fireEvent.click(view.getByLabelText("Resolve thread"));
  fireEvent.change(editor, { target: { value: "Guarded now" } });
  fireEvent.click(view.getByRole("button", { name: "Reply now" }));
  await waitFor(() => assert.equal(calls.length, 2));
  // The composer is gone, and the sentence is on the thread that asked for it.
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".diff-thread-notice").length, 1),
  );
  assert.equal(view.container.querySelectorAll(".diff-thread-composer").length, 0);
  assert.equal(
    view.container
      .querySelector(".diff-thread-notice")
      .textContent.startsWith("The reply was published."),
    true,
  );
  assert.equal(
    view.container.querySelectorAll('.diff-thread-notice[role="alert"]').length,
    1,
  );
  // The reply itself landed and the thread is still unresolved.
  assert.equal(
    view.container.querySelector(".diff-thread-summary").textContent,
    "1 reply, unresolved, last by @you",
  );
  // A row carrying a sentence takes the height that can show it.
  assert.equal(
    view.container.querySelectorAll(".thread-row.thread-row-expanded").length,
    1,
  );
});

test("a failed reread after the reply is named and blocks a second reply on that thread", async () => {
  const review = "review-thread-reread-failed";
  const calls = [];
  let reads = 0;
  const view = renderDiff(
    threadBridge(review, [[thread("d1")]], calls, {
      // Only the reread that follows the reply fails. The read the Refresh
      // press makes afterwards succeeds, and carries the published reply.
      listDiscussions: () => {
        reads += 1;
        if (reads === 2)
          return {
            requestToken: crypto.randomUUID(),
            result: Promise.reject(new Error("sidecar closed")),
          };
        return read({
          discussions: [
            reads === 1
              ? thread("d1")
              : thread("d1", { replies: ["Guarded now"], replyAuthor: "you" }),
          ],
        });
      },
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Reply to the discussion on new line 11" }),
  );
  fireEvent.change(
    await view.findByLabelText("Reply body for the discussion on new line 11"),
    { target: { value: "Guarded now" } },
  );
  fireEvent.click(view.getByRole("button", { name: "Reply now" }));
  await waitFor(() => assert.equal(calls.length, 1));
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".diff-thread-notice").length, 1),
  );
  const refusal =
    "The reply was published. Rereading the discussions failed, so this thread is out of date. Refresh the diff before replying again.";
  assert.equal(
    view.container.querySelector(".diff-thread-notice").textContent,
    refusal,
  );
  assert.equal(reads, 2);

  // The thread is known to be out of date, so it refuses another reply rather
  // than let the same one be published twice.
  const again = view.getByRole("button", {
    name: "Reply to the discussion on new line 11",
  });
  assert.equal(again.disabled, true);
  assert.equal(
    again.title,
    "Rereading the discussions failed, so this thread is out of date. Refresh the diff before replying again.",
  );
  fireEvent.click(again);
  assert.equal(view.container.querySelectorAll(".diff-thread-composer").length, 0);
  assert.equal(calls.length, 1);

  // Refresh rereads the discussions, which is what the sentence tells the
  // reader to do, and the thread takes replies again.
  fireEvent.click(view.getByRole("button", { name: "Refresh" }));
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".diff-thread-notice").length, 0),
  );
  assert.equal(reads, 3);
  assert.equal(
    view.container.querySelector(".diff-thread-summary").textContent,
    "1 reply, unresolved, last by @you",
  );
  assert.equal(
    view.getByRole("button", { name: "Reply to the discussion on new line 11" })
      .disabled,
    false,
  );
});

test("a reply composer opened while a review is pending says the reply publishes at once", async () => {
  const review = "review-thread-pending-note";
  const view = renderDiff(
    threadBridge(review, [[thread("d1")]], [], {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 2, [inlineEntry("entry-a", "Still pending", 11)])],
        }),
    }),
    review,
  );
  await view.findByLabelText("Pending review comment on new line 11");
  fireEvent.click(
    view.getByRole("button", { name: "Reply to the discussion on new line 11" }),
  );
  const composer = await view.findByLabelText(
    "Reply composer for the discussion on new line 11",
  );
  assert.equal(
    composer.querySelector(".diff-thread-immediate").textContent,
    "This reply publishes immediately. It is not added to your pending review.",
  );
  // The action says so too, in the vocabulary the quick inline write uses.
  assert.equal(view.getByRole("button", { name: "Reply now" }).tagName, "BUTTON");
});

test("a reply composer with no pending review does not claim one", async () => {
  const review = "review-thread-no-pending-note";
  const view = renderDiff(threadBridge(review, [[thread("d1")]], []), review);
  fireEvent.click(
    await view.findByRole("button", { name: "Reply to the discussion on new line 11" }),
  );
  await view.findByLabelText("Reply body for the discussion on new line 11");
  assert.equal(
    view.container.querySelectorAll(".diff-thread-immediate").length,
    0,
  );
});

test("two threads on one line are named by their place among them", async () => {
  const review = "review-thread-ordinals";
  const calls = [];
  const view = renderDiff(
    threadBridge(
      review,
      [[thread("d1"), thread("d2", { body: "And this one?" })]],
      calls,
    ),
    review,
  );
  await view.findByLabelText("Discussion thread 1 of 2 on new line 11");
  await view.findByLabelText("Discussion thread 2 of 2 on new line 11");
  assert.deepEqual(
    [...view.container.querySelectorAll(".diff-thread")].map((node) =>
      node.getAttribute("aria-label"),
    ),
    [
      "Discussion thread 1 of 2 on new line 11",
      "Discussion thread 2 of 2 on new line 11",
    ],
  );
  assert.deepEqual(
    [...view.container.querySelectorAll(".diff-thread-heading .button")].map(
      (node) => node.getAttribute("aria-label"),
    ),
    [
      "Reply to discussion 1 of 2 on new line 11",
      "Reply to discussion 2 of 2 on new line 11",
    ],
  );

  // Answering the second one reaches the second one.
  fireEvent.click(
    view.getByRole("button", { name: "Reply to discussion 2 of 2 on new line 11" }),
  );
  fireEvent.change(
    await view.findByLabelText("Reply body for discussion 2 of 2 on new line 11"),
    { target: { value: "The second one" } },
  );
  fireEvent.click(view.getByRole("button", { name: "Reply now" }));
  await waitFor(() => assert.equal(calls.length, 1));
  assert.deepEqual(calls, [["reply", "d2", "The second one"]]);
});

function splitCell(view, side, line) {
  const cell = view.container.querySelector(
    `.split-cell[data-anchor-side="${side}"][data-${side}-line="${line}"]`,
  );
  assert.ok(cell, `no ${side} split cell for line ${line}`);
  return cell;
}

function splitNumber(view, side, line) {
  const number = splitCell(view, side, line).querySelector(".line-number");
  assert.ok(number, `no ${side} number column for line ${line}`);
  return number;
}

function lineAnchors(view) {
  return view.container.querySelectorAll(".line-anchor");
}

function lineAnchor(view, side, line) {
  const anchor = view.container.querySelector(
    `.line-anchor[aria-label="Select ${side} line ${line}"]`,
  );
  assert.ok(anchor, `no ${side} line ${line} anchor`);
  return anchor;
}

function recorder(review, saves) {
  return async (params) => {
    saves.push(params);
    return draft(review, params.expected_version + 1, params.content.comments);
  };
}

function wideBridge(review, changes = {}) {
  return diffBridge(review, {
    openDiff: (params) => read(widePage(review, params.layout ?? "unified")),
    ...changes,
  });
}

function renderDiff(bridge, review, forge = "github") {
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
          { handle: "repo", display_name: "example/repo", forge_type: forge },
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
    openDiff: (params) => read(diffPage(review, params.layout ?? "unified")),
    pageDiff: () => {
      throw new Error("the fixture diff has one page");
    },
    getReviewMutationCapabilities: () =>
      read({ review, capabilities: capabilities() }),
    // The diff reads the published discussions to place its thread rows, so
    // every diff bridge answers that read.
    listDiscussions: () => read({ discussions: [] }),
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [] }),
    getReviewDraft: () => read(draft(review, 1, [])),
    createReviewDraft: async () => draft(review, 1, []),
    saveReviewDraft: async (params) =>
      draft(review, params.expected_version + 1, params.content.comments),
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

/** One stored inline entry on the fixture file, anchored on the new side. */
function inlineEntry(id, body, newLine, anchorChanges = {}) {
  return {
    id,
    kind: "inline",
    body,
    anchor: {
      revision: REVISION,
      old_path: "src/calc.py",
      new_path: "src/calc.py",
      old_line: null,
      new_line: newLine,
      side: "new",
      context_fingerprint: "f".repeat(64),
      start_line: null,
      start_side: null,
      stale: false,
      ...anchorChanges,
    },
  };
}

function draft(review, version, comments) {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    review,
    revision: REVISION,
    version,
    body: "",
    verdict: null,
    comments,
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
      web_url: "https://github.com/example/repo/pull/46",
      comment_count: 0,
      has_conflicts: false,
      labels: [],
      review_decision: null,
      additions: 1,
      deletions: 1,
    },
  };
}

/**
 * One published discussion anchored to a diff line, in the shape the sidecar
 * returns it. A review-level discussion is asked for with `inline: false`.
 */
function thread(id, changes = {}) {
  const {
    line = 11,
    side = "new",
    path = "src/calc.py",
    resolved = false,
    resolvable = true,
    body = "Should this log?",
    author = "alustosa",
    replies = [],
    replyAuthor = "author",
    inline = true,
  } = changes;
  const comment = (suffix, commentBody, commentAuthor) => ({
    id: `${id}-${suffix}`,
    author: { username: commentAuthor, display_name: "" },
    body: commentBody,
    created_at: "2026-09-09T00:00:00Z",
    file_path: inline ? path : null,
    old_line: side === "old" && suffix === "root" ? line : null,
    new_line: side === "new" && suffix === "root" ? line : null,
    is_resolved: resolved,
    replies: [],
  });
  return {
    id,
    is_inline: inline,
    is_resolved: resolved,
    resolvable,
    root_comment: {
      ...comment("root", body, author),
      replies: replies.map((text, index) =>
        comment(`reply-${index}`, text, replyAuthor),
      ),
    },
  };
}

/**
 * The diff bridge plus the review's published discussions, with the reply and
 * resolve mutations recorded in order. `pages` hands out one discussion list
 * per read, so a test can state what the reread after a reply returns.
 */
function threadBridge(review, pages, calls, changes = {}) {
  let index = 0;
  return diffBridge(review, {
    listDiscussions: () => {
      const discussions = pages[Math.min(index, pages.length - 1)];
      index += 1;
      return read({ discussions });
    },
    replyReviewDiscussion: async (params) => {
      calls.push(["reply", params.discussion_id, params.body]);
      return mutation(params.operation_id);
    },
    resolveReviewDiscussion: async (params) => {
      calls.push(["resolve", params.discussion_id, params.resolved]);
      return mutation(params.operation_id);
    },
    ...changes,
  });
}

/** One file of `lineCount` contiguous new-side lines, to fill several windows. */
function manyLinesPage(review, layout, lineCount) {
  const page = diffPage(review, layout);
  const [file, hunk] = page.entries;
  const sources = [];
  for (let line = 1; line <= lineCount; line += 1)
    sources.push({
      old_line: null,
      new_line: line,
      content: `    step(${line})`,
      line_type: "addition",
    });
  return {
    ...page,
    snapshot_id: `snapshot-many-${layout}`,
    entries: [
      file,
      {
        ...hunk,
        header: `@@ -1,0 +1,${lineCount} @@`,
        old_start: 1,
        old_count: 0,
        new_start: 1,
        new_count: lineCount,
      },
      ...toEntries(sources, layout),
    ],
  };
}

function truncatedDiffPage(review, layout) {
  const page = diffPage(review, layout);
  const [file, ...rest] = page.entries;
  return {
    ...page,
    snapshot_id: `snapshot-truncated-${layout}`,
    entries: [{ ...file, is_truncated: true }, ...rest],
  };
}

/** The wide hunk plus a second one, to drag across the boundary between them. */
function twoHunkPage(review, layout) {
  const page = widePage(review, layout);
  const second = [
    {
      old_line: 30,
      new_line: 30,
      content: "def main():",
      line_type: "context",
    },
    {
      old_line: null,
      new_line: 31,
      content: "    divide(1, 0)",
      line_type: "addition",
    },
  ];
  return {
    ...page,
    snapshot_id: `snapshot-two-hunk-${layout}`,
    entries: [
      ...page.entries,
      {
        kind: "hunk",
        file_index: 0,
        hunk_index: 1,
        header: "@@ -30,1 +30,2 @@",
        old_start: 30,
        old_count: 1,
        new_start: 30,
        new_count: 2,
        context_text: "def main():",
      },
      ...toEntries(second, layout).map((entry) => ({
        ...entry,
        hunk_index: 1,
        ...(entry.kind === "split" ? { row_index: entry.row_index + 5 } : {}),
      })),
    ],
  };
}

function truncatedWidePage(review, layout) {
  const page = widePage(review, layout);
  const [file, ...rest] = page.entries;
  return {
    ...page,
    snapshot_id: `snapshot-wide-truncated-${layout}`,
    entries: [{ ...file, is_truncated: true }, ...rest],
  };
}

/** One hunk with four contiguous new-side lines, so a range spans three. */
function widePage(review, layout) {
  const page = diffPage(review, layout);
  const [file, hunk] = page.entries;
  const sources = [
    {
      old_line: 10,
      new_line: 10,
      content: "def divide(a, b):",
      line_type: "context",
    },
    {
      old_line: null,
      new_line: 11,
      content: "    if b == 0:",
      line_type: "addition",
    },
    {
      old_line: null,
      new_line: 12,
      content: '        raise ValueError("b")',
      line_type: "addition",
    },
    {
      old_line: null,
      new_line: 13,
      content: "    return a / b",
      line_type: "addition",
    },
    {
      old_line: 11,
      new_line: null,
      content: "    return a/b",
      line_type: "deletion",
    },
  ];
  return {
    ...page,
    snapshot_id: `snapshot-wide-${layout}`,
    entries: [
      file,
      { ...hunk, header: "@@ -10,2 +10,4 @@", old_count: 2, new_count: 4 },
      ...toEntries(sources, layout),
    ],
  };
}

function toEntries(sources, layout) {
  return layout === "split"
    ? sources.map((source, index) => ({
        kind: "split",
        file_index: 0,
        hunk_index: 0,
        row_index: index,
        old:
          source.old_line === null ? null : { ...source, anchor_side: "old" },
        new:
          source.new_line === null ? null : { ...source, anchor_side: "new" },
      }))
    : sources.map((source) => ({
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        ...source,
      }));
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
    { old_line: 12, new_line: null, content: "    return a / b", line_type: "deletion" },
  ];
  const entries =
    layout === "split"
      ? sources.map((source, index) => ({
          kind: "split",
          file_index: 0,
          hunk_index: 0,
          row_index: index,
          old:
            source.old_line === null
              ? null
              : { ...source, anchor_side: "old" },
          new:
            source.new_line === null
              ? null
              : { ...source, anchor_side: "new" },
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
