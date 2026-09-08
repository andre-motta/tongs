import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import {
  createInboxFeature,
  listDiscoveredReviews,
  sortReviewItems,
} from "../../../desktop/dist/src/renderer/features/inbox/index.js";
import {
  filterAndSortRepositories,
  RepositoryNavigation,
} from "../../../desktop/dist/src/renderer/features/repositories/index.js";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const { JSDOM } = desktopRequire("jsdom");
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "tongs://app/index.html",
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

test("review controls keep All Open as the default and send exact scope and state", async () => {
  const calls = [];
  const pendingMyReviews = deferred();
  const bridge = baseBridge({
    listReviews: (params) => {
      calls.push(params);
      if (params.scope === "my_reviews")
        return read(pendingMyReviews.promise);
      return read({
        items: [reviewItem(`${params.scope}-${params.state}`, params.state)],
        failures: [],
      });
    },
  });
  const feature = createInboxFeature();
  const view = render(
    feature.render(featureContext(bridge), {
      kind: "inbox",
      repository: repository("repo", "Repository"),
    }),
  );

  await view.findByText("all_open-open");
  assert.deepEqual(calls, [
    { scope: "all_open", state: "open", repository: "repo" },
  ]);
  assert.equal(
    view.getByRole("button", { name: "All Open" }).getAttribute("aria-pressed"),
    "true",
  );

  fireEvent.click(view.getByRole("button", { name: "My Reviews" }));
  assert.equal(view.queryByText("all_open-open"), null);
  assert.ok(view.getByText("Loading reviews from the local service…"));
  assert.equal(
    view.getByRole("button", { name: "Closed & merged" }).disabled,
    true,
  );
  assert.deepEqual(calls.at(-1), {
    scope: "my_reviews",
    state: "open",
    repository: "repo",
  });
  pendingMyReviews.resolve({
    items: [reviewItem("my_reviews-open", "open")],
    failures: [],
  });
  await view.findByText("my_reviews-open");

  fireEvent.click(view.getByRole("button", { name: "My MRs" }));
  await view.findByText("my_mrs-open");
  assert.deepEqual(calls.at(-1), {
    scope: "my_mrs",
    state: "open",
    repository: "repo",
  });

  fireEvent.click(view.getByRole("button", { name: "All Open" }));
  fireEvent.click(view.getByRole("button", { name: "Closed & merged" }));
  await view.findByText("all_open-closed");
  assert.deepEqual(calls.at(-1), {
    scope: "all_open",
    state: "closed",
    repository: "repo",
  });
});

test("All reviews fans the selected scope out only to discovered handles", async () => {
  const calls = [];
  const bridge = baseBridge({
    listReviews: (params) => {
      calls.push(params);
      return read({ items: [], failures: [] });
    },
  });
  const combined = listDiscoveredReviews(
    bridge,
    [{ handle: "github-local" }, { handle: "gitlab-local" }],
    "my_reviews",
    "open",
  );
  await combined.result;
  assert.deepEqual(calls, [
    { scope: "my_reviews", state: "open", repository: "github-local" },
    { scope: "my_reviews", state: "open", repository: "gitlab-local" },
  ]);
});

test("scope-specific empty and retained refresh failure states stay explicit", async () => {
  let personalReads = 0;
  const bridge = baseBridge({
    listReviews: (params) => {
      if (params.scope === "all_open")
        return read({ items: [], failures: [] });
      personalReads += 1;
      return read(
        personalReads === 1
          ? {
              items: [reviewItem("Assigned review", "open")],
              failures: [],
            }
          : Promise.reject(new Error("controlled refresh failure")),
      );
    },
  });
  const feature = createInboxFeature();
  const view = render(
    feature.render(featureContext(bridge), {
      kind: "inbox",
      repository: repository("repo", "Repository"),
    }),
  );
  await view.findByText("No open reviews match this repository scope.");
  fireEvent.click(view.getByRole("button", { name: "My Reviews" }));
  await view.findByText("Assigned review");
  fireEvent.click(view.getByRole("button", { name: "Refresh reviews" }));
  await view.findByText("Refresh failed. Showing the previous review list.");
  assert.ok(view.getByText("Assigned review"));
});

