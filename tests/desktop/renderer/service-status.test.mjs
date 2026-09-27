import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { ServiceStatusPublisher } from "../../../desktop/dist/src/main/sidecar.js";
import {
  SERVICE_STATUS_TEXT,
  ServiceStatusLine,
  ServiceStatusModel,
} from "../../../desktop/dist/src/renderer/core/service-status.js";
import { harness, transportFor } from "../electron/fake-sidecar.mjs";

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
const { act, cleanup, render } = desktopRequire("@testing-library/react");
afterEach(cleanup);

/**
 * Connects a real transport over a fake service to the header through the same
 * path the desktop uses: the main-process publisher sends each change and
 * answers the current-state query, and the renderer bridge relays both.
 */
function wire(transport) {
  const listeners = new Set();
  const publisher = new ServiceStatusPublisher(transport, (status) => {
    const copy = structuredClone(status);
    for (const listener of listeners) listener(copy);
  });
  publisher.start();
  const bridge = {
    getServiceStatus: async () => structuredClone(publisher.current),
    onServiceStatus: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  return { bridge, publisher, listeners };
}

function headerText() {
  return document.querySelector("#service-status")?.textContent ?? null;
}

function headerClass() {
  return document.querySelector("#service-status")?.className ?? null;
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setImmediate(resolve));
  });
}

test("header changes from connected to not running when the service stops, and back after a restart", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const { bridge, publisher, listeners } = wire(transport);
  const model = new ServiceStatusModel();
  const view = render(
    React.createElement(ServiceStatusLine, { model, bridge }),
  );
  await settle();
  assert.equal(headerText(), "Local service connected");
  assert.equal(headerClass(), "service-status service-status-ready");

  await act(async () => {
    fake.children[0].finish(1, null);
  });
  assert.equal(headerText(), "Local service not running");
  assert.equal(headerClass(), "service-status service-status-error");

  await act(async () => {
    await transport.restart();
  });
  assert.equal(headerText(), "Local service connected");
  assert.equal(headerClass(), "service-status service-status-ready");

  view.unmount();
  assert.equal(listeners.size, 0);
  publisher.dispose();
  await transport.stop();
});

test("header shows not running when the connection fails on a bad frame", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const { bridge, publisher } = wire(transport);
  const model = new ServiceStatusModel();
  render(React.createElement(ServiceStatusLine, { model, bridge }));
  await settle();
  assert.equal(headerText(), "Local service connected");
  const pending = transport.requestRead("assets.list", {}).result;
  await act(async () => {
    fake.children[0].stdout.write("not json\n");
    await assert.rejects(pending, { code: "invalid_frame" });
  });
  assert.equal(headerText(), "Local service not running");
  publisher.dispose();
  await transport.stop();
});

test("a header mounted after the service stopped starts at not running", async () => {
  const fake = harness();
  const transport = transportFor(fake);
  await transport.start();
  const { bridge, publisher } = wire(transport);
  fake.children[0].finish(1, null);
  const model = new ServiceStatusModel();
  model.probeSucceeded();
  render(React.createElement(ServiceStatusLine, { model, bridge }));
  await settle();
  assert.equal(headerText(), "Local service not running");
  publisher.dispose();
  await transport.stop();
});

test("a stale current-state answer cannot undo a newer stop", async () => {
  const model = new ServiceStatusModel();
  model.applyStatus({ state: "stopped", revision: 4 });
  model.applyStatus({ state: "connected", revision: 3 });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.stopped);
  model.applyStatus({ state: "connected", revision: 5 });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.connected);
});

test("not running outranks the updates notice, and a reconnect clears it", () => {
  const model = new ServiceStatusModel();
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.connecting);
  model.applyStatus({ state: "connected", revision: 1 });
  model.updatesAvailable();
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.updates);
  model.applyStatus({ state: "stopped", revision: 2 });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.stopped);
  model.applyStatus({ state: "connected", revision: 3 });
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.connected);
});

test("malformed status reports are ignored", () => {
  const model = new ServiceStatusModel();
  model.applyStatus({ state: "exploded", revision: 1 });
  model.applyStatus({ state: "stopped", revision: -1 });
  model.applyStatus(null);
  assert.equal(model.view.text, SERVICE_STATUS_TEXT.connecting);
});
