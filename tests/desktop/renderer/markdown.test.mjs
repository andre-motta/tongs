import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";

import {
  SafeMarkdown,
  admitMarkdownExternalUrl,
  externalLinkHost,
  linkTextNamesOtherOrigin,
  safeMarkdownPresentationBytes,
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
    view.container.querySelectorAll(
      "script, img, iframe, object, embed, style, form",
    ).length,
    0,
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
  assert.equal(view.queryAllByRole("link", { name: "relative" }).length, 0);
  assert.equal(view.queryAllByRole("link", { name: "fragment" }).length, 0);

  fireEvent.click(link);
  await waitFor(() => assert.deepEqual(calls, ["https://example.com/path"]));
  assert.equal(
    (await view.findByRole("status")).textContent,
    "Could not open link.",
  );
  assert.equal(view.container.querySelectorAll("a[href]").length, 0);
});

test("exposes every link destination as a title and a described host", () => {
  const view = renderMarkdown("[docs](https://Docs.Example.com:8443/guide?q=1)");
  const link = view.getByRole("link", { name: "docs" });
  assert.equal(link.getAttribute("title"), "https://docs.example.com:8443/guide?q=1");
  assert.equal(link.getAttribute("data-link-host"), "docs.example.com:8443");
  const describedBy = link.getAttribute("aria-describedby") ?? "";
  assert.notEqual(describedBy, "");
  const description = view.container.ownerDocument.getElementById(describedBy);
  assert.equal(description?.textContent, "Opens docs.example.com:8443");
  assert.equal(description?.hasAttribute("hidden"), true);
  assert.equal(view.container.textContent.includes("(opens"), false);
});

test("shows the real host beside link text that names a different URL", async () => {
  const calls = [];
  const view = renderMarkdown(
    [
      "[https://github.com/org/repo](https://evil.example/phish)",
      "[github.com/org/repo](https://evil.example/other)",
      "[**https://gitlab.com/x**](https://xn--gthub-cta.com/y)",
      "[https://github.com/org/repo](https://github.com/org/repo/pulls)",
      "[see the pipeline](https://gitlab.com/org/repo/-/pipelines)",
    ].join("\n\n"),
    async (url) => {
      calls.push(url);
      return true;
    },
  );
  const links = view.getAllByRole("link");
  assert.equal(links.length, 5);
  const described = links.map((link) => {
    const id = link.getAttribute("aria-describedby") ?? "";
    const element = view.container.ownerDocument.getElementById(id);
    return {
      name: link.textContent,
      title: link.getAttribute("title"),
      description: element?.textContent ?? "",
      hidden: element?.hasAttribute("hidden") ?? null,
    };
  });
  assert.deepEqual(described, [
    {
      name: "https://github.com/org/repo",
      title: "https://evil.example/phish",
      description: "(opens evil.example)",
      hidden: false,
    },
    {
      name: "github.com/org/repo",
      title: "https://evil.example/other",
      description: "(opens evil.example)",
      hidden: false,
    },
    {
      name: "https://gitlab.com/x",
      title: "https://xn--gthub-cta.com/y",
      description: "(opens xn--gthub-cta.com)",
      hidden: false,
    },
    {
      name: "https://github.com/org/repo",
      title: "https://github.com/org/repo/pulls",
      description: "Opens github.com",
      hidden: true,
    },
    {
      name: "see the pipeline",
      title: "https://gitlab.com/org/repo/-/pipelines",
      description: "Opens gitlab.com",
      hidden: true,
    },
  ]);
  assert.equal(
    view.container.querySelectorAll(".safe-markdown-link-host-mismatch").length,
    3,
  );
  assert.deepEqual(calls, []);

  fireEvent.click(links[0]);
  await waitFor(() => assert.deepEqual(calls, ["https://evil.example/phish"]));
});

test("shows the real host beside lookalike link text without a scheme", () => {
  const view = renderMarkdown(
    [
      "[git\u00adhub.com/org/repo](https://evil.example/x)",
      "[g\u0456thub.com/org](https://evil.example/y)",
      "[github\u3002com/x](https://evil.example/z)",
    ].join("\n\n"),
  );
  const links = view.getAllByRole("link");
  assert.equal(links.length, 3);
  const described = links.map((link) => {
    const id = link.getAttribute("aria-describedby") ?? "";
    const element = view.container.ownerDocument.getElementById(id);
    return {
      title: link.getAttribute("title"),
      description: element?.textContent ?? "",
      hidden: element?.hasAttribute("hidden") ?? null,
    };
  });
  assert.deepEqual(described, [
    { title: "https://evil.example/x", description: "(opens evil.example)", hidden: false },
    { title: "https://evil.example/y", description: "(opens evil.example)", hidden: false },
    { title: "https://evil.example/z", description: "(opens evil.example)", hidden: false },
  ]);
  assert.equal(
    view.container.querySelectorAll(".safe-markdown-link-host-mismatch").length,
    3,
  );
});

