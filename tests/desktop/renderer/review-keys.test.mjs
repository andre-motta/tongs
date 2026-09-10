import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { createDiffFeature } from "../../../desktop/dist/src/renderer/features/diff/index.js";
import { clearPendingEdit } from "../../../desktop/dist/src/renderer/features/review/drawer.js";
import {
  REVIEW_KEY_PARITY,
  isTextEntry,
  reviewKeyMatches,
  reviewKeyParityTable,
} from "../../../desktop/dist/src/renderer/features/review/keys.js";

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

/** The line-number button for a side and line. It shares its accessible name
 * with the row's code column, so it is reached by the class instead. */
function lineAnchor(view, label) {
  return view.container.querySelector(`.line-anchor[aria-label="${label}"]`);
}

/** The path the diff is showing, which is what `]` and `[` move. */
function shownFile(view) {
  return view.container.querySelector(".diff-view .file-path").textContent;
}

/** The row `n` and `p` last landed on, read back from where the focus went. */
function focusedRow() {
  const row = document.activeElement?.closest("[data-review-row]") ?? null;
  return row === null ? null : row.getAttribute("data-review-row");
}

/**
 * Waits until every control the review keyboard map presses through has an
 * answer from `getReviewMutationCapabilities` behind it. The composer's
 * quick and primary writes (composer.tsx's `quickReason` / `draftReason`)
 * and the thread Reply button (thread.tsx's `replyReason`) all carry a
 * title ending exactly this way for as long as that read is still in
 * flight, and a key dispatched into that window finds a disabled control
 * and does nothing, with nothing later to retry it: that is the shape of
 * the hosted-runner flake in #235. Counted rather than matched against a
 * node, the same shape review-components.test.mjs's settledCapabilities
 * uses for #215.
 *
 * A count of zero also satisfies this wait, so it proves nothing on its own
 * about a surface that has not rendered yet: the caller must already have
 * waited for the gated control itself to exist (a row count, a
 * `findByLabelText`) before calling this. All three call sites in this file
 * do.
 */
async function capabilitiesReady(view) {
  await waitFor(() => {
    const loading = [...view.container.querySelectorAll("button")].filter(
      (button) => button.title.endsWith("is still loading."),
    ).length;
    assert.equal(loading, 0);
  });
}

/**
 * Waits until the drawer's verdict tiles have rendered from an answered
 * capability read. `v` cycles `allowedVerdicts(capabilities)` (drawer.tsx),
 * which stays empty for as long as `capabilities` is null, so a `v`
 * dispatched before this settles finds no verdict to move to and nothing
 * later retries it.
 */
async function verdictTilesReady(view) {
  await waitFor(() => {
    const tiles = view.container.querySelectorAll(
      ".review-drawer-verdict-tiles input",
    ).length;
    assert.notEqual(tiles, 0);
  });
}

test("the capability-loading sentence capabilitiesReady keys on matches composer.tsx exactly", async () => {
  const review = "keys-capabilities-loading";
  let release = () => {};
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const view = renderDiff(
    diffBridge(review, {
      listDiscussions: () => read({ discussions: [thread("d-1", { line: 10 })] }),
      getReviewMutationCapabilities: () => ({
        requestToken: crypto.randomUUID(),
        result: gate.then(() => ({ review, capabilities: capabilities() })),
      }),
    }),
    review,
  );
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll("[data-review-row]").length,
      1,
    ),
  );

  // The Reply button renders unconditionally in the thread header
  // (thread.tsx), so while the capability read above is held open its
  // title is exactly what capabilitiesReady's suffix match is meant to
  // catch (composer.tsx:1315-1320). Pinning the literal sentence here
  // means a reword that drops the "is still loading." suffix, or changes
  // the sentence itself, fails this assertion loudly instead of leaving
  // capabilitiesReady to quietly stop matching anything and #235 to
  // reopen with no test naming the cause.
  const loading = [...view.container.querySelectorAll("button")].filter(
    (button) => button.title.endsWith("is still loading."),
  );
  assert.equal(loading.length, 1);
  assert.equal(
    loading[0].title,
    "Reply support for this review is still loading.",
  );

  release();
  await capabilitiesReady(view);
});

test("c opens the composer on the current selection from the document body", async () => {
  const review = "keys-comment";
  const view = renderDiff(diffBridge(review), review);
  await view.findByRole("button", { name: "Comment on new line 11" });
  fireEvent.click(lineAnchor(view, "Select new line 11"));
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 0);

  // Nothing is focused: the key is delivered to the body and never bubbles
  // through the React tree, which is the whole reason the map is on the
  // document.
  document.body.focus();
  fireEvent.keyDown(document.body, { key: "c" });
  await view.findByLabelText("Inline comment composer");
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 1);
  assert.equal(
    view.getByText("src/calc.py, new line 11").textContent,
    "src/calc.py, new line 11",
  );
});