test("review sort order is deterministic and focus follows a stable review", async () => {
  const newest = reviewItem("Zulu", "open", {
    handle: "review-new",
    updated_at: "2026-09-08T12:00:00Z",
    ci_status: "success",
    author: "zoe",
  });
  const failed = reviewItem("Alpha", "open", {
    handle: "review-failed",
    updated_at: "2026-09-08T11:00:00Z",
    ci_status: "failed",
    author: "amy",
  });
  const pending = reviewItem("Beta", "open", {
    handle: "review-pending",
    updated_at: "2026-09-08T10:00:00Z",
    ci_status: "pending",
    author: "bob",
  });
  assert.deepEqual(
    sortReviewItems([pending, newest, failed], "updated").map(
      (item) => item.handle,
    ),
    ["review-new", "review-failed", "review-pending"],
  );
  assert.deepEqual(
    sortReviewItems([pending, newest, failed], "title").map(
      (item) => item.handle,
    ),
    ["review-failed", "review-pending", "review-new"],
  );
  assert.deepEqual(
    sortReviewItems([pending, newest, failed], "ci").map(
      (item) => item.handle,
    ),
    ["review-failed", "review-pending", "review-new"],
  );
  assert.deepEqual(
    sortReviewItems([pending, newest, failed], "author").map(
      (item) => item.handle,
    ),
    ["review-failed", "review-pending", "review-new"],
  );

  const bridge = baseBridge({
    listReviews: () =>
      read({ items: [newest, failed, pending], failures: [] }),
  });
  const feature = createInboxFeature();
  const view = render(
    feature.render(featureContext(bridge), {
      kind: "inbox",
      repository: repository("repo", "Repository"),
    }),
  );
  await view.findByText("Zulu");
  const stable = view.getByText("Zulu").closest("button");
  stable.focus();
  fireEvent.change(view.getByLabelText("Sort reviews"), {
    target: { value: "title" },
  });
  await waitFor(() =>
    assert.equal(
      view.container.querySelector(".review-card .review-title")?.textContent,
      "Alpha",
    ),
  );
  assert.equal(document.activeElement, stable);
  fireEvent.keyDown(stable, { key: "Home" });
  assert.equal(
    document.activeElement.querySelector(".review-title")?.textContent,
    "Alpha",
  );
  fireEvent.keyDown(document.activeElement, { key: "End" });
  assert.equal(
    document.activeElement.querySelector(".review-title")?.textContent,
    "Zulu",
  );
});

test("repository controls search display names and sort legacy hosts last", () => {
  const repositories = [
    repository("legacy", "Beta", "gitlab"),
    repository("github", "gamma", "github", "z.example"),
    repository("gitlab", "Alpha", "gitlab", "a.example"),
  ];
  assert.deepEqual(
    filterAndSortRepositories(repositories, " ALP ", "all", "name").map(
      (item) => item.handle,
    ),
    ["gitlab"],
  );
  assert.deepEqual(
    filterAndSortRepositories(repositories, "", "github", "forge").map(
      (item) => item.handle,
    ),
    ["github"],
  );
  assert.deepEqual(
    filterAndSortRepositories(repositories, "", "all", "host").map(
      (item) => item.handle,
    ),
    ["gitlab", "github", "legacy"],
  );
});

