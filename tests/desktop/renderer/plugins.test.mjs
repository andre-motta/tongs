import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { FeatureRegistry } from "../../../desktop/dist/src/renderer/core/navigation.js";
import { createInboxFeature } from "../../../desktop/dist/src/renderer/features/inbox/index.js";
import { createPluginsFeature } from "../../../desktop/dist/src/renderer/features/plugins/index.js";
import {
  PluginLocationPublisher,
  PluginRuntime,
  routeLocation,
} from "../../../desktop/dist/src/renderer/features/plugins/runtime.js";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const { JSDOM } = desktopRequire("jsdom");
const dom = new JSDOM(
  "<!doctype html><html><head></head><body></body></html>",
  {
    url: "tongs://app/index.html",
  },
);
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  ShadowRoot: dom.window.ShadowRoot,
  DOMException: dom.window.DOMException,
});
const { cleanup, fireEvent, render, waitFor } = desktopRequire(
  "@testing-library/react",
);
afterEach(() => {
  cleanup();
  document.body.replaceChildren();
  document.head.replaceChildren();
});

test("plugin navigation renders scoped contributions and unavailable states", async () => {
  const navigated = [];
  const bridge = bridgeFixture({
    plugins: [
      plugin("alpha"),
      plugin("beta"),
      plugin("terminal", "terminal_only", null),
      plugin("disabled", "disabled", unavailableManifest()),
      plugin(
        "old",
        "incompatible",
        unavailableManifest(),
        "Requires a newer host.",
      ),
      plugin("broken", "failed", null, "Provider failed safely."),
    ],
  });
  const feature = createPluginsFeature(bridge.value, dependencies());
  await feature.runtime.refresh();
  const view = render(
    feature.navigation(
      feature.runtime.snapshot,
      { kind: "inbox", repository: null },
      (route) => navigated.push(route),
    ),
  );

  assert.ok(view.getByText("Available in the terminal only."));
  assert.ok(view.getByText("Disabled in configuration."));
  assert.ok(view.getByText("Requires a newer host."));
  assert.ok(view.getByText("Provider failed safely."));
  const dashboards = view.getAllByRole("button", { name: "Dashboard" });
  assert.equal(dashboards.length, 2);
  fireEvent.click(dashboards[1]);
  assert.deepEqual(navigated[0], {
    kind: "plugin",
    pluginId: "beta",
    navigationId: "dashboard",
    moduleId: "dashboard",
  });
  const commands = feature.commandsFor(feature.runtime.snapshot);
  assert.deepEqual(
    commands.map((item) => item.id),
    ["plugin:alpha:command:open", "plugin:beta:command:open"],
  );
  await feature.runtime.dispose();
});

test("plugin workspace mounts a usable module and renders help as inert text", async () => {
  const bridge = bridgeFixture();
  const feature = createPluginsFeature(
    bridge.value,
    dependencies({
      loadHelp: async () => "# Safe help\n\n<img src=x onerror=alert(1)>",
      loadModule: async () => ({
        mount: (container) => {
          const button = document.createElement("button");
          button.textContent = "Refresh example";
          container.append(button);
          return () => button.remove();
        },
      }),
    }),
  );
  await feature.runtime.refresh();
  const context = {
    bridge: bridge.value,
    queries: {},
    repositories: [],
    repositoriesReady: true,
    repositoryGeneration: 1,
    reviewPanels: [],
    inlineAnchor: null,
    selectInlineAnchor: () => undefined,
    navigate: () => undefined,
  };
  const view = render(feature.render(context, route()));
  await waitFor(() =>
    assert.equal(
      view.container
        .querySelector(".plugin-module")
        ?.shadowRoot?.querySelector("button")?.textContent,
      "Refresh example",
    ),
  );
  assert.ok(await view.findByText("Safe help"));
  assert.ok(
    await view.findByText("<img src=x onerror=alert(1)>", { exact: true }),
  );
  assert.equal(view.container.querySelector("img"), null);
  await feature.runtime.dispose();
});

