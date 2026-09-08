import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { after } from "node:test";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const { JSDOM } = desktopRequire("jsdom");
const dom = new JSDOM(
  "<!doctype html><html><head></head><body><div id='app'></div></body></html>",
  { url: "tongs://app/index.html" },
);
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
  DOMException: dom.window.DOMException,
  requestAnimationFrame: (callback) => setTimeout(callback, 0),
});
dom.window.requestAnimationFrame = globalThis.requestAnimationFrame;
dom.window.HTMLCanvasElement.prototype.getContext = () => null;
const { fireEvent, queryByLabelText, waitFor } = desktopRequire(
  "@testing-library/react",
);

after(() => dom.window.close());

test("mounted app reconciles successful discovery without losing review work", async () => {
  const fixture = bridgeFixture();
  Object.defineProperty(window, "tongs", { value: fixture.bridge });
  await import("../../../desktop/dist/src/renderer/app.js");

  await waitFor(() => assert.ok(button("Repo A")));
  fireEvent.click(button("Repo A"));
  await waitFor(() => assert.equal(title(), "Repo A"));

  fixture.discoveries.push([repositoryA(), repositoryB()]);
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() =>
    assert.equal(button("Refresh local repositories").disabled, false),
  );
  assert.equal(title(), "Repo A");
  assert.ok(routeNotice() === null);

  fixture.discoveries.push(new Error("controlled discovery failure"));
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() => assert.ok(button("Retry")));
  assert.equal(title(), "Repo A");
  assert.ok(routeNotice() === null);

  fixture.discoveries.push([repositoryB(), repositoryA("Repo A renamed")]);
  fireEvent.click(button("Retry"));
  await waitFor(() => assert.equal(title(), "Repo A renamed"));
  assert.ok(routeNotice() === null);

  fireEvent.click(await awaitReview("Persistent review"));
  await waitFor(() => assert.ok(button("Discussions")));
  fireEvent.click(button("Discussions"));
  await waitFor(() => assert.ok(labelled("Quick comment")));
  fireEvent.click(button("Start review"));
  await waitFor(() => assert.ok(labelled("Review body")));
  setValue(labelled("Review body"), "unsaved local draft body");
  setValue(labelled("Add general draft comment"), "unsent general composer");

  fireEvent.click(button("Files changed"));
  await waitFor(() =>
    assert.equal(
      Boolean(
        document.querySelector(
          'code.line-content[role="button"][aria-label="Select new line 1"]',
        ),
      ),
      true,
    ),
  );
  fireEvent.click(
    document.querySelector(
      'code.line-content[role="button"][aria-label="Select new line 1"]',
    ),
  );
  fireEvent.click(button("Discussions"));
  await waitFor(() => assert.ok(labelled("Add selected line to draft")));
  setValue(labelled("Add selected line to draft"), "unsent anchored composer");
  const suggestionExplanation = "explanation for the original anchor";
  const suggestionReplacement = "replacement for the original anchor";
  await waitFor(() =>
    assert.equal(Boolean(labelled("Suggestion explanation")), true),
  );
  setValue(labelled("Suggestion explanation"), suggestionExplanation);
  setValue(labelled("Suggestion replacement code"), suggestionReplacement);
  assert.equal(
    labelled("Suggestion explanation").value,
    suggestionExplanation,
  );
  assert.equal(
    labelled("Suggestion replacement code").value,
    suggestionReplacement,
  );

  const writesBeforeRemoval = fixture.writes.length;
  const readsBeforeRemoval = fixture.reviewReads.length;
  fixture.discoveries.push([repositoryB()]);
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() => assert.equal(title(), "All reviews"));
  assert.match(
    routeNotice()?.textContent ?? "",
    /no longer in the local workspace/,
  );
  await waitFor(() =>
    assert.equal(
      fixture.reviewReads
        .slice(readsBeforeRemoval)
        .some((params) => params.repository === "repo-a"),
      false,
    ),
  );
  assert.equal(fixture.writes.length, writesBeforeRemoval);
  assert.equal(Boolean(labelled("Suggestion explanation")), false);
  assert.equal(Boolean(labelled("Suggestion replacement code")), false);
  assert.equal(document.body.textContent.includes(suggestionExplanation), false);
  assert.equal(document.body.textContent.includes(suggestionReplacement), false);

  fixture.discoveries.push([repositoryA("Repo A restored"), repositoryB()]);
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() => assert.ok(routeNotice() === null));
  fireEvent.click(button("Repo A restored"));
  fireEvent.click(await awaitReview("Persistent review"));
  fireEvent.click(await awaitButton("Discussions"));
  await waitFor(() =>
    assert.equal(labelled("Review body")?.value, "unsaved local draft body"),
  );
  assert.equal(
    labelled("Add general draft comment").value,
    "unsent general composer",
  );
  assert.equal(labelled("Add selected line to draft").value, "");
  assert.ok(document.body.textContent.includes("unsent anchored composer"));
  assert.ok(document.body.textContent.includes("Draft review active"));
  assert.equal(fixture.writes.length, writesBeforeRemoval);

  fireEvent.click(button("Files changed"));
  await waitFor(() =>
    assert.equal(
      Boolean(
        document.querySelector(
          'code.line-content[role="button"][aria-label="Select new line 2"]',
        ),
      ),
      true,
    ),
  );
  fireEvent.click(
    document.querySelector(
      'code.line-content[role="button"][aria-label="Select new line 2"]',
    ),
  );
  fireEvent.click(button("Discussions"));
  await waitFor(() =>
    assert.equal(Boolean(labelled("Suggestion explanation")), true),
  );
  assert.equal(labelled("Suggestion explanation").value, "");
  assert.equal(
    labelled("Suggestion replacement code").value,
    "alternate selected line",
  );
  assert.equal(document.body.textContent.includes(suggestionExplanation), false);
  assert.equal(document.body.textContent.includes(suggestionReplacement), false);

  fireEvent.click(button("Files changed"));
  await waitFor(() =>
    assert.equal(
      Boolean(
        document.querySelector(
          'code.line-content[role="button"][aria-label="Select new line 1"]',
        ),
      ),
      true,
    ),
  );
  fireEvent.click(
    document.querySelector(
      'code.line-content[role="button"][aria-label="Select new line 1"]',
    ),
  );
  fireEvent.click(button("Discussions"));
  await waitFor(() =>
    assert.equal(
      labelled("Suggestion explanation")?.value,
      suggestionExplanation,
    ),
  );
  assert.equal(
    labelled("Suggestion replacement code")?.value,
    suggestionReplacement,
  );

  fireEvent.click(button("← Reviews"));
  fireEvent.click(await awaitButton("Repo A restored"));
  await waitFor(() => assert.equal(title(), "Repo A restored"));
  fixture.discoveries.push([]);
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() => assert.equal(title(), "All reviews"));
  assert.match(routeNotice()?.textContent ?? "", /Showing All reviews/);
  assert.equal(fixture.writes.length, writesBeforeRemoval);

  fireEvent.click(await awaitButton("Plugin dashboard"));
  await waitFor(() => assert.equal(title(), "Plugin dashboard"));
  fixture.discoveries.push([repositoryB(), repositoryA("Repo A current")]);
  fireEvent.click(button("Refresh local repositories"));
  await waitFor(() =>
    assert.equal(button("Refresh local repositories").disabled, false),
  );
  assert.equal(title(), "Plugin dashboard");
  assert.ok(routeNotice() === null);
});

