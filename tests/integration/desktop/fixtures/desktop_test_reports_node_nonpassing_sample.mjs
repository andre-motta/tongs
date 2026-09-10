import { test } from "node:test";

test("fails honestly", () => {
  throw new Error("controlled fixture failure");
});

test("skips honestly", { skip: "fixture skip" }, () => {});
test("todo honestly", { todo: "fixture todo" }, () => {});
test("cancelled honestly", async () => {
  await new Promise(() => {});
});