test("a failed plugin mount does not disturb the core inbox feature", async () => {
  const bridge = bridgeFixture();
  const plugins = createPluginsFeature(
    bridge.value,
    dependencies({
      loadModule: async () => ({
        mount: () => {
          throw new Error("bad plugin");
        },
      }),
    }),
  );
  await plugins.runtime.refresh();
  const container = document.createElement("div");
  const statuses = [];
  await plugins.runtime.mount(
    route(),
    container,
    () => undefined,
    (value) => statuses.push(value),
  );
  const registry = new FeatureRegistry();
  const inbox = createInboxFeature();
  registry.register(inbox);
  registry.register(plugins);
  assert.equal(registry.find({ kind: "inbox", repository: null }), inbox);
  assert.match(statuses.at(-1)?.error ?? "", /Core reviews remain available/);
  await plugins.runtime.dispose();
});

test("mounted API scopes invoke, events, navigation, location, and focus", async () => {
  const invocations = [];
  const navigated = [];
  let api;
  let eventPayload = null;
  let cleanupCalls = 0;
  let stylesheetRemovals = 0;
  let stylesheetRoot = null;
  const bridge = bridgeFixture({
    invoke: (params) => {
      invocations.push(params);
      return { ok: true };
    },
  });
  const runtime = new PluginRuntime(
    bridge.value,
    dependencies({
      addStylesheet: async (_url, _signal, root) => {
        stylesheetRoot = root;
        return () => {
          stylesheetRemovals += 1;
        };
      },
      loadModule: async () => ({
        mount: (container, received) => {
          api = received;
          const button = document.createElement("button");
          button.textContent = "Summary";
          container.append(button);
          received.bindFocusTarget("summary", button);
          received.on("refreshed", (payload) => {
            eventPayload = payload;
          });
          return () => {
            cleanupCalls += 1;
          };
        },
      }),
    }),
  );
  runtime.start();
  await tick();
  const container = document.createElement("div");
  document.body.append(container);
  const statuses = [];
  await runtime.mount(
    route(),
    container,
    (next) => navigated.push(next),
    (value) => statuses.push(value),
  );

  assert.deepEqual(await api.invoke("refresh", { requested: true }), {
    ok: true,
  });
  assert.deepEqual(invocations, [
    { plugin: "alpha", method: "refresh", params: { requested: true } },
  ]);
  assert.deepEqual(api.currentLocation(), {
    kind: "plugin",
    plugin_id: "alpha",
    navigation_id: "dashboard",
    module_id: "dashboard",
  });
  api.navigate("dashboard");
  assert.deepEqual(navigated[0], route());
  assert.throws(() => api.navigate("core-inbox"), /Unknown plugin navigation/);
  await assert.rejects(api.invoke("other", {}), /Unknown plugin method/);
  bridge.emit({
    sequence: 1,
    name: "plugin.event",
    data: { plugin: "alpha", event: "refreshed", payload: { count: 2 } },
  });
  assert.deepEqual(eventPayload, { count: 2 });
  bridge.emit({
    sequence: 2,
    name: "plugin.event",
    data: { plugin: "beta", event: "refreshed", payload: { count: 9 } },
  });
  assert.deepEqual(eventPayload, { count: 2 });
  bridge.emit({
    sequence: 3,
    name: "plugin.focus",
    data: { plugin: "alpha", target: "summary", metadata: {} },
  });
  assert.equal(container.shadowRoot?.activeElement?.textContent, "Summary");
  bridge.emit({
    sequence: 4,
    name: "plugin.focus",
    data: { plugin: "alpha", target: "missing", metadata: {} },
  });
  assert.equal(container.shadowRoot?.activeElement?.textContent, "Summary");
  bridge.emit({
    sequence: 5,
    name: "plugin.navigation",
    data: { plugin: "alpha", navigation: "core-inbox" },
  });
  assert.equal(navigated.length, 1);
  assert.deepEqual(statuses.at(-1), { loading: false, error: null });
  assert.equal(stylesheetRoot, container.shadowRoot);

  await runtime.unmount();
  bridge.emit({
    sequence: 6,
    name: "plugin.event",
    data: { plugin: "alpha", event: "refreshed", payload: { count: 7 } },
  });
  assert.deepEqual(eventPayload, { count: 2 });
  assert.equal(cleanupCalls, 1);
  assert.equal(stylesheetRemovals, 1);
  assert.equal(container.childElementCount, 0);
  await runtime.dispose();
});

