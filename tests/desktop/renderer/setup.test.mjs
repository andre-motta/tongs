import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
// The runner already loads this module with --import; importing it again is a
// no-op there and keeps this file meaningful when it is run on its own. Like
// --import, it is evaluated before the jsdom globals below exist.
import { ASYNC_UTIL_TIMEOUT_MS } from "./setup.mjs";

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
const { cleanup, getConfig, render, screen, waitFor } = desktopRequire(
  "@testing-library/react",
);
afterEach(cleanup);

test("renderer waits allow several seconds for a slow runner", () => {
  assert.equal(ASYNC_UTIL_TIMEOUT_MS, 5000);
  assert.equal(getConfig().asyncUtilTimeout, ASYNC_UTIL_TIMEOUT_MS);
});

test("screen binds to the document installed after the setup", () => {
  render(React.createElement("p", null, "present text"));
  assert.equal(screen.getByText("present text").textContent, "present text");
});

test("a failed query reports a short message without the DOM", () => {
  render(React.createElement("p", { className: "marker-class" }, "present"));
  let message = "";
  try {
    screen.getByText("absent text");
  } catch (error) {
    message = `${error.name}: ${error.message}`;
  }
  assert.match(message, /^TestingLibraryElementError: Unable to find/);
  assert.equal(message.includes("marker-class"), false);
  assert.equal(message.includes("<body"), false);
  assert.ok(message.length < 400, `message has ${message.length} characters`);
});

test("a timed-out wait reports a short message without the DOM", async () => {
  render(React.createElement("p", { className: "marker-class" }, "present"));
  let message = "";
  try {
    await waitFor(() => screen.getByText("absent text"), { timeout: 50 });
  } catch (error) {
    message = String(error.message);
  }
  assert.match(message, /Unable to find/);
  assert.equal(message.includes("marker-class"), false);
  assert.equal(message.includes("<body"), false);
  assert.ok(message.length < 400, `message has ${message.length} characters`);
});