test("flags a URL among other words and leaves bare file names unmarked", () => {
  const view = renderMarkdown(
    [
      "[see https://github.com/org/repo](https://evil.example/)",
      "[github.com/org/repo (docs)](https://evil.example/)",
      "[setup.py](https://github.com/org/repo/blob/main/setup.py)",
      "[README.md](https://github.com/org/repo/blob/main/README.md)",
    ].join("\n\n"),
  );
  const links = view.getAllByRole("link");
  assert.equal(links.length, 4);
  const described = links.map((link) => {
    const id = link.getAttribute("aria-describedby") ?? "";
    const element = view.container.ownerDocument.getElementById(id);
    return {
      title: link.getAttribute("title"),
      description: element?.textContent ?? "",
      hidden: element?.hasAttribute("hidden") ?? null,
    };
  });
  assert.deepEqual(described, [
    { title: "https://evil.example/", description: "(opens evil.example)", hidden: false },
    { title: "https://evil.example/", description: "(opens evil.example)", hidden: false },
    {
      title: "https://github.com/org/repo/blob/main/setup.py",
      description: "Opens github.com",
      hidden: true,
    },
    {
      title: "https://github.com/org/repo/blob/main/README.md",
      description: "Opens github.com",
      hidden: true,
    },
  ]);
  assert.equal(
    view.container.querySelectorAll(".safe-markdown-link-host-mismatch").length,
    2,
  );
});

test("fails closed on padded long link text and reads image alt text", () => {
  const padded = `github.com${" ".repeat(2100)}x`;
  const view = renderMarkdown(
    [
      `[${padded}](https://evil.example/long)`,
      "[![https://github.com/org/repo](https://github.com/logo.png)](https://evil.example/img)",
      "[![project logo](https://github.com/logo.png)](https://evil.example/logo)",
      "[github.com.](https://evil.example/dot)",
      "[http://github.com/x](https://github.com/x)",
    ].join("\n\n"),
  );
  const links = view.getAllByRole("link");
  assert.equal(links.length, 5);
  const described = links.map((link) => {
    const id = link.getAttribute("aria-describedby") ?? "";
    const element = view.container.ownerDocument.getElementById(id);
    return {
      title: link.getAttribute("title"),
      description: element?.textContent ?? "",
      hidden: element?.hasAttribute("hidden") ?? null,
    };
  });
  assert.deepEqual(described, [
    { title: "https://evil.example/long", description: "(opens evil.example)", hidden: false },
    { title: "https://evil.example/img", description: "(opens evil.example)", hidden: false },
    { title: "https://evil.example/logo", description: "Opens evil.example", hidden: true },
    { title: "https://evil.example/dot", description: "(opens evil.example)", hidden: false },
    { title: "https://github.com/x", description: "Opens github.com", hidden: true },
  ]);
  assert.equal(links[1].textContent, "[Image: https://github.com/org/repo]");
  assert.equal(
    view.container.querySelectorAll(".safe-markdown-link-host-mismatch").length,
    3,
  );
});

test("leaves file names, file references, and code identifiers unmarked", () => {
  const view = renderMarkdown(
    [
      "[setup.py:42](https://evil.example/a)",
      "[README.md#install](https://evil.example/b)",
      "[app.tsx#L10](https://evil.example/c)",
      "[tongs.config.toml](https://evil.example/d)",
      "[vite.config.ts](https://evil.example/e)",
      "[foo.bar.baz](https://evil.example/f)",
      "[evil.dev](https://github.com/g)",
      "[docs.github.io](https://evil.example/h)",
      "[github.com:8443](https://github.com/i)",
    ].join("\n\n"),
  );
  const links = view.getAllByRole("link");
  assert.equal(links.length, 9);
  const descriptions = links.map((link) => {
    const id = link.getAttribute("aria-describedby") ?? "";
    return view.container.ownerDocument.getElementById(id)?.textContent ?? "";
  });
  assert.deepEqual(descriptions, [
    "Opens evil.example",
    "Opens evil.example",
    "Opens evil.example",
    "Opens evil.example",
    "Opens evil.example",
    "Opens evil.example",
    "(opens github.com)",
    "(opens evil.example)",
    "(opens github.com)",
  ]);
  assert.equal(
    view.container.querySelectorAll(".safe-markdown-link-host-mismatch").length,
    3,
  );
});