test("focus bindings reject other modules, outside elements, and arbitrary selectors", async () => {
  let api;
  const runtime = new PluginRuntime(
    bridgeFixture().value,
    dependencies({
      loadModule: async () => ({
        mount: (_container, received) => {
          api = received;
          return () => undefined;
        },
      }),
    }),
  );
  await runtime.refresh();
  const container = document.createElement("div");
  const inside = document.createElement("div");
  const outside = document.createElement("button");
  container.append(inside);
  document.body.append(container, outside);
  await runtime.mount(
    route(),
    container,
    () => undefined,
    () => undefined,
  );
  assert.throws(
    () => api.bindFocusTarget("summary", outside),
    /Invalid plugin focus/,
  );
  assert.throws(
    () => api.bindFocusTarget("summary", inside),
    /Invalid plugin focus/,
  );
  assert.throws(
    () => api.bindFocusTarget("#content", outside),
    /Invalid plugin focus/,
  );
  await runtime.dispose();
});

test("late mount completion is cleaned without reviving a stopped module", async () => {
  const gate = deferred();
  let cleanupCalls = 0;
  const statuses = [];
  const runtime = new PluginRuntime(
    bridgeFixture().value,
    dependencies({
      loadModule: async () => ({
        mount: async (container) => {
          await gate.promise;
          container.textContent = "late";
          return () => {
            cleanupCalls += 1;
          };
        },
      }),
    }),
  );
  await runtime.refresh();
  const container = document.createElement("div");
  const mounting = runtime.mount(
    route(),
    container,
    () => undefined,
    (value) => statuses.push(value),
  );
  await tick();
  await runtime.unmount();
  gate.resolve();
  await mounting;
  assert.equal(cleanupCalls, 1);
  assert.equal(container.childElementCount, 0);
  assert.equal(
    statuses.some((item) => item.loading === false && item.error === null),
    false,
  );
  await runtime.dispose();
});

test("late invocation completion is rejected after navigation cleanup", async () => {
  const result = deferred();
  let api;
  const bridge = bridgeFixture({ invokeResult: result.promise });
  const runtime = new PluginRuntime(
    bridge.value,
    dependencies({
      loadModule: async () => ({
        mount: (_container, received) => {
          api = received;
          return () => undefined;
        },
      }),
    }),
  );
  await runtime.refresh();
  await runtime.mount(
    route(),
    document.createElement("div"),
    () => undefined,
    () => undefined,
  );
  const invocation = api.invoke("refresh", {});
  await runtime.unmount();
  result.resolve({ value: { stale: true } });
  await assert.rejects(invocation, /Plugin module stopped/);
  assert.equal(bridge.cancelled.length, 1);
  await runtime.dispose();
});

test("cleanup timeout and failure are bounded and reported independently", async () => {
  for (const cleanupValue of [
    () => new Promise(() => undefined),
    () => Promise.reject(new Error("no")),
  ]) {
    const runtime = new PluginRuntime(
      bridgeFixture().value,
      dependencies({
        cleanupTimeoutMs: 8,
        loadModule: async () => ({ mount: () => cleanupValue }),
      }),
    );
    await runtime.refresh();
    const container = document.createElement("div");
    await runtime.mount(
      route(),
      container,
      () => undefined,
      () => undefined,
    );
    const started = performance.now();
    await runtime.unmount();
    assert.ok(performance.now() - started < 200);
    assert.match(runtime.snapshot.notices.at(-1)?.message ?? "", /cleanup/);
    await runtime.dispose();
  }
});