function bridgeFixture() {
  let sequence = 0;
  let activeDraft = null;
  const discoveries = [[repositoryA(), repositoryB()]];
  const reviewReads = [];
  const writes = [];
  const read = (value) => ({
    requestToken: `request-${++sequence}`,
    result:
      value instanceof Error ? Promise.reject(value) : Promise.resolve(value),
  });
  const bridge = {
    discoverRepositories: () => {
      const next = discoveries.shift() ?? [];
      return read(next instanceof Error ? next : { repositories: next });
    },
    listReviews: (params) => {
      reviewReads.push(params);
      return read({
        items: params.repository === "repo-a" ? [reviewItem()] : [],
        failures: [],
      });
    },
    getReview: () => read(reviewSnapshot()),
    listDiscussions: () => read({ discussions: [] }),
    getReviewMutationCapabilities: () =>
      read({ review: "review-a", capabilities: mutationCapabilities() }),
    getReviewActionCapabilities: () =>
      read({
        review: "review-a",
        capabilities: { merge: false, close: false, reopen: false, unapprove: false },
      }),
    listReviewDrafts: () =>
      read({
        cursor: 0,
        next_cursor: null,
        drafts: activeDraft ? [activeDraft] : [],
      }),
    listReviewSubmissions: () =>
      read({ cursor: 0, next_cursor: null, attempts: [] }),
    createReviewDraft: async () => {
      writes.push("create-draft");
      activeDraft = draft();
      return activeDraft;
    },
    openDiff: ({ layout }) => read(diffPage(layout)),
    pageDiff: () => {
      throw new Error("No next page");
    },
    listPlugins: () => read({ plugins: [plugin()] }),
    listAssets: () => read([]),
    setLocation: async () => ({ accepted: true }),
    cancelRead: async () => true,
    onEvent: () => () => {},
    openExternal: async () => true,
  };
  return { bridge, discoveries, reviewReads, writes };
}

