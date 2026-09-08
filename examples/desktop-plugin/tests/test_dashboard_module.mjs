import assert from "node:assert/strict";
import test from "node:test";
import { mount } from "../src/tongs_example_dashboard/assets/dashboard.mjs";

class MockElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.textContent = "";
    this.attributes = new Map();
    this.className = "";
    this.disabled = false;
    this.tabIndex = -1;
  }

  append(...children) {
    this.children.push(...children);
  }

  prepend(...children) {
    this.children.unshift(...children);
  }

  replaceChildren(...children) {
    this.children = [...children];
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  addEventListener(name, listener) {
    this.listeners.set(name, listener);
  }

  removeEventListener(name, listener) {
    if (this.listeners.get(name) === listener) this.listeners.delete(name);
  }

  async click() {
    await this.listeners.get("click")?.();
  }
}

function find(element, tagName) {
  for (const child of element.children) {
    if (child.tagName === tagName) return child;
    const match = find(child, tagName);
    if (match) return match;
  }
  return null;
}

function makeApi(container) {
  const controller = new AbortController();
  const listeners = new Map();
  const calls = [];
  const notices = [];
  const navigations = [];
  const bindings = [];
  return {
    api: Object.freeze({
      pluginId: "example_dashboard",
      signal: controller.signal,
      invoke: async (method, params) => {
        calls.push([method, params]);
        assert.equal(method, "refresh");
        return {
          summary: { open_reviews: 3, waiting_on_me: 1, ci_passing: 2 },
          reviews: [{ title: "Updated row", repository: "example/tongs", status: "ready" }],
        };
      },
      on: (event, listener) => {
        listeners.set(event, listener);
        return () => listeners.delete(event);
      },
      notify: (message, severity) => notices.push([message, severity]),
      navigate: (navigation) => navigations.push(navigation),
      currentLocation: () => Object.freeze({ navigation_id: "dashboard" }),
      bindFocusTarget: (target, element) => {
        assert.ok(container.children.includes(element));
        bindings.push([target, element]);
        return () => bindings.splice(bindings.findIndex(([id]) => id === target), 1);
      },
    }),
    controller,
    listeners,
    calls,
    notices,
    navigations,
    bindings,
  };
}

test("dashboard mounts, handles events and refresh, and cleans every binding", async () => {
  globalThis.document = { createElement: (tagName) => new MockElement(tagName) };
  const container = new MockElement("main");
  const fixture = makeApi(container);
  const cleanup = mount(container, fixture.api);

  assert.deepEqual(fixture.navigations, []);
  assert.equal(fixture.bindings[0][0], "summary");
  assert.ok(fixture.listeners.has("refreshed"));
  assert.equal(container.children.length, 7);

  fixture.listeners.get("refreshed")({
    summary: { open_reviews: 4, waiting_on_me: 2, ci_passing: 3 },
    reviews: [],
  });
  const openReviews = fixture.bindings[0][1].children[1].children[1];
  assert.equal(openReviews.textContent, "4");

  const buttons = container.children.filter((child) => child.tagName === "button");
  await buttons[0].click();
  assert.deepEqual(fixture.navigations, ["dashboard"]);
  await buttons[1].click();
  assert.deepEqual(fixture.calls, [["refresh", {}]]);
  assert.deepEqual(fixture.notices, [["Example dashboard refreshed", "information"]]);

  fixture.controller.abort();
  assert.equal(buttons[1].disabled, true);
  cleanup();
  assert.equal(container.children.length, 0);
  assert.equal(fixture.listeners.size, 0);
  assert.equal(fixture.bindings.length, 0);
  assert.equal(buttons[0].listeners.size, 0);
  assert.equal(buttons[1].listeners.size, 0);
  assert.equal(fixture.api.signal.aborted, true);
  await buttons[0].click();
  await buttons[1].click();
  assert.deepEqual(fixture.navigations, ["dashboard"]);
  assert.deepEqual(fixture.calls, [["refresh", {}]]);
  assert.deepEqual(fixture.notices, [["Example dashboard refreshed", "information"]]);
});

function makeDeferredApi(container) {
  const fixture = makeApi(container);
  const deferred = [];
  fixture.api = Object.freeze({
    ...fixture.api,
    invoke: (method, params) => {
      fixture.calls.push([method, params]);
      const entry = { resolve: null, reject: null, promise: null };
      entry.promise = new Promise((resolve, reject) => {
        entry.resolve = resolve;
        entry.reject = reject;
      });
      deferred.push(entry);
      return entry.promise;
    },
  });
  fixture.deferred = deferred;
  return fixture;
}

test("a refresh that resolves after cleanup cannot touch detached DOM or notify", async () => {
  globalThis.document = { createElement: (tagName) => new MockElement(tagName) };
  const container = new MockElement("main");
  const fixture = makeDeferredApi(container);
  const cleanup = mount(container, fixture.api);
  const navigate = container.children.at(-2);
  const refresh = container.children.at(-1);
  const refreshPromise = refresh.click();

  assert.equal(fixture.calls.length, 1);
  cleanup();
  await navigate.click();
  await refresh.click();
  assert.deepEqual(fixture.navigations, []);
  assert.equal(fixture.calls.length, 1);
  fixture.deferred[0].resolve({
    summary: { open_reviews: 99, waiting_on_me: 99, ci_passing: 99 },
    reviews: [{ title: "Detached row" }],
  });
  await refreshPromise;

  assert.deepEqual(fixture.notices, []);
  assert.equal(container.children.length, 0);
  assert.equal(refresh.disabled, true);
});

test("a refresh that rejects after cleanup is consumed without notification", async () => {
  globalThis.document = { createElement: (tagName) => new MockElement(tagName) };
  const container = new MockElement("main");
  const fixture = makeDeferredApi(container);
  const cleanup = mount(container, fixture.api);
  const refresh = container.children.at(-1);
  const refreshPromise = refresh.click();

  cleanup();
  fixture.deferred[0].reject(new Error("late failure"));
  await refreshPromise;

  assert.deepEqual(fixture.notices, []);
  assert.equal(container.children.length, 0);
});