test("c composes on the whole range and leaves the selection as the reader built it", async () => {
  const review = "keys-comment-range";
  const view = renderDiff(diffBridge(review), review);
  await view.findByRole("button", { name: "Comment on new line 10" });
  fireEvent.click(lineAnchor(view, "Select new line 10"));
  fireEvent.click(lineAnchor(view, "Select new line 11"), { shiftKey: true });
  assert.equal(view.container.querySelectorAll(".line-selected").length, 2);

  fireEvent.keyDown(document.body, { key: "c" });
  await view.findByLabelText("Inline comment composer");
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 1);
  assert.equal(view.container.querySelectorAll(".line-selected").length, 2);
  assert.equal(
    view.getByText("src/calc.py, Lines 10 to 11 (new)").textContent,
    "src/calc.py, Lines 10 to 11 (new)",
  );
});

test("Shift+C opens the review drawer from the document body", async () => {
  const review = "keys-drawer";
  const view = renderDiff(diffBridge(review), review);
  await view.findByRole("button", { name: /Your review/ });
  assert.equal(view.container.querySelectorAll(".review-drawer").length, 0);

  // A lower case c with no Shift is the comment key, not this one.
  fireEvent.keyDown(document.body, { key: "C", shiftKey: true });
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".review-drawer").length, 1),
  );
  assert.equal(
    view.getByRole("dialog", { name: "Your review" }).getAttribute("tabindex"),
    "-1",
  );
});

test("] and [ step the changed files from the document body and wrap", async () => {
  const review = "keys-files";
  const view = renderDiff(diffBridge(review), review);
  await waitFor(() => assert.equal(shownFile(view), "src/calc.py"));

  fireEvent.keyDown(document.body, { key: "]" });
  await waitFor(() => assert.equal(shownFile(view), "docs/guide.md"));
  // Wrapping is stated in the documentation, so it is asserted here.
  fireEvent.keyDown(document.body, { key: "]" });
  await waitFor(() => assert.equal(shownFile(view), "src/calc.py"));
  fireEvent.keyDown(document.body, { key: "[" });
  await waitFor(() => assert.equal(shownFile(view), "docs/guide.md"));
  fireEvent.keyDown(document.body, { key: "[" });
  await waitFor(() => assert.equal(shownFile(view), "src/calc.py"));
});

test("n and p step the threads and pending cards in document order and wrap", async () => {
  const review = "keys-rows";
  const view = renderDiff(
    diffBridge(review, {
      listDiscussions: () =>
        read({ discussions: [thread("d-1", { line: 10 }), thread("d-2", { line: 11 })] }),
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 4, [inlineEntry("entry-a", "Float division", 11)])],
        }),
    }),
    review,
  );
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll("[data-review-row]").length,
      3,
    ),
  );
  assert.deepEqual(
    [...view.container.querySelectorAll("[data-review-row]")].map((row) =>
      row.getAttribute("data-review-row"),
    ),
    ["thread:d-1", "thread:d-2", "pending:entry-a"],
  );

  fireEvent.keyDown(document.body, { key: "n" });
  assert.equal(focusedRow(), "thread:d-1");
  fireEvent.keyDown(document.body, { key: "n" });
  assert.equal(focusedRow(), "thread:d-2");
  fireEvent.keyDown(document.body, { key: "n" });
  assert.equal(focusedRow(), "pending:entry-a");
  fireEvent.keyDown(document.body, { key: "n" });
  assert.equal(focusedRow(), "thread:d-1");
  fireEvent.keyDown(document.body, { key: "p" });
  assert.equal(focusedRow(), "pending:entry-a");
  fireEvent.keyDown(document.body, { key: "p" });
  assert.equal(focusedRow(), "thread:d-2");
});

