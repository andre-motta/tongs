// Regression coverage for "/" search in the desktop pipelines, jobs and logs
// view. Issue #177: the view advertised "Press / to search", but the handler was
// bound to a React onKeyDown on the non-focusable ".ci-page" section, so a
// keystroke typed with nothing focused was delivered to the document body and
// never reached it. Desktop sibling of the TUI defect #168.
//
// Assertions here compare short strings and booleans rather than live DOM nodes,
// so a future regression reports a readable diff instead of serialising two jsdom
// trees inside the project's bounded test units.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { createPipelinesFeature } from "../../../desktop/dist/src/renderer/features/pipelines/index.js";
import {
  WorkspaceUtilityController,
  WorkspaceUtilityOverlay,
} from "../../../desktop/dist/src/renderer/features/utilities/index.js";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const React = desktopRequire("react");
const { JSDOM } = desktopRequire("jsdom");
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://app.invalid/",
});
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  HTMLInputElement: dom.window.HTMLInputElement,
  Node: dom.window.Node,
});
const { act, cleanup, fireEvent, render, waitFor } = desktopRequire(
  "@testing-library/react",
);
afterEach(cleanup);

const LOG_LINES = 700;
const MATCH_LINES = [5, 205, 405, 605];
const LOG_TEXT = `${[...Array(LOG_LINES).keys()]
  .map((index) =>
    MATCH_LINES.includes(index) ? `line ${index} error boom` : `line ${index} ok`,
  )
  .join("\n")}\n`;
const SEARCH = "INPUT.ci-log-search";
const BODY = "BODY";

test("slash focuses the log search when nothing in the view holds focus", async () => {
  const view = await openLog();

  assert.equal(focused(), BODY);
  assert.equal(fireEvent.keyDown(document.body, { key: "/" }), false);
  assert.equal(focused(), SEARCH);
  assert.equal(searchBox(view).value, "");
});

test("slash reaches the search from a focused control inside the view", async () => {
  const view = await openLog();
  view.getByRole("button", { name: "Refresh log" }).focus();
  assert.equal(focused(), "BUTTON[Refresh log]");

  fireEvent.keyDown(document.activeElement, { key: "/" });
  assert.equal(focused(), SEARCH);
});

test("slash is left to a focused text field instead of stealing the keystroke", async () => {
  const view = await openLog();
  searchBox(view).focus();

  assert.equal(fireEvent.keyDown(document.activeElement, { key: "/" }), true);
  assert.equal(focused(), SEARCH);
});

test("slash with a modifier is left to the platform", async () => {
  const view = await openLog();

  assert.equal(
    fireEvent.keyDown(document.body, { key: "/", ctrlKey: true }),
    true,
  );
  assert.equal(focused(), BODY);
  assert.equal(searchBox(view).value, "");
});

test("typing filters live and n and N step matches with a wrapping counter", async () => {
  const view = await openLog();
  const search = searchBox(view);

  fireEvent.keyDown(document.body, { key: "/" });
  fireEvent.change(search, { target: { value: "error" } });
  await matchCount(view, `1 of ${MATCH_LINES.length} matches`);
  assert.equal(selectedLine(view), MATCH_LINES[0] + 1);

  // n and N belong to the log, so Enter hands the keyboard back first.
  assert.equal(fireEvent.keyDown(search, { key: "n" }), true);
  await matchCount(view, `1 of ${MATCH_LINES.length} matches`);

  fireEvent.keyDown(search, { key: "Enter" });
  assert.equal(focused(), BODY);

  assert.equal(fireEvent.keyDown(document.body, { key: "n" }), false);
  await matchCount(view, `2 of ${MATCH_LINES.length} matches`);
  assert.equal(selectedLine(view), MATCH_LINES[1] + 1);

  fireEvent.keyDown(document.body, { key: "n" });
  await matchCount(view, `3 of ${MATCH_LINES.length} matches`);
  assert.equal(rowWindow(view), "301-600 of 700");

  fireEvent.keyDown(document.body, { key: "N" });
  await matchCount(view, `2 of ${MATCH_LINES.length} matches`);

  fireEvent.keyDown(document.body, { key: "N" });
  fireEvent.keyDown(document.body, { key: "N" });
  await matchCount(
    view,
    `${MATCH_LINES.length} of ${MATCH_LINES.length} matches`,
  );
  assert.equal(rowWindow(view), "601-700 of 700");

  fireEvent.keyDown(document.body, { key: "n" });
  await matchCount(view, `1 of ${MATCH_LINES.length} matches`);
});

test("n does nothing without matches", async () => {
  const view = await openLog();
  const search = searchBox(view);

  fireEvent.keyDown(document.body, { key: "/" });
  fireEvent.change(search, { target: { value: "no such text" } });
  fireEvent.keyDown(search, { key: "Enter" });
  await matchCount(view, "No matches");

  assert.equal(fireEvent.keyDown(document.body, { key: "n" }), true);
  await matchCount(view, "No matches");
});

