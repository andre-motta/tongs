import assert from "node:assert/strict";
import { describe, it } from "node:test";

describe("desktop report sample", () => {
  it("passes a direct assertion", () => {
    assert.equal(2 + 2, 4);
  });

  describe("nested group", () => {
    it("passes a nested assertion", () => {
      assert.deepEqual(["safe"], ["safe"]);
    });
  });
});