test("r replies to the thread the row cursor is on", async () => {
  const review = "keys-reply";
  const view = renderDiff(
    diffBridge(review, {
      listDiscussions: () =>
        read({ discussions: [thread("d-1", { line: 10 }), thread("d-2", { line: 11 })] }),
    }),
    review,
  );
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll("[data-review-row]").length,
      2,
    ),
  );
  fireEvent.keyDown(document.body, { key: "n" });
  fireEvent.keyDown(document.body, { key: "n" });
  assert.equal(focusedRow(), "thread:d-2");

  // The Reply button on this row refuses with "Reply support for this
  // review is still loading." for as long as getReviewMutationCapabilities
  // is in flight, and `r` reads that same refusal (thread.tsx's
  // replyReason), so a press before it settles finds the thread but has
  // nothing to do; there is no second `r` to retry it (#235).
  await capabilitiesReady(view);

  fireEvent.keyDown(document.body, { key: "r" });
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll(".diff-thread-composer").length,
      1,
    ),
  );
  assert.equal(
    view.container.querySelectorAll(
      '[data-review-row="thread:d-2"] .diff-thread-composer',
    ).length,
    1,
  );
});

test("r does nothing when the cursor is on a pending card rather than a thread", async () => {
  const review = "keys-reply-pending";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 4, [inlineEntry("entry-a", "Float division", 11)])],
        }),
    }),
    review,
  );
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll("[data-review-row]").length,
      1,
    ),
  );
  fireEvent.keyDown(document.body, { key: "n" });
  assert.equal(focusedRow(), "pending:entry-a");
  fireEvent.keyDown(document.body, { key: "r" });
  assert.equal(
    view.container.querySelectorAll(".diff-thread-composer").length,
    0,
  );
});

test("Ctrl+Enter and Cmd+Enter run the composer's primary action from outside it", async () => {
  const review = "keys-primary";
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
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  // The composer took focus when it opened; the key is being typed after the
  // reader clicked back onto the diff behind it.
  lineAnchor(view, "Select new line 11").focus();
  assert.equal(
    document.activeElement.getAttribute("aria-label"),
    "Select new line 11",
  );
  // The composer's primary write button refuses with "Inline comment
  // support for this review is still loading." until the same capability
  // read answers, and Ctrl+Enter presses that button rather than reaching
  // into the composer (keys.ts's pressComposerPrimary), so it needs the
  // same settle as `r` above.
  await capabilitiesReady(view);
  fireEvent.keyDown(document.body, { key: "Enter", ctrlKey: true });
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(saves[0].content.comments[0].body, "Guard the zero divisor");
  assert.equal(saves[0].content.comments[0].anchor.new_line, 11);

  // The macOS half of the same binding.
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 10" }));
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Name the context line" },
  });
  lineAnchor(view, "Select new line 11").focus();
  fireEvent.keyDown(document.body, { key: "Enter", metaKey: true });
  await waitFor(() => assert.equal(saves.length, 2));
  assert.equal(saves[1].content.comments[1].body, "Name the context line");
});

test("Ctrl+Enter reaches the composer from a focused diff row, which does not activate", async () => {
  const review = "keys-primary-row";
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
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });
  // The row is where the browser delivers the key when the reader has clicked
  // back onto the diff. A plain Enter selects the row; a held Ctrl belongs to
  // the composer, so the row must leave it alone rather than selecting itself
  // and swallowing the write.
  const row = lineAnchor(view, "Select new line 10");
  row.focus();
  // Same capability race as the two tests above: the primary write is
  // pressed rather than reached into, and it refuses while the read is
  // still in flight.
  await capabilitiesReady(view);
  fireEvent.keyDown(row, { key: "Enter", ctrlKey: true });
  await waitFor(() => assert.equal(saves.length, 1));
  assert.equal(saves[0].content.comments[0].anchor.new_line, 11);
  assert.equal(row.getAttribute("aria-pressed"), "false");

  // A plain Enter on the same row still selects it.
  fireEvent.keyDown(row, { key: "Enter" });
  await waitFor(() => assert.equal(row.getAttribute("aria-pressed"), "true"));
});

test("Ctrl+Enter stays refused while the composer's own primary action is refused", async () => {
  const review = "keys-primary-refused";
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
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  // An empty body disables the button, and the key presses the same button, so
  // it carries the same refusal rather than a second copy of the rule.
  lineAnchor(view, "Select new line 11").focus();
  fireEvent.keyDown(document.body, { key: "Enter", ctrlKey: true });
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".inline-composer").length, 1),
  );
  assert.equal(saves.length, 0);
});