function repositoryA(displayName = "Repo A") {
  return { handle: "repo-a", display_name: displayName, forge_type: "github" };
}

function repositoryB() {
  return { handle: "repo-b", display_name: "Repo B", forge_type: "gitlab" };
}

function plugin() {
  return {
    plugin_id: "fixture-plugin",
    state: "started",
    has_terminal_entry_point: true,
    has_desktop_entry_point: true,
    error: null,
    manifest: {
      title: "Fixture plugin",
      version: "1.0.0",
      api_major: 1,
      modules: [
        {
          id: "dashboard",
          title: "Plugin dashboard",
          entry_asset: "fixture-module",
          stylesheets: ["fixture-style"],
        },
      ],
      navigation: [
        {
          id: "dashboard",
          title: "Plugin dashboard",
          module_id: "dashboard",
        },
      ],
      commands: [],
      methods: [],
      events: [],
      focus_targets: [],
      help_asset: null,
      reads: [],
      assets_available: true,
    },
  };
}

function reviewItem() {
  return {
    handle: "review-a",
    repository: "repo-a",
    summary: {
      number: 95,
      title: "Persistent review",
      author: { username: "author", display_name: "Author" },
      state: "open",
      is_draft: false,
      source_branch: "feature",
      target_branch: "main",
      ci_status: "success",
      created_at: "2026-09-08T00:00:00Z",
      updated_at: "2026-09-08T00:00:00Z",
      web_url: "https://example.invalid/review/95",
      comment_count: 0,
      has_conflicts: false,
      labels: [],
      review_decision: null,
      additions: 1,
      deletions: 0,
    },
  };
}

function reviewSnapshot() {
  return {
    handle: "review-a",
    repository: "repo-a",
    detail: { ...reviewItem().summary, description: "", merge_status: "can_be_merged" },
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

function draft() {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    review: "review-a",
    revision: { head_sha: "head", base_sha: "base", start_sha: null },
    version: 1,
    body: "",
    verdict: null,
    comments: [],
    state: "editable",
    created_at: "2026-09-08T00:00:00+00:00",
    updated_at: "2026-09-08T00:00:00+00:00",
  };
}

function diffPage(layout) {
  return {
    snapshot_id: `snapshot-${layout}`,
    resource: "review-a",
    revision: { head_sha: "head", base_sha: "base", start_sha: null },
    cursor: 0,
    next_cursor: null,
    entries: [
      {
        kind: "file",
        file_index: 0,
        old_path: "src/example.py",
        new_path: "src/example.py",
        status: "modified",
        additions: 1,
        deletions: 0,
        is_binary: false,
        language: "python",
        is_truncated: false,
        is_empty: false,
        is_mode_only: false,
        is_unavailable: false,
      },
      {
        kind: "hunk",
        file_index: 0,
        hunk_index: 0,
        header: "@@ -1,2 +1,2 @@",
        old_start: 1,
        old_count: 2,
        new_start: 1,
        new_count: 2,
        context_text: "",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: 1,
        new_line: 1,
        content: "selected line",
        line_type: "context",
      },
      {
        kind: "line",
        file_index: 0,
        hunk_index: 0,
        old_line: 2,
        new_line: 2,
        content: "alternate selected line",
        line_type: "context",
      },
    ],
  };
}

function button(name) {
  return [...document.querySelectorAll("button")].find(
    (item) => item.textContent.trim() === name,
  );
}

async function awaitButton(name) {
  await waitFor(() => assert.ok(button(name)));
  return button(name);
}

async function awaitReview(name) {
  await waitFor(() => assert.ok(reviewButton(name)));
  return reviewButton(name);
}

function reviewButton(name) {
  return [...document.querySelectorAll(".review-card")].find((item) =>
    item.querySelector(".review-title")?.textContent.trim() === name,
  );
}

function labelled(name) {
  return queryByLabelText(document.body, name) ?? undefined;
}

function setValue(element, value) {
  fireEvent.change(element, { target: { value } });
}

function title() {
  return document.querySelector(".view-title")?.textContent ?? null;
}

function routeNotice() {
  return [...document.querySelectorAll("main > .notice")].find((item) =>
    item.textContent.includes("selected repository"),
  ) ?? null;
}