test("compares link text to the destination by host", () => {
  assert.equal(externalLinkHost("https://example.com/a"), "example.com");
  assert.equal(externalLinkHost("https://b\u00fccher.example/"), "xn--bcher-kva.example");
  const cases = [
    ["https://github.com/a", "https://github.com/b", false],
    ["HTTPS://GitHub.com/a", "https://github.com/b", false],
    ["https://github.com:444/a", "https://github.com/a", true],
    ["http://github.com/a", "https://github.com/a", false],
    ["http://github.com/x", "https://github.com/x", false],
    ["http://github.com:443/x", "https://github.com/x", true],
    ["https://github.com.evil.example/a", "https://github.com/a", true],
    ["github.com", "https://github.com/", false],
    ["github.com/a", "https://gitlab.com/a", true],
    ["  https://evil.example  ", "https://github.com/", true],
    ["click here", "https://evil.example/", false],
    ["v1.0.2", "https://github.com/", false],
    ["README.md", "https://github.com/", false],
    ["setup.py", "https://github.com/org/repo/blob/main/setup.py", false],
    ["notes.txt", "https://evil.example/", false],
    ["setup.py/", "https://evil.example/", false],
    ["setup.py:42", "https://evil.example/", false],
    ["README.md#install", "https://evil.example/", false],
    ["app.tsx#L10", "https://evil.example/", false],
    ["tongs.config.toml", "https://evil.example/", false],
    ["vite.config.ts", "https://evil.example/", false],
    ["foo.bar.baz", "https://evil.example/", false],
    ["see foo.bar.baz in vite.config.ts", "https://evil.example/", false],
    ["me@corp.internal", "https://evil.example/", false],
    ["github.com/org/repo", "https://evil.example/", true],
    ["github.com.", "https://evil.example/", true],
    ["github.com,", "https://evil.example/", true],
    ["(github.com)", "https://evil.example/", true],
    ["github.com.", "https://github.com/", false],
    ["docs.xn--gthub-cta.com", "https://github.com/", true],
    ["evil.xn--p1ai", "https://github.com/", true],
    ["docs.github.io", "https://evil.example/", true],
    ["evil.dev", "https://github.com/", true],
    ["github.com:8443", "https://github.com/", true],
    ["see https://github.com/org/repo", "https://evil.example/", true],
    ["see https://github.com/org/repo", "https://github.com/org/repo", false],
    ["github.com/org/repo (docs)", "https://evil.example/", true],
    ["the docs (github.com/org/repo).", "https://evil.example/", true],
    ["open setup.py and README.md", "https://evil.example/", false],
    ["e.g. the pipeline", "https://github.com/", false],
    ["", "https://github.com/", false],
    ["https://a b", "https://github.com/", true],
    ["git\u00adhub.com/org", "https://evil.example/x", true],
    ["git\u00adhub.com/org", "https://github.com/org", false],
    ["g\u0456thub.com/org/repo", "https://evil.example/x", true],
    ["github\u3002com/org/repo", "https://evil.example/x", true],
    ["github.com\u200b/org", "https://evil.example/x", true],
    ["github.c\u043em/org", "https://evil.example/x", true],
    ["github\uFF0Ecom", "https://github.com/", false],
    ["b\u00fccher.example/a", "https://xn--bcher-kva.example/a", false],
    ["github.com@evil.example/x", "https://evil.example/x", true],
    ["1.0.2", "https://github.com/", false],
    ["e.g.", "https://github.com/", false],
    ["github.com%/org", "https://evil.example/x", true],
    ["setup.py:abc", "https://evil.example/x", false],
    [`github.com${" ".repeat(2100)}x`, "https://evil.example/", true],
  ];
  for (const [text, destination, expected] of cases) {
    assert.equal(
      linkTextNamesOtherOrigin(text, destination),
      expected,
      `${text} -> ${destination}`,
    );
  }
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
  assert.equal(boundaryView.queryAllByRole("status").length, 0);
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

test("reports the exact source or preview allocation used by aggregate budgets", () => {
  assert.equal(safeMarkdownPresentationBytes("x".repeat(4096)), 4096);
  assert.equal(safeMarkdownPresentationBytes("😀".repeat(1024)), 4096);
  assert.equal(safeMarkdownPresentationBytes("x".repeat(5000)), 4096);
  assert.equal(safeMarkdownPresentationBytes("😀".repeat(4096)), 16_384);
});

test("handles Unicode, CRLF, trailing breaks, empty code, and incomplete Markdown", () => {
  const view = renderMarkdown(
    "Unicode café 中文\r\nline break  \r\nnext\r\n\r\n```\r\n```\r\n\r\n**incomplete",
  );
  assert.match(view.container.textContent, /Unicode café 中文/);
  assert.equal(view.container.querySelectorAll("br").length, 1);
  assert.equal(view.container.querySelector("pre code")?.textContent, "");
  assert.match(view.container.textContent, /\*\*incomplete/);
  assert.equal(view.queryAllByRole("status").length, 0);
});