test("Escape closes the composer from outside the diff rows and keeps the text", async () => {
  const review = "keys-escape";
  const view = renderDiff(diffBridge(review), review);
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.change(await view.findByLabelText("Inline review comment"), {
    target: { value: "Guard the zero divisor" },
  });

  // The carry-over from PR #199: Escape used to be bound to the non-focusable
  // `.diff-view` section, so it did nothing once the focus left the diff rows.
  // The changed-file navigation is one of the places it did nothing.
  view.container.querySelector(".file-list .file-item").focus();
  fireEvent.keyDown(document.body, { key: "Escape" });
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".inline-composer").length, 0),
  );

  // The text belongs to the anchor, not to the composer, so it comes back.
  fireEvent.click(view.getByRole("button", { name: "Comment on new line 11" }));
  assert.equal(
    (await view.findByLabelText("Inline review comment")).value,
    "Guard the zero divisor",
  );

  // And from the layout toggle, the other place named in the carry-over.
  view.getByRole("button", { name: "Split" }).focus();
  fireEvent.keyDown(document.body, { key: "Escape" });
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".inline-composer").length, 0),
  );
});

test("Escape typed inside the composer is still answered by the composer's own stages", async () => {
  const review = "keys-escape-stages";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 4, [inlineEntry("entry-a", "Float division", 10)])],
        }),
    }),
    review,
  );
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  fireEvent.click(
    await view.findByRole("button", { name: "More review actions" }),
  );
  assert.equal(
    view.container.querySelectorAll('[aria-label="Review actions"]').length,
    1,
  );
  fireEvent.click(view.getByRole("button", { name: "Discard review" }));
  assert.equal(
    view.getAllByRole("button", { name: /Confirm discard of/ }).length,
    1,
  );

  // Escape answers the nearest question first: the armed discard, then the
  // overflow, then the composer. The document map never sees any of these,
  // because the composer's own handler prevents the default.
  const composer = view.getByLabelText("Inline comment composer");
  fireEvent.keyDown(composer, { key: "Escape" });
  assert.equal(
    view.container.querySelectorAll(".button-danger").length,
    1,
  );
  assert.equal(
    view.queryAllByRole("button", { name: /Confirm discard of/ }).length,
    0,
  );
  fireEvent.keyDown(composer, { key: "Escape" });
  assert.equal(
    view.container.querySelectorAll('[aria-label="Review actions"]').length,
    0,
  );
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 1);
  fireEvent.keyDown(composer, { key: "Escape" });
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 0);
});

test("v cycles the verdict in the drawer, among the verdicts this review allows", async () => {
  const review = "keys-verdict";
  const view = renderDiff(
    diffBridge(review, {
      listReviewDrafts: () =>
        read({
          cursor: 0,
          next_cursor: null,
          drafts: [draft(review, 4, [inlineEntry("entry-a", "Float division", 11)])],
        }),
      getReviewMutationCapabilities: () =>
        read({ review, capabilities: capabilities({ request_changes: false }) }),
    }),
    review,
  );
  await view.findByRole("button", { name: /Your review/ });
  fireEvent.keyDown(document.body, { key: "C", shiftKey: true });
  await waitFor(() =>
    assert.equal(
      view.container.querySelectorAll(".review-drawer-verdict-tiles input")
        .length,
      2,
    ),
  );
  assert.equal(
    view.container.querySelectorAll(
      ".review-drawer-verdict-tiles input:checked",
    ).length,
    0,
  );

  fireEvent.keyDown(document.body, { key: "v" });
  await waitFor(() => assert.equal(checkedVerdict(view), "comment"));
  fireEvent.keyDown(document.body, { key: "v" });
  await waitFor(() => assert.equal(checkedVerdict(view), "approve"));
  // Request changes is not offered on this review, so the cycle wraps over it.
  fireEvent.keyDown(document.body, { key: "v" });
  await waitFor(() => assert.equal(checkedVerdict(view), "comment"));
});

/** A review with a draft and one pending entry, so the drawer has content. */
function drawerBridge(review, changes = {}) {
  return diffBridge(review, {
    listReviewDrafts: () =>
      read({
        cursor: 0,
        next_cursor: null,
        drafts: [draft(review, 4, [inlineEntry("entry-a", "Float division", 11)])],
      }),
    ...changes,
  });
}

async function openDrawer(view) {
  await view.findByRole("button", { name: /Your review/ });
  fireEvent.keyDown(document.body, { key: "C", shiftKey: true });
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".review-drawer").length, 1),
  );
}

