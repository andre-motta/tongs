import { describe, expect, it } from "vitest";
import { DIFF_OVERSCAN, DIFF_ROW_HEIGHT, getDiffWindow } from "../src/virtualDiff";

describe("bounded diff window", () => {
  it("keeps the scroll canvas at the complete diff height", () => {
    const result = getDiffWindow(20_000, 0, 460);

    expect(result.height).toBe(20_000 * DIFF_ROW_HEIGHT);
    expect(result.start).toBe(0);
    expect(result.end).toBeGreaterThan(16);
    expect(result.end).toBeLessThan(20_000);
  });

  it("moves a small overscanned window around the requested viewport", () => {
    const result = getDiffWindow(20_000, 10_000 * DIFF_ROW_HEIGHT, 460);

    expect(result.start).toBe(10_000 - DIFF_OVERSCAN);
    expect(result.end).toBeLessThan(10_000 + 30);
    expect(result.end - result.start).toBeLessThan(40);
    expect(result.top).toBe(result.start * DIFF_ROW_HEIGHT);
  });

  it("clamps the final window to the end of a short diff", () => {
    const result = getDiffWindow(80, 79 * DIFF_ROW_HEIGHT, 460);

    expect(result.start).toBe(54);
    expect(result.end).toBe(80);
  });

  it("does not invert the window when a large diff is replaced by a short one", () => {
    const result = getDiffWindow(80, 20_000 * DIFF_ROW_HEIGHT, 460);

    expect(result.start).toBeLessThan(result.end);
    expect(result.end).toBe(80);
  });
});
