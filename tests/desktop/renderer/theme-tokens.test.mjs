import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

/**
 * The #181 redesign shipped several panel-like surfaces (the review drawer,
 * the in-diff composer, pending cards, thread rows) whose background comes
 * entirely from `:root` custom properties: `:root` declares the dark palette
 * directly and a `@media (prefers-color-scheme: light)` block overrides the
 * same names, so a surface renders correctly in both schemes only if its own
 * rule spends a `var(--...)` token rather than a literal colour or an
 * undeclared token. #225 was exactly the second failure: the review drawer
 * read `background: var(--surface, #fff)`, `--surface` was never declared on
 * `:root` in either scheme, so the fallback `#fff` won unconditionally and
 * the drawer rendered light no matter what the OS theme was.
 *
 * Both checks below read the built stylesheet (`dist/shell/styles.css`,
 * copied byte for byte from `src/main/shell/styles.css` by `scripts/build.mjs`)
 * as plain text and reason about it with string matching only, the way the
 * rest of this suite avoids asserting on DOM nodes for styling: a rendered
 * node's computed style is not reliable in jsdom, so the CSS text itself is
 * the source of truth here.
 */

const stylesPath = path.resolve(
  import.meta.dirname,
  "../../../desktop/dist/shell/styles.css",
);
const css = readFileSync(stylesPath, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

/**
 * Every rule the stylesheet declares, as `{ selectorList, body }` pairs, with
 * `selectorList` split on commas and trimmed. Nesting (only `@media` nests in
 * this file) is handled by repeatedly pulling out the innermost `{...}`
 * block until none remain, which also drops every at-rule prelude (`@media
 * ...`) since only a plain selector list is ever recorded.
 */
function extractRules(source) {
  let text = source;
  const rules = [];
  let changed = true;
  while (changed) {
    changed = false;
    text = text.replace(/([^{}]*)\{([^{}]*)\}/g, (_match, prelude, body) => {
      changed = true;
      const selector = prelude.trim();
      if (selector.length > 0 && !selector.startsWith("@")) {
        rules.push({
          selectors: selector.split(",").map((part) => part.trim()),
          body,
        });
      }
      return "";
    });
  }
  return rules;
}

/**
 * Splits the stylesheet into the `@media (prefers-color-scheme: light)`
 * block's own body and everything else. Brace matched rather than regex
 * matched with a non-greedy body, because the light block itself contains a
 * `:root { ... }`, whose own closing brace a non-greedy `[^{}]*}` would stop
 * at instead of the media block's.
 */
function splitLightScheme(source) {
  const marker = /@media\s*\(\s*prefers-color-scheme\s*:\s*light\s*\)\s*\{/;
  const match = marker.exec(source);
  if (!match) return { light: "", rest: source };
  let depth = 1;
  let index = match.index + match[0].length;
  const bodyStart = index;
  while (index < source.length && depth > 0) {
    if (source[index] === "{") depth += 1;
    else if (source[index] === "}") depth -= 1;
    index += 1;
  }
  return {
    light: source.slice(bodyStart, index - 1),
    rest: source.slice(0, match.index) + source.slice(index),
  };
}

const rules = extractRules(css);
const { rest: baseOnlyCss } = splitLightScheme(css);
const baseRules = extractRules(baseOnlyCss);

/** Every rule body whose selector list names `selector` exactly. */
function bodiesFor(selector) {
  return rules
    .filter((rule) => rule.selectors.includes(selector))
    .map((rule) => rule.body);
}

const BACKGROUND_DECLARATION = /\bbackground(?:-color)?\s*:\s*[^;]*var\(--[a-zA-Z0-9-]+/;

/**
 * The redesigned panel-like surfaces from PRs #199-#208: the in-diff composer
 * (its overflow menu is a `.inline-composer-writes` group that nests inside
 * it and never carries its own background), the pending card and its stale
 * ribbon, a thread row (shared by both the diff surface and the Discussions
 * jump list, which renders the same `.review-workflow-thread` card), the
 * Overview general composer's page panel (`.panel`, which the composer wraps
 * itself in rather than duplicating), and the whole review drawer: the panel
 * itself, a pending entry card, a verdict tile, and the stale-entry ribbon.
 * This list is the contract: a selector added here without an explicit
 * `background: var(--...)` declaration fails the build instead of shipping a
 * surface that only happens to look right in whichever scheme someone last
 * tested it in.
 */
const THEMED_SURFACE_SELECTORS = [
  ".panel",
  ".inline-composer",
  ".inline-composer-preview",
  ".pending-card",
  ".pending-card-ribbon",
  ".diff-thread",
  ".review-workflow-thread",
  ".review-drawer",
  ".review-drawer-entry",
  ".review-drawer-verdict-tile",
  ".review-drawer-entry-ribbon",
];

for (const selector of THEMED_SURFACE_SELECTORS) {
  test(`${selector} declares a background from a token`, () => {
    const bodies = bodiesFor(selector);
    assert.ok(
      bodies.length > 0,
      `expected a rule for ${selector} in the built stylesheet`,
    );
    assert.ok(
      bodies.some((body) => BACKGROUND_DECLARATION.test(body)),
      `expected one rule for ${selector} to declare a background referencing a var(--...) token`,
    );
  });
}

/**
 * The `--surface` shape: a `var(--name, fallback)` whose `--name` is never
 * declared on `:root` in the default (dark) scheme, so the fallback wins
 * always and the declaration silently stops being theme-aware. `defined` is
 * built only from the base `:root { ... }` block, outside the
 * `prefers-color-scheme: light` media query, and deliberately not from the
 * light block's own `:root`: the light block only overrides values for names
 * the base already owns, it never introduces a name of its own, so a token
 * declared only inside it would still have no value in the default scheme
 * and must fail this check exactly as `--surface` should have.
 */
test("every var(--token) referenced in the stylesheet is declared on the base :root", () => {
  const defined = new Set();
  for (const rule of baseRules) {
    if (!rule.selectors.includes(":root")) continue;
    for (const match of rule.body.matchAll(/--([a-zA-Z0-9-]+)\s*:/g)) {
      defined.add(match[1]);
    }
  }
  const used = new Set();
  for (const match of css.matchAll(/var\(\s*--([a-zA-Z0-9-]+)/g)) {
    used.add(match[1]);
  }
  const undeclared = [...used].filter((name) => !defined.has(name)).sort();
  assert.deepEqual(
    undeclared,
    [],
    `var(--...) referenced a custom property never declared on the base :root: ${undeclared.join(", ")}`,
  );
});
