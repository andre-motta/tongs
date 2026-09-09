import assert from "node:assert/strict";
import test from "node:test";
import { parseInertMarkdown } from "../../../desktop/dist/src/renderer/core/markdown.js";
import {
  FeatureRegistry,
  Navigator,
  reconcileDiscoveryRoute,
} from "../../../desktop/dist/src/renderer/core/navigation.js";
import {
  QueryCoordinator,
  StaleQueryError,
} from "../../../desktop/dist/src/renderer/core/query.js";
import {
  inboxPresentation,
  listDiscoveredReviews,
} from "../../../desktop/dist/src/renderer/features/inbox/index.js";
import {
  RendererReadError,
  safeError,
} from "../../../desktop/dist/src/renderer/core/presentation.js";
import { encodeReadFailure } from "../../../desktop/dist/src/shared/bridge.js";

test("markdown remains inert data without HTML or remote resource parsing", () => {
  const blocks = parseInertMarkdown(
    "# Review\n\n<img src=https://attacker.invalid/a>\n\n- safe\n\n```js\nfetch('https://attacker.invalid')\n```",
  );
  assert.deepEqual(blocks, [
    { kind: "heading", level: 1, text: "Review" },
    { kind: "paragraph", text: "<img src=https://attacker.invalid/a>" },
    { kind: "list", text: "safe" },
    { kind: "code", text: "fetch('https://attacker.invalid')" },
  ]);
});

test("feature registry rejects duplicate seams and selects by stable order", () => {
  const registry = new FeatureRegistry();
  const fallback = {
    id: "fallback",
    order: 20,
    matches: () => true,
    render: () => {},
  };
  const inbox = {
    id: "inbox",
    order: 10,
    matches: (route) => route.kind === "inbox",
    render: () => {},
  };
  registry.register(fallback);
  registry.register(inbox);
  assert.equal(registry.find({ kind: "inbox", repository: null }).id, "inbox");
  assert.throws(() => registry.register(inbox), /Duplicate feature/);
});

test("feature registry orders typed panels and route-aware commands", () => {
  const registry = new FeatureRegistry();
  const command = (id, order, visible = true) => ({
    id,
    label: id,
    order,
    isVisible: () => visible,
    disabledReason: () => (id === "later" ? "Requires a draft" : null),
    run: () => {},
  });
  registry.register({
    id: "diff",
    order: 20,
    reviewPanel: { id: "diff", label: "Files changed", order: 20 },
    commands: [
      command("later", 20),
      command("first", 10),
      command("hidden", 1, false),
    ],
    matches: () => true,
    render: () => {},
  });
  registry.register({
    id: "overview",
    order: 10,
    reviewPanel: { id: "overview", label: "Overview", order: 10 },
    matches: () => true,
    render: () => {},
  });
  const route = { kind: "inbox", repository: null };
  const context = featureContext();
  assert.deepEqual(
    registry.reviewPanels().map((panel) => panel.id),
    ["overview", "diff"],
  );
  assert.deepEqual(
    registry
      .commands(context, route)
      .map((item) => [item.id, item.disabledReason(context, route)]),
    [
      ["first", null],
      ["later", "Requires a draft"],
    ],
  );
  assert.throws(
    () =>
      registry.register({
        id: "duplicate-panel",
        order: 30,
        reviewPanel: { id: "diff", label: "Duplicate", order: 30 },
        matches: () => false,
        render: () => {},
      }),
    /Duplicate review panel/,
  );
  assert.throws(
    () =>
      registry.register({
        id: "duplicate-command",
        order: 30,
        commands: [command("first", 30)],
        matches: () => false,
        render: () => {},
      }),
    /Duplicate command/,
  );
});

test("renderer errors preserve typed revision and paging messages", () => {
  assert.match(
    safeError(new RendererReadError("revision_changed", "internal", false)),
    /review changed/,
  );
  assert.match(
    safeError(new RendererReadError("pagination_limit", "internal", false)),
    /bounded partial snapshot/,
  );
});

test("read failures report the cause instead of one generic sentence", () => {
  assert.match(
    safeError(
      encodeReadFailure(
        new Error(
          'Invalid diff.open result: field "language" must be null or non-empty text',
        ),
      ),
    ),
    /field "language" must be null or non-empty text/,
  );
  assert.match(
    safeError(
      encodeReadFailure({
        code: "snapshot_expired",
        message: "The resource snapshot is invalid or expired; refetch it.",
        retryable: true,
      }),
    ),
    /snapshot expired/,
  );
  assert.equal(
    safeError(new Error("an opaque failure")),
    "The local service could not complete this read.",
  );
});

