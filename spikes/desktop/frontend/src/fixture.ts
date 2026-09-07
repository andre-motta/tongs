import type { Bridge, Diff, PluginRecord, Review } from "./types";

export const fixtureReviews: Review[] = [
  {
    id: "normal",
    number: 42,
    title: "Improve review navigation",
    author: "sam",
    repo: "acme/review-tools",
    forge: "GitHub",
    status: "Passing",
  },
  {
    id: "large",
    number: 84,
    title: "Large generated diff (20,000 lines)",
    author: "alex",
    repo: "acme/platform",
    forge: "GitLab",
    status: "Running",
  },
];

export const fixturePlugins: PluginRecord[] = [];

export function createFixtureDiff(id: string): Diff {
  const count = id === "large" ? 20_000 : 80;
  let oldLine = 0;
  let newLine = 0;
  const lines = Array.from({ length: count }, (_, index) => {
    const kind = index % 9 === 3 ? "deletion" : index % 9 === 4 ? "addition" : "context";
    if (kind !== "addition") oldLine += 1;
    if (kind !== "deletion") newLine += 1;
    return {
      old_line: kind === "addition" ? null : oldLine,
      new_line: kind === "deletion" ? null : newLine,
      kind: kind as "context" | "addition" | "deletion",
      text: `    render_review_item(${index}, status='ready')`,
    };
  });
  return { path: "src/review/workspace.py", lines };
}

export const fixtureBridge: Bridge = {
  async invoke(method, params = {}) {
    if (method === "health") return { fixture: true, protocol: "prototype-1" };
    if (method === "list_reviews") return fixtureReviews;
    if (method === "get_diff" && typeof params.id === "string") return createFixtureDiff(params.id);
    if (method === "list_plugins") return fixturePlugins;
    throw new Error(`Fixture method unavailable: ${method}`);
  },
};
