import assert from "node:assert/strict";
import test from "node:test";
import {
  CONTENT_SECURITY_POLICY,
  isAllowedNavigation,
  validateAssetUrl,
  validateInvocation,
  validateRenderer,
} from "../src/security.mjs";

test("asset URLs are restricted to an unadorned IPv4 loopback origin", () => {
  assert.equal(validateAssetUrl("http://127.0.0.1:3456/").origin, "http://127.0.0.1:3456");
  for (const url of [
    "https://127.0.0.1:3456/",
    "http://localhost:3456/",
    "http://127.0.0.1:3456/path",
    "http://user@127.0.0.1:3456/",
    "http://example.test/",
  ]) {
    assert.throws(() => validateAssetUrl(url));
  }
});

test("navigation is limited to the loopback application root", () => {
  const origin = "http://127.0.0.1:4000";
  assert.equal(isAllowedNavigation(`${origin}/`, origin), true);
  assert.equal(isAllowedNavigation(`${origin}/plugins/sample/module.js`, origin), false);
  assert.equal(isAllowedNavigation("https://example.test/", origin), false);
  assert.equal(isAllowedNavigation("not a URL", origin), false);
});

test("renderer invocations have a bounded allowlist and JSON object params", () => {
  assert.deepEqual(validateInvocation("health"), { method: "health", params: {} });
  assert.throws(() => validateInvocation("merge", {}));
  assert.throws(() => validateInvocation("health", []));
  assert.throws(() => validateInvocation("health", { payload: "x".repeat(70_000) }));
  const circular = {};
  circular.self = circular;
  assert.throws(() => validateInvocation("health", circular));
});

test("renderer validation requires the owned main frame at the asset origin", () => {
  const mainFrame = { url: "http://127.0.0.1:4000/" };
  const webContents = { mainFrame };
  validateRenderer({ sender: webContents, senderFrame: mainFrame }, webContents, "http://127.0.0.1:4000");
  assert.throws(() =>
    validateRenderer(
      { sender: webContents, senderFrame: { url: mainFrame.url } },
      webContents,
      "http://127.0.0.1:4000",
    ),
  );
  mainFrame.url = "http://127.0.0.1:5000/";
  assert.throws(() =>
    validateRenderer({ sender: webContents, senderFrame: mainFrame }, webContents, "http://127.0.0.1:4000"),
  );
});

test("content policy blocks remote script, object, framing, and form sources", () => {
  for (const directive of [
    "default-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "script-src 'self'",
  ]) {
    assert.ok(CONTENT_SECURITY_POLICY.includes(directive));
  }
});
