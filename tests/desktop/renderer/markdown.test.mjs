import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

import {
  SafeMarkdown,
  admitMarkdownExternalUrl,
} from "../../../desktop/dist/src/renderer/core/safe-markdown.js";

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
const { cleanup, fireEvent, render, waitFor } = desktopRequire(
  "@testing-library/react",
);
afterEach(cleanup);

function renderMarkdown(source, openExternal = async () => true) {
  return render(
    React.createElement(SafeMarkdown, { source, openExternal }),
  );
}

test("renders CommonMark and GFM structure while preserving code text", () => {
  const source = [
    "# Heading",
    "",
    "Text with *emphasis*, **strength**, ~~deletion~~, and `inline code`.",
    "",
    "> quoted",
    "",
    "- first",
    "  - nested",
    "- [x] complete",
    "- [ ] pending",
    "",
    "| A | B |",
    "| - | - |",
    "| α | β |",
    "",
    "````suggestion:-1+2",
    "const marker = ```;",
    "  indented",
    "````",
    "",
    "~~~text",
    "tilde fence",
    "~~~",
    "",
    "Reference[^1]",
    "",
    "[^1]: Footnote text",
  ].join("\n");
  const view = renderMarkdown(source);

  assert.equal(view.getByRole("heading", { level: 1 }).textContent, "Heading");
  assert.equal(view.container.querySelector("em")?.textContent, "emphasis");
  assert.equal(view.container.querySelector("strong")?.textContent, "strength");
  assert.equal(view.container.querySelector("del")?.textContent, "deletion");
  assert.equal(
    view.container.querySelector("p code")?.textContent,
    "inline code",
  );
  assert.equal(
    view.container.querySelector("blockquote")?.textContent.trim(),
    "quoted",
  );
  assert.equal(view.container.querySelectorAll("ul ul").length, 1);
  assert.equal(
    view.container.querySelectorAll('input[type="checkbox"]:disabled').length,
    2,
  );
  assert.equal(view.container.querySelector("table td")?.textContent, "α");
  assert.match(view.container.querySelector("section")?.textContent ?? "", /Footnote text/);
  const blocks = [...view.container.querySelectorAll("pre code")];
  assert.equal(blocks[0]?.textContent, "const marker = ```;\n  indented\n");
  assert.match(blocks[0]?.className ?? "", /language-suggestion:-1\+2/);
  assert.equal(blocks[1]?.textContent, "tilde fence\n");
});

test("keeps raw HTML inert and replaces every Markdown image with text", () => {
  const calls = [];
  const view = renderMarkdown(
    [
      '<script>window.pwned = true</script><img src="https://bad.invalid/raw.png">',
      "",
      "![diagram](https://bad.invalid/image.png)",
      "![embedded](data:image/png;base64,AAAA)",
    ].join("\n"),
    async (url) => {
      calls.push(url);
      return true;
    },
  );

  assert.equal(
    view.container.querySelector(
      "script, img, iframe, object, embed, style, form",
    ),
    null,
  );
  assert.match(
    view.container.textContent,
    /<script>window\.pwned = true<\/script>/,
  );
  assert.match(
    view.container.textContent,
    /<img src="https:\/\/bad\.invalid\/raw\.png">/,
  );
  assert.match(view.container.textContent, /\[Image: diagram\]/);
  assert.match(view.container.textContent, /\[Image: embedded\]/);
  assert.deepEqual(calls, []);
});

test("admits only absolute credential-free HTTPS URLs without ambiguity", () => {
  assert.equal(
    admitMarkdownExternalUrl("HTTPS://Example.COM/a?b=1"),
    "https://example.com/a?b=1",
  );
  for (const value of [
    "http://example.com",
    "javascript:alert(1)",
    "data:text/plain,x",
    "file:///tmp/x",
    "mailto:test@example.com",
    "//example.com/path",
    "/relative",
    "#fragment",
    "https://user@example.com",
    "https://user:pass@example.com",
    "https://example.com/has space",
    "https://example.com/line\nbreak",
    `https://example.com/${"x".repeat(4090)}`,
  ]) {
    assert.equal(admitMarkdownExternalUrl(value), null, value);
  }
});

test("opens an admitted link only on activation and reports bridge refusal", async () => {
  const calls = [];
  const view = renderMarkdown(
    "[safe](HTTPS://Example.COM/path) [relative](/local) [fragment](#note)",
    async (url) => {
      calls.push(url);
      return false;
    },
  );
  const link = view.getByRole("link", { name: "safe" });
  fireEvent.focus(link);
  fireEvent.mouseOver(link);
  assert.deepEqual(calls, []);
  assert.equal(view.queryByRole("link", { name: "relative" }), null);
  assert.equal(view.queryByRole("link", { name: "fragment" }), null);

  fireEvent.click(link);
  await waitFor(() => assert.deepEqual(calls, ["https://example.com/path"]));
  assert.equal(
    (await view.findByRole("status")).textContent,
    "Could not open link.",
  );
  assert.equal(view.container.querySelector("a[href]"), null);
});

test("uses bounded plain-text fallbacks without parsing truncated Markdown", () => {
  const oversized = `${"😀".repeat(1024)}${"x".repeat(5000)}`;
  const inputView = renderMarkdown(oversized);
  assert.match(inputView.getByRole("status").textContent, /safe input limit/);
  assert.equal(
    [...inputView.container.querySelector("pre").textContent].length,
    4096,
  );
  assert.match(
    inputView.getByRole("status").textContent,
    /Additional content was omitted/,
  );
  cleanup();

  const pathologicalDelimiters = `${"[".repeat(32_768)}${"]".repeat(32_768)}`;
  const delimiterView = renderMarkdown(pathologicalDelimiters);
  assert.match(
    delimiterView.getByRole("status").textContent,
    /safe input limit/,
  );
  cleanup();

  const boundaryView = renderMarkdown("x".repeat(4 * 1024));
  assert.equal(boundaryView.queryByRole("status"), null);
  cleanup();

  const overBoundaryView = renderMarkdown("x".repeat(4 * 1024 + 1));
  assert.match(
    overBoundaryView.getByRole("status").textContent,
    /safe input limit/,
  );
  cleanup();

  const deeplyNested = `${"> ".repeat(34)}deep`;
  const depthView = renderMarkdown(deeplyNested);
  assert.match(depthView.getByRole("status").textContent, /structure exceeds/);
});

test("handles Unicode, CRLF, trailing breaks, empty code, and incomplete Markdown", () => {
  const view = renderMarkdown(
    "Unicode café 中文\r\nline break  \r\nnext\r\n\r\n```\r\n```\r\n\r\n**incomplete",
  );
  assert.match(view.container.textContent, /Unicode café 中文/);
  assert.equal(view.container.querySelectorAll("br").length, 1);
  assert.equal(view.container.querySelector("pre code")?.textContent, "");
  assert.match(view.container.textContent, /\*\*incomplete/);
  assert.equal(view.queryByRole("status"), null);
});