test("the open drawer owns the keyboard from the diff behind it", async () => {
  const review = "keys-drawer-owns";
  const view = renderDiff(drawerBridge(review), review);
  await openDrawer(view);

  // The drawer is a dialog without aria-modal, so the diff behind it stays
  // clickable and the focus can leave the panel. Its keys have to follow.
  lineAnchor(view, "Select new line 11").focus();
  assert.equal(
    document.activeElement.getAttribute("aria-label"),
    "Select new line 11",
  );
  // The tiles `v` cycles through are empty until getReviewMutationCapabilities
  // answers (drawer.tsx's allowedVerdicts), so a press before that leaves
  // the verdict unset with no later press to retry it.
  await verdictTilesReady(view);
  fireEvent.keyDown(document.body, { key: "v" });
  await waitFor(() => assert.equal(checkedVerdict(view), "comment"));

  // The diff's own keys stand down for as long as the drawer is open.
  assert.equal(shownFile(view), "src/calc.py");
  fireEvent.keyDown(document.body, { key: "]" });
  assert.equal(shownFile(view), "src/calc.py");

  fireEvent.keyDown(document.body, { key: "Escape" });
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".review-drawer").length, 0),
  );
  assert.equal(
    document.activeElement.className,
    "button button-secondary review-drawer-toggle",
  );
  // And the diff has its keys back.
  fireEvent.keyDown(document.body, { key: "]" });
  await waitFor(() => assert.equal(shownFile(view), "docs/guide.md"));
});

test("the open drawer answers v and Escape from the document body", async () => {
  const review = "keys-drawer-body";
  const view = renderDiff(drawerBridge(review), review);
  await openDrawer(view);
  document.body.focus();

  // Same capability race as the sibling test above.
  await verdictTilesReady(view);
  fireEvent.keyDown(document.body, { key: "v" });
  await waitFor(() => assert.equal(checkedVerdict(view), "comment"));
  fireEvent.keyDown(document.body, { key: "Escape" });
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".review-drawer").length, 0),
  );
  assert.equal(
    document.activeElement.className,
    "button button-secondary review-drawer-toggle",
  );
});

test("Escape in the drawer disarms a confirmation before it closes anything", async () => {
  const review = "keys-drawer-confirm";
  const view = renderDiff(drawerBridge(review), review);
  await openDrawer(view);
  fireEvent.click(view.getByRole("button", { name: "Discard review" }));
  assert.equal(
    view.getAllByRole("button", { name: /Confirm discard of/ }).length,
    1,
  );

  // The first press answers the nearest question and the drawer stays open,
  // whether the focus is inside the panel or out on the diff behind it.
  lineAnchor(view, "Select new line 11").focus();
  fireEvent.keyDown(document.body, { key: "Escape" });
  assert.equal(
    view.queryAllByRole("button", { name: /Confirm discard of/ }).length,
    0,
  );
  assert.equal(view.container.querySelectorAll(".review-drawer").length, 1);

  fireEvent.keyDown(document.body, { key: "Escape" });
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll(".review-drawer").length, 0),
  );
});

test("the drawer keys keep their guards while the drawer owns the keyboard", async () => {
  const review = "keys-drawer-guards";
  const view = renderDiff(drawerBridge(review), review);
  await openDrawer(view);

  // The Summary field is a text entry, so v types rather than cycling.
  const summary = view.container.querySelector(".review-drawer-summary textarea");
  summary.focus();
  fireEvent.keyDown(document.body, { key: "v" });
  assert.equal(checkedVerdict(view), null);
  assert.equal(view.container.querySelectorAll(".review-drawer").length, 1);

  // A modal dialog owns the keyboard even over this one.
  summary.blur();
  assert.equal(document.activeElement.tagName, "BODY");
  const modal = document.createElement("div");
  modal.setAttribute("aria-modal", "true");
  modal.setAttribute("role", "alertdialog");
  document.body.append(modal);
  try {
    fireEvent.keyDown(document.body, { key: "v" });
    fireEvent.keyDown(document.body, { key: "Escape" });
    assert.equal(checkedVerdict(view), null);
    assert.equal(view.container.querySelectorAll(".review-drawer").length, 1);
  } finally {
    modal.remove();
  }
  // This press is the one expected to land, so it needs the same capability
  // settle as the two sibling tests above.
  await verdictTilesReady(view);
  fireEvent.keyDown(document.body, { key: "v" });
  await waitFor(() => assert.equal(checkedVerdict(view), "comment"));
});

test("] and [ stay unclaimed on a review with one changed file", async () => {
  const review = "keys-one-file";
  const view = renderDiff(
    diffBridge(review, { openDiff: () => read(onePageDiff(review)) }),
    review,
  );
  await waitFor(() => assert.equal(shownFile(view), "src/calc.py"));
  assert.equal(view.container.querySelectorAll(".file-list .file-item").length, 2);
  for (const key of ["]", "["]) {
    const event = new window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
    document.body.dispatchEvent(event);
    assert.equal(event.defaultPrevented, false);
  }
  assert.equal(shownFile(view), "src/calc.py");
});

