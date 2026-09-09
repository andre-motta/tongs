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
    assert.equal(view.queryByLabelText("Inline comment composer"), null),
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

test("one recovered pending review is adopted rather than duplicated", async () => {
  const review = "review-composer-adopt";
  const creates = [];
  const saves = [];
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({ cursor: 0, next_cursor: null, drafts: [draft(review, 3, [])] }),
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
  assert.equal(creates.length, 0);
  assert.equal(saves[0].expected_version, 3);
  assert.equal(saves[0].content.comments.length, 1);
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
  fireEvent.click(view.getByRole("button", { name: "Start a review" }));
  await view.findByText(
    "The pending review is bound to an earlier revision. Migrate it in the review workflow before adding inline feedback.",
  );
  assert.equal(saves.length, 0);
  assert.equal(
    view.getByRole("button", { name: "Add to review" }).disabled,
    true,
  );
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
    assert.equal(view.queryByLabelText("Inline comment composer"), null),
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
    assert.equal(view.queryByLabelText("Inline comment composer"), null),
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