test("escape closes the search and restores the row window and focus", async () => {
  const view = await openLog();
  const search = searchBox(view);
  const nextRows = view.getByRole("button", { name: "Next rows" });
  nextRows.focus();
  fireEvent.click(nextRows);
  assert.equal(rowWindow(view), "301-600 of 700");

  fireEvent.keyDown(nextRows, { key: "/" });
  fireEvent.change(search, { target: { value: "error" } });
  assert.equal(rowWindow(view), "1-300 of 700");
  await matchCount(view, `1 of ${MATCH_LINES.length} matches`);

  assert.equal(fireEvent.keyDown(search, { key: "Escape" }), false);
  assert.equal(search.value, "");
  await matchCount(view, "700 lines");
  assert.equal(rowWindow(view), "301-600 of 700");
  assert.equal(focused(), "BUTTON[Next rows]");
  assert.equal(view.container.querySelectorAll(".ci-log-match").length, 0);
});

test("escape from the log reopens a clean search on the next slash", async () => {
  const view = await openLog();
  const search = searchBox(view);

  fireEvent.keyDown(document.body, { key: "/" });
  fireEvent.change(search, { target: { value: "error" } });
  fireEvent.keyDown(search, { key: "Enter" });
  assert.equal(fireEvent.keyDown(document.body, { key: "Escape" }), false);
  assert.equal(focused(), BODY);

  fireEvent.keyDown(document.body, { key: "/" });
  assert.equal(focused(), SEARCH);
  assert.equal(search.value, "");
  await matchCount(view, "700 lines");
});

test("escape closes a search that was typed into the field with the mouse", async () => {
  const view = await openLog();
  const search = searchBox(view);
  fireEvent.change(search, { target: { value: "error" } });
  view.getByRole("button", { name: "Refresh log" }).focus();
  await matchCount(view, `1 of ${MATCH_LINES.length} matches`);

  assert.equal(fireEvent.keyDown(document.activeElement, { key: "Escape" }), false);
  assert.equal(search.value, "");
  await matchCount(view, "700 lines");
});

test("escape is left to other consumers when no search is running", async () => {
  await openLog();

  assert.equal(fireEvent.keyDown(document.body, { key: "Escape" }), true);
});

test("the search keys are ignored outside the pipelines view", async () => {
  const view = await openLog();
  const outside = document.createElement("button");
  outside.textContent = "Elsewhere";
  document.body.append(outside);
  outside.focus();

  assert.equal(fireEvent.keyDown(outside, { key: "/" }), true);
  assert.equal(focused(), "BUTTON[Elsewhere]");
  assert.equal(searchBox(view).value, "");
  outside.remove();
});

test("the search keys are ignored while a modal dialog owns the keyboard", async () => {
  const controller = new WorkspaceUtilityController();
  const view = await openLog({ controller });
  const search = searchBox(view);

  fireEvent.keyDown(document.body, { key: "/" });
  fireEvent.change(search, { target: { value: "error" } });
  fireEvent.keyDown(search, { key: "Enter" });
  await matchCount(view, `1 of ${MATCH_LINES.length} matches`);

  await act(async () => controller.beginClear());
  assert.ok(view.getByText("Clear shared API cache?"));
  assert.equal(focused(), "BUTTON[Clear cache]");

  // "/" must not pull focus out from behind the dialog.
  assert.equal(fireEvent.keyDown(document.body, { key: "/" }), true);
  assert.equal(focused(), "BUTTON[Clear cache]");

  // n must not walk the log behind the dialog.
  assert.equal(fireEvent.keyDown(document.activeElement, { key: "n" }), true);
  await matchCount(view, `1 of ${MATCH_LINES.length} matches`);

  // Escape belongs to the dialog alone, so the log search survives it.
  fireEvent.keyDown(document.activeElement, { key: "Escape" });
  assert.equal(view.queryByText("Clear shared API cache?"), null);
  assert.equal(search.value, "error");
  await matchCount(view, `1 of ${MATCH_LINES.length} matches`);
});

test("a log refresh ends an open search and restores the row window", async () => {
  const view = await openLog({ varySnapshot: true });
  const search = searchBox(view);
  const nextRows = view.getByRole("button", { name: "Next rows" });
  fireEvent.click(nextRows);
  assert.equal(rowWindow(view), "301-600 of 700");

  fireEvent.keyDown(document.body, { key: "/" });
  fireEvent.change(search, { target: { value: "error" } });
  fireEvent.keyDown(search, { key: "Enter" });
  await matchCount(view, `1 of ${MATCH_LINES.length} matches`);
  assert.equal(rowWindow(view), "1-300 of 700");

  await act(async () => {
    fireEvent.click(view.getByRole("button", { name: "Refresh log" }));
  });
  assert.equal(search.value, "");
  await matchCount(view, "700 lines");
  assert.equal(rowWindow(view), "301-600 of 700");
  assert.equal(view.container.querySelectorAll(".ci-log-match").length, 0);

  // Nothing is left running for Escape to close, and the keys still work.
  assert.equal(fireEvent.keyDown(document.body, { key: "Escape" }), true);
  assert.equal(fireEvent.keyDown(document.body, { key: "/" }), false);
  assert.equal(focused(), SEARCH);
});