test("n and p walk the split panes in the order the documentation states", async () => {
  const review = "keys-split-order";
  const view = renderDiff(
    diffBridge(review, {
      listDiscussions: () =>
        read({
          discussions: [
            thread("d-new", { line: 11, side: "new" }),
            thread("d-old", { line: 12, side: "old" }),
          ],
        }),
    }),
    review,
  );
  fireEvent.click(await view.findByRole("button", { name: "Split" }));
  await waitFor(() =>
    assert.equal(view.container.querySelectorAll("[data-review-row]").length, 2),
  );
  // The panes are independent columns, so the old pane's rows come first. The
  // documentation states exactly this rather than claiming screen order.
  assert.deepEqual(
    [...view.container.querySelectorAll("[data-review-row]")].map((row) =>
      row.getAttribute("data-review-row"),
    ),
    ["thread:d-old", "thread:d-new"],
  );
  fireEvent.keyDown(document.body, { key: "n" });
  assert.equal(focusedRow(), "thread:d-old");
  fireEvent.keyDown(document.body, { key: "n" });
  assert.equal(focusedRow(), "thread:d-new");
});

test("isTextEntry names every control the map stands down for", () => {
  const make = (html) => {
    const host = document.createElement("div");
    host.innerHTML = html;
    return host.firstElementChild;
  };
  assert.equal(isTextEntry(null), false);
  assert.equal(isTextEntry(make("<input />")), true);
  assert.equal(isTextEntry(make("<textarea></textarea>")), true);
  assert.equal(isTextEntry(make("<select></select>")), true);
  assert.equal(isTextEntry(make('<div contenteditable="true"></div>')), true);
  assert.equal(isTextEntry(make('<div contenteditable=""></div>')), true);
  // A focused descendant of an editable host is still inside the text entry.
  assert.equal(
    isTextEntry(make('<div contenteditable="true"><span>x</span></div>').firstElementChild),
    true,
  );
  assert.equal(isTextEntry(make("<div></div>")), false);
  assert.equal(isTextEntry(make('<div contenteditable="false"></div>')), false);
  assert.equal(isTextEntry(make("<button></button>")), false);
});

test("the review keys stand down while a text field holds the keyboard", async () => {
  const review = "keys-text-entry";
  const view = renderDiff(diffBridge(review), review);
  fireEvent.click(
    await view.findByRole("button", { name: "Comment on new line 11" }),
  );
  const editor = await view.findByLabelText("Inline review comment");
  editor.focus();
  assert.equal(document.activeElement.tagName, "TEXTAREA");

  for (const key of ["c", "]", "[", "n", "p", "r", "v", "Escape"])
    fireEvent.keyDown(document.body, { key });
  fireEvent.keyDown(document.body, { key: "C", shiftKey: true });

  assert.equal(shownFile(view), "src/calc.py");
  assert.equal(view.container.querySelectorAll(".review-drawer").length, 0);
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 1);
  assert.equal(document.activeElement.tagName, "TEXTAREA");

  // The same for the other three shapes of text entry, focused inside the
  // region the map listens over.
  for (const html of [
    '<input type="text" />',
    "<select><option>a</option></select>",
    '<div contenteditable="true" tabindex="0"></div>',
  ]) {
    const host = document.createElement("div");
    host.innerHTML = html;
    const control = host.firstElementChild;
    view.container.append(control);
    control.focus();
    fireEvent.keyDown(document.body, { key: "]" });
    fireEvent.keyDown(document.body, { key: "C", shiftKey: true });
    assert.equal(shownFile(view), "src/calc.py");
    assert.equal(view.container.querySelectorAll(".review-drawer").length, 0);
    control.remove();
  }
});

test("the review keys stand down while an aria-modal dialog is on screen", async () => {
  const review = "keys-modal";
  const view = renderDiff(diffBridge(review), review);
  await waitFor(() => assert.equal(shownFile(view), "src/calc.py"));
  const modal = document.createElement("div");
  modal.setAttribute("aria-modal", "true");
  modal.setAttribute("role", "alertdialog");
  document.body.append(modal);
  try {
    for (const key of ["c", "]", "[", "n", "p", "r", "v", "Escape"])
      fireEvent.keyDown(document.body, { key });
    fireEvent.keyDown(document.body, { key: "C", shiftKey: true });
    fireEvent.keyDown(document.body, { key: "Enter", ctrlKey: true });
    assert.equal(shownFile(view), "src/calc.py");
    assert.equal(view.container.querySelectorAll(".review-drawer").length, 0);
    assert.equal(view.container.querySelectorAll(".inline-composer").length, 0);
    assert.equal(document.activeElement.tagName, "BODY");
  } finally {
    modal.remove();
  }
  // The same key acts once the dialog is gone.
  fireEvent.keyDown(document.body, { key: "]" });
  await waitFor(() => assert.equal(shownFile(view), "docs/guide.md"));
});

