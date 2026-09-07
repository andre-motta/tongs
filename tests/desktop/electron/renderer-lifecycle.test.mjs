import assert from "node:assert/strict";
import test from "node:test";
import {
  recoverRendererSession,
  RendererRecoveryCoordinator,
} from "../../../desktop/dist/src/main/renderer_lifecycle.js";

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

test("renderer recovery queues a crash received while a reload is completing", async () => {
  const calls = [];
  let releaseFirstLoad;
  const firstLoadBlocked = new Promise((resolve) => {
    releaseFirstLoad = resolve;
  });
  const coordinator = new RendererRecoveryCoordinator({
    resetBindings: () => calls.push("reset"),
    restartSidecar: async () => calls.push("restart"),
    refreshAssets: async () => calls.push("assets"),
    loadDocument: async () => {
      calls.push("load");
      if (calls.filter((call) => call === "load").length === 1) {
        await firstLoadBlocked;
      }
    },
    showFailure: async () => calls.push("failure"),
  });

  const reload = coordinator.request();
  await new Promise((resolve) => setImmediate(resolve));
  const crash = coordinator.request();
  releaseFirstLoad();
  await Promise.all([reload, crash]);

  assert.deepEqual(calls, [
    "reset",
    "restart",
    "assets",
    "load",
    "reset",
    "restart",
    "assets",
    "load",
  ]);
});