test("the search keys are released when the view unmounts", async () => {
  const view = await openLog();
  view.unmount();

  assert.equal(fireEvent.keyDown(document.body, { key: "/" }), true);
  assert.equal(focused(), BODY);
});

async function openLog({ controller = null, varySnapshot = false } = {}) {
  const bridge = logBridge(varySnapshot);
  const context = featureContext(bridge);
  const page = createPipelinesFeature().render(context, reviewRoute());
  const view = render(
    controller === null
      ? page
      : React.createElement(
          React.Fragment,
          null,
          page,
          React.createElement(WorkspaceUtilityOverlay, { controller, context }),
        ),
  );
  await view.findByText("line 0 ok");
  // The document listener is installed by an effect, so let React flush its
  // passive effects before the first keystroke reaches the document.
  await act(async () => {});
  return view;
}

/** Short, stable description of the focused element, so a failure stays readable. */
function focused() {
  const active = document.activeElement;
  if (active === null) return "none";
  if (active.tagName === "BODY") return "BODY";
  const text = active.textContent ?? "";
  return text === ""
    ? `${active.tagName}.${active.className}`
    : `${active.tagName}[${text}]`;
}

function searchBox(view) {
  return view.getByRole("searchbox", { name: "Search log" });
}

// The status text depends on the synchronous `matches` memo and on
// `selectedMatch`, which a query change resets through a separate effect, so
// a query or step event can need more than one commit before the text
// settles. Wait for the expected string rather than reading it right away.
async function matchCount(view, expected) {
  await waitFor(() => {
    assert.equal(
      view.container.querySelector(".ci-match-count").textContent,
      expected,
    );
  });
  return expected;
}

function rowWindow(view) {
  return view.container.querySelector(".ci-log-windows span").textContent;
}

function selectedLine(view) {
  // Reading the whole set also asserts that exactly one row is selected, and it
  // avoids a jsdom querySelector result that kept naming a row whose class a
  // re-render had already moved.
  const selected = [...view.container.querySelectorAll(".ci-log-match-selected")];
  assert.equal(selected.length, 1);
  return Number(selected[0].dataset.line);
}

function logBridge(varySnapshot) {
  let opens = 0;
  return {
    cancelRead: async () => true,
    onEvent: () => () => {},
    listReviewPipelines: () => read({ pipelines: [pipeline()] }),
    listJobs: () => read({ jobs: [job()] }),
    openLog: ({ job: handle }) => {
      opens += 1;
      return read(logPage(handle, varySnapshot ? opens : 1));
    },
    pageLog: () => {
      throw new Error("no next log page");
    },
    getCICapabilities: () =>
      read({
        repository: "repo",
        capabilities: {
          retry_pipeline: false,
          cancel_pipeline: false,
          retry_job: false,
          cancel_job: false,
        },
      }),
    getCIReceipt: () => read({ receipt: null }),
    openExternal: async () => true,
    openJobLogInEditor: async () => ({
      outcome: "started",
      message: "Editor started.",
    }),
    clearCache: async () => ({
      outcome: "cleared",
      message: "Shared API cache cleared.",
    }),
  };
}

function featureContext(bridge) {
  return {
    bridge,
    queries: new QueryCoordinator(bridge),
    repositories: [],
    repositoriesReady: true,
    repositoryGeneration: 1,
    reviewPanels: [{ id: "pipelines", label: "Pipelines", order: 40 }],
    inlineAnchor: null,
    selectInlineAnchor: () => {},
    navigate: () => {},
  };
}

function reviewRoute() {
  return {
    kind: "review",
    panel: "pipelines",
    item: {
      handle: "review-47",
      repository: "repo",
      summary: {
        number: 47,
        title: "Desktop pipelines",
        author: { username: "andre", display_name: "Andre" },
        state: "open",
        is_draft: false,
        source_branch: "feature",
        target_branch: "feat/desktop-app",
        ci_status: "running",
        created_at: "2026-09-08T00:00:00Z",
        updated_at: "2026-09-08T00:00:00Z",
        web_url: "https://example.invalid/review/47",
        labels: [],
        review_decision: null,
        additions: 1,
        deletions: 1,
      },
    },
  };
}

function pipeline() {
  return {
    handle: "pipeline-101",
    value: {
      id: 101,
      status: "running",
      ref: "branch-101",
      sha: "1".repeat(40),
      web_url: "https://example.invalid/pipeline/101",
      source: "push",
      created_at: "2026-09-08T00:00:00Z",
      finished_at: null,
      duration_seconds: null,
    },
  };
}

function job() {
  return {
    handle: "job-201",
    value: {
      id: 201,
      name: "build-201",
      stage: "test",
      status: "running",
      web_url: "https://example.invalid/job/201",
      started_at: "2026-09-08T00:00:00Z",
      finished_at: null,
      duration_seconds: null,
      allow_failure: false,
    },
  };
}

function logPage(resource, generation) {
  return {
    snapshot_id: `snapshot-${resource}-${generation}`,
    resource,
    revision: { sha256: "hash", byte_count: 99 },
    cursor: 0,
    next_cursor: null,
    entries: [{ text: LOG_TEXT }],
  };
}

function read(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}