test("the review keys stand down when a modifier they do not name is held", async () => {
  const review = "keys-modifiers";
  const view = renderDiff(diffBridge(review), review);
  await view.findByRole("button", { name: "Comment on new line 11" });
  fireEvent.click(lineAnchor(view, "Select new line 11"));
  for (const event of [
    { key: "c", ctrlKey: true },
    { key: "c", metaKey: true },
    { key: "c", altKey: true },
    { key: "c", shiftKey: true },
    { key: "]", ctrlKey: true },
    { key: "]", altKey: true },
    { key: "n", metaKey: true },
    { key: "C", shiftKey: true, ctrlKey: true },
    { key: "Enter", altKey: true, ctrlKey: true },
    { key: "Escape", ctrlKey: true },
  ])
    fireEvent.keyDown(document.body, event);
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 0);
  assert.equal(shownFile(view), "src/calc.py");
  assert.equal(view.container.querySelectorAll(".review-drawer").length, 0);
});

test("reviewKeyMatches refuses every modifier a binding does not name", () => {
  const plain = { key: "c", run: () => true };
  const shifted = { key: "C", shift: true, run: () => true };
  const primary = { key: "Enter", primary: true, run: () => true };
  const event = (changes) => ({
    key: "c",
    shiftKey: false,
    ctrlKey: false,
    metaKey: false,
    altKey: false,
    ...changes,
  });
  assert.equal(reviewKeyMatches(event({}), plain), true);
  assert.equal(reviewKeyMatches(event({ ctrlKey: true }), plain), false);
  assert.equal(reviewKeyMatches(event({ metaKey: true }), plain), false);
  assert.equal(reviewKeyMatches(event({ altKey: true }), plain), false);
  assert.equal(reviewKeyMatches(event({ shiftKey: true }), plain), false);
  assert.equal(reviewKeyMatches(event({ key: "C", shiftKey: true }), shifted), true);
  assert.equal(reviewKeyMatches(event({ key: "C" }), shifted), false);
  assert.equal(
    reviewKeyMatches(event({ key: "Enter", ctrlKey: true }), primary),
    true,
  );
  assert.equal(
    reviewKeyMatches(event({ key: "Enter", metaKey: true }), primary),
    true,
  );
  assert.equal(reviewKeyMatches(event({ key: "Enter" }), primary), false);
  assert.equal(
    reviewKeyMatches(event({ key: "Enter", ctrlKey: true, altKey: true }), primary),
    false,
  );
});

test("Enter on the split gutter opens the composer and keeps the range", async () => {
  const review = "keys-split-gutter";
  const view = renderDiff(diffBridge(review), review);
  fireEvent.click(await view.findByRole("button", { name: "Split" }));
  await view.findByRole("button", { name: "Comment on new line 10" });
  fireEvent.click(view.getByRole("button", { name: "Select new line 10" }));
  fireEvent.click(view.getByRole("button", { name: "Select new line 11" }), {
    shiftKey: true,
  });
  assert.equal(view.container.querySelectorAll(".line-selected").length, 2);

  // The carry-over from PR #201: the keydown bubbled to the split cell, which
  // called preventDefault and re-selected the one row, so the range collapsed
  // and no activation click was ever synthesised for the button.
  fireEvent.keyDown(view.getByRole("button", { name: "Comment on new line 11" }), {
    key: "Enter",
  });
  await view.findByLabelText("Inline comment composer");
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 1);
  assert.equal(view.container.querySelectorAll(".line-selected").length, 2);
  assert.equal(
    view.getByText("src/calc.py, Lines 10 to 11 (new)").textContent,
    "src/calc.py, Lines 10 to 11 (new)",
  );
});

test("Space on the unified gutter opens the composer without collapsing the range", async () => {
  const review = "keys-unified-gutter";
  const view = renderDiff(diffBridge(review), review);
  await view.findByRole("button", { name: "Comment on new line 10" });
  fireEvent.click(lineAnchor(view, "Select new line 10"));
  fireEvent.click(lineAnchor(view, "Select new line 11"), { shiftKey: true });
  fireEvent.keyDown(view.getByRole("button", { name: "Comment on new line 11" }), {
    key: " ",
  });
  await view.findByLabelText("Inline comment composer");
  assert.equal(view.container.querySelectorAll(".inline-composer").length, 1);
  assert.equal(view.container.querySelectorAll(".line-selected").length, 2);
});

