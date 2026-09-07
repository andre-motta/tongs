import assert from "node:assert/strict";
import test from "node:test";
import { shouldCopyPythonAsset } from "../scripts/build-distribution.mjs";

test("Python distribution assets exclude caches and bytecode", () => {
  assert.equal(shouldCopyPythonAsset("/app/backend/__init__.py"), true);
  assert.equal(shouldCopyPythonAsset("/app/backend/data.json"), true);
  assert.equal(shouldCopyPythonAsset("/app/backend/__pycache__"), false);
  assert.equal(shouldCopyPythonAsset("/app/backend/__pycache__/module.pyc"), false);
  assert.equal(shouldCopyPythonAsset("/app/backend/module.pyo"), false);
});
