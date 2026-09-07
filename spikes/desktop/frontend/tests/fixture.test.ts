import { describe, expect, it } from "vitest";
import { createFixtureDiff, fixtureBridge, fixtureReviews } from "../src/fixture";
import { invoke } from "../src/bridge";

describe("browser fixture bridge", () => {
  it("matches the contract health and review shape", async () => {
    await expect(invoke("health", undefined, fixtureBridge)).resolves.toEqual({
      fixture: true,
      protocol: "prototype-1",
    });
    await expect(invoke("list_reviews", undefined, fixtureBridge)).resolves.toEqual(fixtureReviews);
  });

  it("generates a large fixture without changing the bridge protocol", () => {
    const diff = createFixtureDiff("large");
    expect(diff.path).toBe("src/review/workspace.py");
    expect(diff.lines).toHaveLength(20_000);
    expect(diff.lines.some((line) => line.kind === "addition" && line.old_line === null)).toBe(true);
    expect(diff.lines.some((line) => line.kind === "deletion" && line.new_line === null)).toBe(true);
  });

  it("keeps unknown calls isolated to the fixture bridge", async () => {
    await expect(invoke("plugin_invoke", { plugin: "unknown" }, fixtureBridge)).rejects.toThrow(
      "Fixture method unavailable",
    );
  });
});