test("both documentation pages carry the same parity table the map publishes", () => {
  const table = reviewKeyParityTable();
  const pages = [
    "docs/reference/keybindings.md",
    "docs/desktop/workspace.md",
  ];
  for (const page of pages) {
    const text = readFileSync(
      new URL(`../../../${page}`, import.meta.url),
      "utf8",
    );
    assert.equal(
      text.includes(table),
      true,
      `${page} does not carry the parity table verbatim`,
    );
  }
  // Every key the map registers is named in the table.
  const keys = REVIEW_KEY_PARITY.map((row) => row.key).join(" ");
  for (const key of ["`c`", "Shift+C", "`]` / `[`", "`n` / `p`", "`r`", "Enter", "Esc", "`v`"])
    assert.equal(keys.includes(key), true, `${key} is missing from the table`);
});

function checkedVerdict(view) {
  const checked = view.container.querySelector(
    ".review-drawer-verdict-tiles input:checked",
  );
  return checked === null ? null : checked.value;
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
    listDiscussions: () => read({ discussions: [] }),
    listReviewDrafts: () => read({ cursor: 0, next_cursor: null, drafts: [] }),
    getReviewDraft: () => read(draft(review, 1, [])),
    createReviewDraft: async () => draft(review, 1, []),
    saveReviewDraft: async (params) =>
      draft(review, params.expected_version + 1, params.content.comments),
    postInlineReviewComment: async (params) => mutation(params.operation_id),
    replyReviewDiscussion: async (params) => mutation(params.operation_id),
    resolveReviewDiscussion: async (params) => mutation(params.operation_id),
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
    status: "accepted",
    detail: null,
    receipt: null,
  };
}

function thread(id, changes = {}) {
  const {
    line = 11,
    side = "new",
    path = "src/calc.py",
    resolved = false,
    resolvable = true,
    body = "Should this log?",
    author = "alustosa",
  } = changes;
  return {
    id,
    is_inline: true,
    is_resolved: resolved,
    resolvable,
    root_comment: {
      id: `${id}-root`,
      author: { username: author, display_name: "" },
      body,
      created_at: "2026-09-09T00:00:00Z",
      file_path: path,
      old_line: side === "old" ? line : null,
      new_line: side === "new" ? line : null,
      is_resolved: resolved,
      replies: [],
    },
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

/** The same fixture with the second file removed, for the one-file case. */
function onePageDiff(review) {
  const page = diffPage(review, "unified");
  return {
    ...page,
    entries: page.entries.filter((entry) => entry.file_index === 0),
  };
}

/**
 * Two changed files, so `]` and `[` have somewhere to go. The first file
 * carries the rows every other fixture in the suite uses.
 */
function diffPage(review, layout) {
  const calc = {
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
  const guide = {
    ...calc,
    file_index: 1,
    old_path: "docs/guide.md",
    new_path: "docs/guide.md",
    language: "markdown",
  };
  const hunk = (fileIndex, contextText) => ({
    kind: "hunk",
    file_index: fileIndex,
    hunk_index: 0,
    header: "@@ -10,3 +10,3 @@",
    old_start: 10,
    old_count: 3,
    new_start: 10,
    new_count: 3,
    context_text: contextText,
  });
  const calcSources = [
    { old_line: 10, new_line: 10, content: "def divide(a, b):", line_type: "context" },
    { old_line: null, new_line: 11, content: "    if b == 0:", line_type: "addition" },
    { old_line: 12, new_line: null, content: "    return a / b", line_type: "deletion" },
  ];
  const guideSources = [
    { old_line: 3, new_line: 3, content: "Usage", line_type: "context" },
    { old_line: null, new_line: 4, content: "Divide safely.", line_type: "addition" },
  ];
  const rows = (fileIndex, sources) =>
    layout === "split"
      ? sources.map((source, index) => ({
          kind: "split",
          file_index: fileIndex,
          hunk_index: 0,
          row_index: index,
          old:
            source.old_line === null ? null : { ...source, anchor_side: "old" },
          new:
            source.new_line === null ? null : { ...source, anchor_side: "new" },
        }))
      : sources.map((source) => ({
          kind: "line",
          file_index: fileIndex,
          hunk_index: 0,
          ...source,
        }));
  return {
    snapshot_id: `snapshot-${layout}`,
    resource: review,
    revision: REVISION,
    cursor: 0,
    next_cursor: null,
    entries: [
      calc,
      hunk(0, "def divide(a, b):"),
      ...rows(0, calcSources),
      guide,
      hunk(1, "Usage"),
      ...rows(1, guideSources),
    ],
  };
}