test("navigator publishes typed route changes", () => {
  const navigator = new Navigator();
  const routes = [];
  const unsubscribe = navigator.subscribe((route) => routes.push(route));
  navigator.navigate({
    kind: "inbox",
    repository: { handle: "r1", display_name: "tongs", forge_type: "github" },
  });
  unsubscribe();
  navigator.navigate({ kind: "inbox", repository: null });
  assert.equal(routes.length, 1);
  assert.equal(routes[0].repository.handle, "r1");
});

test("successful discovery reconciles only local repository routes", () => {
  const oldRepository = {
    handle: "repo-a",
    display_name: "Old name",
    forge_type: "github",
  };
  const updatedRepository = {
    ...oldRepository,
    display_name: "Updated name",
  };
  const repositories = [
    { handle: "repo-b", display_name: "B", forge_type: "gitlab" },
    updatedRepository,
  ];
  const inbox = { kind: "inbox", repository: oldRepository };
  const reconciled = reconcileDiscoveryRoute(inbox, repositories);
  assert.equal(reconciled.removed, false);
  assert.equal(reconciled.route.repository, updatedRepository);

  const review = {
    kind: "review",
    item: { handle: "review-a", repository: "repo-a", summary: {} },
    panel: "overview",
  };
  assert.equal(reconcileDiscoveryRoute(review, repositories).route, review);
  assert.deepEqual(reconcileDiscoveryRoute(review, repositories.slice(0, 1)), {
    route: { kind: "inbox", repository: null },
    removed: true,
  });
  assert.deepEqual(reconcileDiscoveryRoute(inbox, []), {
    route: { kind: "inbox", repository: null },
    removed: true,
  });

  const all = { kind: "inbox", repository: null };
  const plugin = {
    kind: "plugin",
    pluginId: "plugin",
    navigationId: "home",
    moduleId: "main",
  };
  assert.equal(reconcileDiscoveryRoute(all, []).route, all);
  assert.equal(reconcileDiscoveryRoute(plugin, []).route, plugin);
});

test("query coordinator cancels prior owned read and rejects its stale result", async () => {
  const canceled = [];
  const coordinator = new QueryCoordinator({
    cancelRead: async (token) => {
      canceled.push(token);
      return true;
    },
  });
  let resolveFirst;
  const first = coordinator.run("inbox", () => ({
    requestToken: "one",
    result: new Promise((resolve) => {
      resolveFirst = resolve;
    }),
  }));
  const second = coordinator.run("inbox", () => ({
    requestToken: "two",
    result: Promise.resolve("new"),
  }));
  resolveFirst("old");
  assert.equal(await second, "new");
  await assert.rejects(first, StaleQueryError);
  assert.deepEqual(canceled, ["one"]);
});

test("query coordinator cancels every token in an aggregated local-repository read", async () => {
  const canceled = [];
  const coordinator = new QueryCoordinator({
    cancelRead: async (token) => {
      canceled.push(token);
      return true;
    },
  });
  let resolveFirst;
  const first = coordinator.run("all", () => ({
    requestTokens: ["repo-a", "repo-b"],
    result: new Promise((resolve) => {
      resolveFirst = resolve;
    }),
  }));
  const second = coordinator.run("all", () => ({
    requestTokens: [],
    result: Promise.resolve("new"),
  }));
  resolveFirst("old");
  assert.equal(await second, "new");
  await assert.rejects(first, StaleQueryError);
  assert.deepEqual(canceled, ["repo-a", "repo-b"]);
});

test("partial review results remain usable while failures stay visible", () => {
  const result = inboxPresentation({
    items: [{ handle: "review", repository: "repo", summary: {} }],
    failures: [
      {
        code: "unavailable",
        message: "safe",
        retryable: true,
        repository: null,
      },
    ],
  });
  assert.deepEqual(result, { empty: false, partialFailures: 1 });
});

test("All reviews queries only current locally discovered handles", async () => {
  const calls = [];
  const bridge = {
    listReviews(params) {
      calls.push(params);
      return {
        requestToken: `token-${calls.length}`,
        result: Promise.resolve({ items: [], failures: [] }),
      };
    },
    openRepository() {
      throw new Error("renderer must not admit remote repositories");
    },
  };
  const read = listDiscoveredReviews(
    bridge,
    [{ handle: "local-a" }, { handle: "local-b" }],
    "all_open",
    "open",
  );
  await read.result;
  assert.deepEqual(calls, [
    { scope: "all_open", state: "open", repository: "local-a" },
    { scope: "all_open", state: "open", repository: "local-b" },
  ]);
  assert.deepEqual(read.requestTokens, ["token-1", "token-2"]);
});

function featureContext() {
  return {
    bridge: {},
    queries: {},
    repositories: [],
    repositoriesReady: true,
    repositoryGeneration: 1,
    reviewPanels: [],
    inlineAnchor: null,
    selectInlineAnchor: () => {},
    navigate: () => {},
  };
}
