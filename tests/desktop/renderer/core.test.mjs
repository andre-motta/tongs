import assert from "node:assert/strict";
import test from "node:test";
import { parseInertMarkdown } from "../../../desktop/dist/src/renderer/core/markdown.js";
import {
  FeatureRegistry,
  Navigator,
} from "../../../desktop/dist/src/renderer/core/navigation.js";
import {
  QueryCoordinator,
  StaleQueryError,
} from "../../../desktop/dist/src/renderer/core/query.js";
import {
  inboxPresentation,
  listDiscoveredReviews,
} from "../../../desktop/dist/src/renderer/features/inbox/index.js";

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
    "open",
  );
  await read.result;
  assert.deepEqual(calls, [
    { scope: "all_open", state: "open", repository: "local-a" },
    { scope: "all_open", state: "open", repository: "local-b" },
  ]);
  assert.deepEqual(read.requestTokens, ["token-1", "token-2"]);
});
