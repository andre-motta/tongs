import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import {
  WorkspaceUtilityController,
  WorkspaceUtilityOverlay,
  createUtilitiesFeature,
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
  Node: dom.window.Node,
});
const { cleanup, fireEvent, render } = desktopRequire("@testing-library/react");
afterEach(cleanup);

function context(overrides = {}) {
  const calls = { copied: [], clears: 0, cancellations: 0 };
  return {
    calls,
    value: {
      bridge: {
        copyReviewUrl: async (review) => {
          calls.copied.push(review);
          return { outcome: "copied", message: "Review URL copied to the clipboard." };
        },
        clearCache: async () => {
          calls.clears += 1;
          return { outcome: "cleared", message: "Shared API cache cleared. Draft reviews were preserved." };
        },
      },
      queries: {
        cancelAll: async () => { calls.cancellations += 1; },
      },
      ...overrides,
    },
  };
}

const reviewRoute = {
  kind: "review",
  item: { handle: "review-handle" },
  panel: "overview",
};

test("Copy URL command is review-scoped and passes only the opaque review", async () => {
  const controller = new WorkspaceUtilityController();
  const feature = createUtilitiesFeature(controller);
  const copy = feature.commands.find((command) => command.id === "workspace.copy-review-url");
  const { calls, value } = context();

  assert.equal(copy.isVisible(value, reviewRoute), true);
  assert.equal(copy.isVisible(value, { kind: "inbox", repository: null }), false);
  await copy.run(value, reviewRoute);

  assert.deepEqual(calls.copied, ["review-handle"]);
  assert.deepEqual(controller.state, {
    kind: "notice",
    severity: "status",
    message: "Review URL copied to the clipboard.",
  });
});

test("Clear Cache requires confirmation and preserves the current draft view", async () => {
  const controller = new WorkspaceUtilityController();
  const feature = createUtilitiesFeature(controller);
  const clear = feature.commands.find((command) => command.id === "workspace.clear-cache");
  const { calls, value } = context();

  clear.run(value, reviewRoute);
  const view = render(
    React.createElement(WorkspaceUtilityOverlay, { controller, context: value }),
  );
  assert.match(view.getByRole("alertdialog").textContent, /drafts will be preserved/);
  assert.equal(calls.clears, 0);

  fireEvent.click(view.getByRole("button", { name: "Clear cache" }));
  await view.findByText("Shared API cache cleared. Draft reviews were preserved.");
  assert.equal(calls.clears, 1);
  assert.equal(calls.cancellations, 1);
  assert.deepEqual(reviewRoute, {
    kind: "review",
    item: { handle: "review-handle" },
    panel: "overview",
  });
});

test("Clear Cache cancel avoids reads while an attempted clear invalidates them", async () => {
  const controller = new WorkspaceUtilityController();
  const feature = createUtilitiesFeature(controller);
  const clear = feature.commands.find((command) => command.id === "workspace.clear-cache");
  const { calls, value } = context({
    bridge: {
      copyReviewUrl: async () => ({ outcome: "failed", message: "unused" }),
      clearCache: async () => {
        calls.clears += 1;
        return { outcome: "failed", message: "Cache clear failed safely." };
      },
    },
  });
  clear.run(value, reviewRoute);
  const view = render(
    React.createElement(WorkspaceUtilityOverlay, { controller, context: value }),
  );

  fireEvent.click(view.getByRole("button", { name: "Cancel" }));
  assert.equal(view.queryByRole("alertdialog"), null);
  assert.equal(calls.clears, 0);
  clear.run(value, reviewRoute);
  fireEvent.click(await view.findByRole("button", { name: "Clear cache" }));
  await view.findByText("Cache clear failed safely.");
  assert.equal(calls.clears, 1);
  assert.equal(calls.cancellations, 1);
});