test("mounted repository controls retain filters and keyboard focus across discovery", async () => {
  const discoveries = [
    [
      repository("legacy", "Beta", "gitlab"),
      repository("github", "Gamma", "github", "z.example"),
      repository("gitlab", "Alpha", "gitlab", "a.example"),
    ],
    [
      repository("gitlab", "Alpha renamed", "gitlab", "a.example"),
      repository("github", "Gamma", "github", "z.example"),
    ],
  ];
  const observed = [];
  const bridge = baseBridge({
    discoverRepositories: () =>
      read({ repositories: discoveries.shift() ?? [] }),
  });
  const view = render(
    React.createElement(RepositoryNavigation, {
      bridge,
      queries: new QueryCoordinator(bridge),
      selected: null,
      navigate: () => {},
      onDiscovery: (repositories) => observed.push(repositories),
    }),
  );
  await view.findByText("Gamma");
  assert.equal(
    view.getByText("Showing 3 of 3 repositories").textContent,
    "Showing 3 of 3 repositories",
  );

  fireEvent.change(view.getByLabelText("Sort repositories"), {
    target: { value: "host" },
  });
  await waitFor(() =>
    assert.deepEqual(repositoryNames(view), ["Alpha", "Gamma", "Beta"]),
  );
  const gammaButton = view.getByText("Gamma").closest("button");
  gammaButton.focus();
  fireEvent.change(view.getByLabelText("Sort repositories"), {
    target: { value: "name" },
  });
  assert.equal(document.activeElement, gammaButton);

  const search = view.getByLabelText("Search repositories");
  fireEvent.change(search, { target: { value: "ga" } });
  await waitFor(() => assert.deepEqual(repositoryNames(view), ["Gamma"]));
  fireEvent.keyDown(search, { key: "Enter" });
  assert.equal(document.activeElement, gammaButton);
  fireEvent.change(view.getByLabelText("Forge"), {
    target: { value: "gitlab" },
  });
  assert.ok(
    view.getByText(
      "No local repositories match the current search and forge filter.",
    ),
  );

  fireEvent.change(search, { target: { value: "alpha" } });
  await waitFor(() => assert.deepEqual(repositoryNames(view), ["Alpha"]));
  fireEvent.click(view.getByRole("button", { name: "Refresh local repositories" }));
  await view.findByText("Alpha renamed");
  assert.equal(view.getByLabelText("Search repositories").value, "alpha");
  assert.equal(view.getByLabelText("Forge").value, "gitlab");
  assert.equal(view.getByLabelText("Sort repositories").value, "name");
  assert.deepEqual(repositoryNames(view), ["Alpha renamed"]);
  await waitFor(() =>
    assert.equal(observed.at(-1)?.[0]?.display_name, "Alpha renamed"),
  );
});

function repository(handle, displayName, forge = "github", hostname) {
  return {
    handle,
    display_name: displayName,
    forge_type: forge,
    ...(hostname ? { hostname } : {}),
  };
}

function reviewItem(title, state, overrides = {}) {
  return {
    handle: overrides.handle ?? `review-${title}`,
    repository: "repo",
    summary: {
      number: 98,
      title,
      author: {
        username: overrides.author ?? "author",
        display_name: overrides.author ?? "Author",
      },
      state,
      is_draft: false,
      source_branch: "feature",
      target_branch: "main",
      ci_status: overrides.ci_status ?? "success",
      created_at: "2026-09-08T00:00:00Z",
      updated_at: overrides.updated_at ?? "2026-09-08T00:00:00Z",
      web_url: "https://example.invalid/review/98",
      comment_count: 0,
      has_conflicts: false,
      labels: [],
      review_decision: null,
      additions: 1,
      deletions: 0,
    },
  };
}

function featureContext(bridge) {
  return {
    bridge,
    queries: new QueryCoordinator(bridge),
    repositories: [repository("repo", "Repository")],
    repositoriesReady: true,
    repositoryGeneration: 1,
    reviewPanels: [],
    inlineAnchor: null,
    selectInlineAnchor: () => {},
    navigate: () => {},
  };
}

function baseBridge(overrides) {
  return {
    cancelRead: async () => true,
    ...overrides,
  };
}

function read(value) {
  return {
    requestToken: crypto.randomUUID(),
    result: Promise.resolve(value),
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((accept) => {
    resolve = accept;
  });
  return { promise, resolve };
}

function repositoryNames(view) {
  return [...view.container.querySelectorAll("button.repository-choice > span")].map(
    (node) => node.textContent,
  );
}
