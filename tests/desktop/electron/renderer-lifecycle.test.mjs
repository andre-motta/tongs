import assert from "node:assert/strict";
import test from "node:test";
import { recoverRendererSession } from "../../../desktop/dist/src/main/renderer_lifecycle.js";

test("renderer recovery resets bindings and reloads after sidecar and assets", async () => {
  const calls = [];
  const recovered = await recoverRendererSession({
    resetBindings: () => calls.push("reset"),
    restartSidecar: async () => calls.push("restart"),
    refreshAssets: async () => calls.push("assets"),
    loadDocument: async () => calls.push("load"),
    showFailure: async () => calls.push("failure"),
  });
  assert.equal(recovered, true);
  assert.deepEqual(calls, ["reset", "restart", "assets", "load"]);
});

test("renderer recovery reloads a visible safe failure after restart rejection", async () => {
  const calls = [];
  const recovered = await recoverRendererSession({
    resetBindings: () => calls.push("reset"),
    restartSidecar: async () => { calls.push("restart"); throw new Error("secret"); },
    refreshAssets: async () => calls.push("assets"),
    loadDocument: async () => calls.push("load"),
    showFailure: async () => calls.push("failure"),
  });
  assert.equal(recovered, false);
  assert.deepEqual(calls, ["reset", "restart", "load", "failure"]);
});