test("mount failures and changed resources remain isolated", async () => {
  const bridge = bridgeFixture();
  const statuses = [];
  const runtime = new PluginRuntime(
    bridge.value,
    dependencies({
      loadModule: async () => {
        throw new Error("module syntax failure");
      },
    }),
  );
  await runtime.refresh();
  const container = document.createElement("div");
  await runtime.mount(
    route(),
    container,
    () => undefined,
    (value) => statuses.push(value),
  );
  assert.match(statuses.at(-1)?.error ?? "", /Core reviews remain available/);

  bridge.setAssets([]);
  await runtime.refresh();
  await runtime.mount(
    route(),
    container,
    () => undefined,
    (value) => statuses.push(value),
  );
  assert.equal(
    statuses.at(-1)?.error,
    "The plugin resources changed or are missing.",
  );
  await runtime.dispose();
});

test("resource matching rejects remote, cross-plugin, and duplicate handles", async () => {
  for (const assets of [
    [
      asset(
        "alpha",
        "alpha-module",
        "module",
        "https://example.invalid/main.mjs",
      ),
      asset("alpha", "alpha-style", "stylesheet"),
    ],
    [
      asset("beta", "alpha-module", "module"),
      asset("alpha", "alpha-style", "stylesheet"),
    ],
    [
      asset("alpha", "alpha-module", "module"),
      asset("alpha", "alpha-module", "module"),
      asset("alpha", "alpha-style", "stylesheet"),
    ],
  ]) {
    const statuses = [];
    const runtime = new PluginRuntime(
      bridgeFixture({ assets }).value,
      dependencies(),
    );
    await runtime.refresh();
    await runtime.mount(
      route(),
      document.createElement("div"),
      () => undefined,
      (value) => statuses.push(value),
    );
    assert.equal(
      statuses.at(-1)?.error,
      "The plugin resources changed or are missing.",
    );
    await runtime.dispose();
  }
});

test("dispose cancels catalog and invocation reads and removes the event listener", async () => {
  const pendingPlugin = deferred();
  const bridge = bridgeFixture({ pluginResult: pendingPlugin.promise });
  const runtime = new PluginRuntime(bridge.value, dependencies());
  runtime.start();
  await tick();
  await runtime.dispose();
  assert.ok(bridge.cancelled.length >= 2);
  assert.equal(bridge.listenerCount(), 0);
  pendingPlugin.resolve({ plugins: [plugin("alpha")] });
});

test("location publication stays ordered, drops queued stale routes, and resets", async () => {
  const first = deferred();
  const locations = [];
  let calls = 0;
  const publisher = new PluginLocationPublisher({
    setLocation: async ({ location }) => {
      locations.push(location);
      if (calls++ === 0) await first.promise;
      return { accepted: true };
    },
  });
  const firstPublish = publisher.publish({ kind: "inbox", repository: null });
  await tick();
  const review = {
    kind: "review",
    item: { handle: "review-69" },
    panel: "diff",
  };
  const stalePublish = publisher.publish(review);
  const currentPublish = publisher.publish(route());
  first.resolve();
  await Promise.all([firstPublish, stalePublish, currentPublish]);
  assert.deepEqual(locations, [
    { kind: "inbox" },
    {
      kind: "plugin",
      plugin_id: "alpha",
      navigation_id: "dashboard",
      module_id: "dashboard",
    },
  ]);
  await publisher.dispose();
  assert.equal(locations.at(-1), null);
  assert.deepEqual(routeLocation(route()), locations.at(-2));
});

test("location failures do not block a newer host-generated location", async () => {
  const locations = [];
  const publisher = new PluginLocationPublisher({
    setLocation: async ({ location }) => {
      locations.push(location);
      if (locations.length === 1) throw new Error("sidecar restarting");
      return { accepted: true };
    },
  });
  await publisher.publish({ kind: "inbox", repository: null });
  await publisher.publish(route());
  assert.deepEqual(locations.at(-1), routeLocation(route()));
});

