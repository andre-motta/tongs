// Regression coverage for "/" search in the desktop pipelines, jobs and logs
// view. Issue #177: the view advertised "Press / to search", but the handler was
// bound to a React onKeyDown on the non-focusable ".ci-page" section, so a
// keystroke typed with nothing focused was delivered to the document body and
// never reached it. Desktop sibling of the TUI defect #168.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import { createPipelinesFeature } from "../../../desktop/dist/src/renderer/features/pipelines/index.js";

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
  HTMLInputElement: dom.window.HTMLInputElement,
  Node: dom.window.Node,
});
const { act, cleanup, fireEvent, render } = desktopRequire(
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

test("slash focuses the log search when nothing in the view holds focus", async () => {
  const view = await openLog();
  const search = searchBox(view);

  assert.equal(document.activeElement, document.body);
  assert.equal(fireEvent.keyDown(document.body, { key: "/" }), false);
  assert.equal(document.activeElement, search);
});

test("slash reaches the search from a focused control inside the view", async () => {
  const view = await openLog();
  const search = searchBox(view);
  const refresh = view.getByRole("button", { name: "Refresh log" });
  refresh.focus();

  fireEvent.keyDown(refresh, { key: "/" });
  assert.equal(document.activeElement, search);
});

test("slash is left to a focused text field instead of stealing the keystroke", async () => {
  const view = await openLog();
  const search = searchBox(view);
  search.focus();

  assert.equal(fireEvent.keyDown(search, { key: "/" }), true);
  assert.equal(document.activeElement, search);
});

test("slash with a modifier is left to the platform", async () => {
  const view = await openLog();

  assert.equal(
    fireEvent.keyDown(document.body, { key: "/", ctrlKey: true }),
    true,
  );
  assert.equal(document.activeElement, document.body);
  assert.equal(searchBox(view).value, "");
});

test("typing filters live and n and N step matches with a wrapping counter", async () => {
  const view = await openLog();
  const search = searchBox(view);

  fireEvent.keyDown(document.body, { key: "/" });
  fireEvent.change(search, { target: { value: "error" } });
  assert.equal(matchCount(view), `1 of ${MATCH_LINES.length} matches`);
  assert.equal(selectedLine(view), MATCH_LINES[0] + 1);

  // n and N belong to the log, so Enter hands the keyboard back first.
  assert.equal(fireEvent.keyDown(search, { key: "n" }), true);
  assert.equal(matchCount(view), `1 of ${MATCH_LINES.length} matches`);

  fireEvent.keyDown(search, { key: "Enter" });
  assert.equal(document.activeElement, document.body);

  assert.equal(fireEvent.keyDown(document.body, { key: "n" }), false);
  assert.equal(matchCount(view), `2 of ${MATCH_LINES.length} matches`);
  assert.equal(selectedLine(view), MATCH_LINES[1] + 1);

  fireEvent.keyDown(document.body, { key: "n" });
  assert.equal(matchCount(view), `3 of ${MATCH_LINES.length} matches`);
  assert.equal(rowWindow(view), "301-600 of 700");

  fireEvent.keyDown(document.body, { key: "N" });
  assert.equal(matchCount(view), `2 of ${MATCH_LINES.length} matches`);

  fireEvent.keyDown(document.body, { key: "N" });
  fireEvent.keyDown(document.body, { key: "N" });
  assert.equal(
    matchCount(view),
    `${MATCH_LINES.length} of ${MATCH_LINES.length} matches`,
  );
  assert.equal(rowWindow(view), "601-700 of 700");

  fireEvent.keyDown(document.body, { key: "n" });
  assert.equal(matchCount(view), `1 of ${MATCH_LINES.length} matches`);
});

test("n does nothing without matches", async () => {
  const view = await openLog();
  const search = searchBox(view);

  fireEvent.keyDown(document.body, { key: "/" });
  fireEvent.change(search, { target: { value: "no such text" } });
  fireEvent.keyDown(search, { key: "Enter" });
  assert.equal(matchCount(view), "No matches");

  assert.equal(fireEvent.keyDown(document.body, { key: "n" }), true);
  assert.equal(matchCount(view), "No matches");
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
  assert.equal(matchCount(view), `1 of ${MATCH_LINES.length} matches`);

  assert.equal(fireEvent.keyDown(search, { key: "Escape" }), false);
  assert.equal(search.value, "");
  assert.equal(matchCount(view), "700 lines");
  assert.equal(rowWindow(view), "301-600 of 700");
  assert.equal(document.activeElement, nextRows);
  assert.equal(view.container.querySelectorAll(".ci-log-match").length, 0);
});

test("escape from the log reopens a clean search on the next slash", async () => {
  const view = await openLog();
  const search = searchBox(view);

  fireEvent.keyDown(document.body, { key: "/" });
  fireEvent.change(search, { target: { value: "error" } });
  fireEvent.keyDown(search, { key: "Enter" });
  assert.equal(fireEvent.keyDown(document.body, { key: "Escape" }), false);
  assert.equal(document.activeElement, document.body);

  fireEvent.keyDown(document.body, { key: "/" });
  assert.equal(document.activeElement, search);
  assert.equal(search.value, "");
  assert.equal(matchCount(view), "700 lines");
});

test("escape is left to other consumers when the search was never opened", async () => {
  await openLog();

  assert.equal(fireEvent.keyDown(document.body, { key: "Escape" }), true);
});

test("the log search keys are released when the view unmounts", async () => {
  const view = await openLog();
  view.unmount();

  assert.equal(fireEvent.keyDown(document.body, { key: "/" }), true);
  assert.equal(document.activeElement, document.body);
});

async function openLog() {
  const view = render(
    createPipelinesFeature().render(featureContext(logBridge()), reviewRoute()),
  );
  await view.findByText("line 0 ok");
  // The document listener is installed by an effect, so let React flush its
  // passive effects before the first keystroke reaches the document.
  await act(async () => {});
  return view;
}

function searchBox(view) {
  return view.getByRole("searchbox", { name: "Search log" });
}

function matchCount(view) {
  return view.container.querySelector(".ci-match-count").textContent;
}

function rowWindow(view) {
  return view.container.querySelector(".ci-log-windows span").textContent;
}

function selectedLine(view) {
  // jsdom's querySelector can return a stale match for a class that a re-render
  // moved, so read the whole set.
  const selected = [...view.container.querySelectorAll(".ci-log-match-selected")];
  assert.equal(selected.length, 1);
  return Number(selected[0].dataset.line);
}

function logBridge() {
  return {
    cancelRead: async () => true,
    onEvent: () => () => {},
    listReviewPipelines: () => read({ pipelines: [pipeline()] }),
    listJobs: () => read({ jobs: [job()] }),
    openLog: ({ job: handle }) => read(logPage(handle, LOG_TEXT)),
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

function logPage(resource, text) {
  return {
    snapshot_id: `snapshot-${resource}`,
    resource,
    revision: { sha256: "hash", byte_count: 99 },
    cursor: 0,
    next_cursor: null,
    entries: [{ text }],
  };
}

function read(value) {
  return { requestToken: crypto.randomUUID(), result: Promise.resolve(value) };
}