function plugin(
  pluginId = "alpha",
  state = "started",
  manifest = startedManifest(pluginId),
  error = null,
) {
  return {
    plugin_id: pluginId,
    state,
    has_terminal_entry_point: true,
    has_desktop_entry_point: state !== "terminal_only",
    error: error
      ? { code: "plugin_error", message: error, retryable: false }
      : null,
    manifest,
  };
}

function startedManifest(pluginId) {
  return {
    title: `${pluginId} plugin`,
    version: "1.0.0",
    api_major: 1,
    modules: [
      {
        id: "dashboard",
        title: "Dashboard",
        entry_asset: `${pluginId}-module`,
        stylesheets: [`${pluginId}-style`],
      },
    ],
    navigation: [
      { id: "dashboard", title: "Dashboard", module_id: "dashboard" },
    ],
    commands: [
      {
        id: "open",
        title: `Open ${pluginId}`,
        navigation_id: "dashboard",
        help_text: "",
      },
    ],
    methods: ["refresh"],
    events: ["refreshed"],
    focus_targets: [
      { id: "summary", title: "Summary", module_id: "dashboard" },
    ],
    help_asset: `${pluginId}-help`,
    reads: [],
    assets_available: true,
  };
}

function unavailableManifest() {
  return {
    ...startedManifest("unavailable"),
    modules: [
      {
        id: "dashboard",
        title: "Dashboard",
        entry_asset: null,
        stylesheets: [null],
      },
    ],
    help_asset: null,
    assets_available: false,
  };
}

function asset(
  pluginId,
  handle,
  kind,
  url = `tongs://app/assets/${encodeURIComponent(handle)}`,
) {
  const extension =
    kind === "module" ? "mjs" : kind === "stylesheet" ? "css" : "md";
  return {
    source: "plugin",
    asset_id: `${handle}.${extension}`,
    plugin_id: pluginId,
    kind,
    media_type:
      kind === "module"
        ? "text/javascript"
        : kind === "stylesheet"
          ? "text/css"
          : "text/markdown",
    byte_count: 12,
    sha256: "a".repeat(64),
    url,
  };
}

function defaultAssets(pluginIds = ["alpha"]) {
  return pluginIds.flatMap((pluginId) => [
    asset(pluginId, `${pluginId}-module`, "module"),
    asset(pluginId, `${pluginId}-style`, "stylesheet"),
    asset(pluginId, `${pluginId}-help`, "help"),
  ]);
}

function route(pluginId = "alpha") {
  return {
    kind: "plugin",
    pluginId,
    navigationId: "dashboard",
    moduleId: "dashboard",
  };
}

function bridgeFixture(options = {}) {
  let plugins = options.plugins ?? [plugin("alpha")];
  let assets =
    options.assets ??
    defaultAssets(
      plugins
        .filter((item) => item.state === "started")
        .map((item) => item.plugin_id),
    );
  let sequence = 0;
  const listeners = new Set();
  const cancelled = [];
  const read = (value) => ({
    requestToken: `request-${++sequence}`,
    result: Promise.resolve(value),
  });
  const value = {
    listPlugins: () => {
      const result = options.pluginResult ?? { plugins };
      return read(result);
    },
    listAssets: () => read(assets),
    invokePlugin: (params) =>
      read(options.invokeResult ?? { value: options.invoke?.(params) ?? null }),
    cancelRead: async (token) => {
      cancelled.push(token);
      return true;
    },
    onEvent: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  return {
    value,
    cancelled,
    emit: (event) => {
      for (const listener of listeners) listener(event);
    },
    listenerCount: () => listeners.size,
    setAssets: (next) => {
      assets = next;
    },
    setPlugins: (next) => {
      plugins = next;
    },
  };
}

function dependencies(changes = {}) {
  return {
    loadModule: async () => ({ mount: () => () => undefined }),
    loadHelp: async () => "# Help\n\nSafe plugin help.",
    addStylesheet: async () => () => undefined,
    cleanupTimeoutMs: 30,
    ...changes,
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function tick() {
  await new Promise((resolve) => setTimeout(resolve, 0));
}
