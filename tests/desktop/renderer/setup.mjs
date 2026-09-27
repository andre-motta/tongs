// Shared Testing Library configuration for the desktop renderer tests.
//
// Loaded before every test file with `node --test --import`, from the desktop
// `npm test` script and the TAP step of the desktop production workflow.
//
// This module must not load the Testing Library entry points: `screen` binds
// to `document.body` and React DOM probes `window` when they are first
// loaded, and each test file installs its own jsdom globals only after this
// runs. It therefore configures the shared `config` module that
// `@testing-library/react` itself resolves, which keeps a single instance.
import { createRequire } from "node:module";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const reactRequire = createRequire(
  desktopRequire.resolve("@testing-library/react"),
);
const { configure, getConfig } = reactRequire(
  "@testing-library/dom/dist/config.js",
);

// Busy CI runners can take more than the default 1000 ms to settle a render.
export const ASYNC_UTIL_TIMEOUT_MS = 5000;

configure({
  asyncUtilTimeout: ASYNC_UTIL_TIMEOUT_MS,
  // The default message appends a pretty-printed DOM, which is long and can
  // walk large jsdom trees. Keep only the query's own message.
  getElementError(message) {
    const error = new Error(message || "Testing Library query failed");
    error.name = "TestingLibraryElementError";
    return error;
  },
});

if (getConfig().asyncUtilTimeout !== ASYNC_UTIL_TIMEOUT_MS) {
  throw new Error("renderer test setup did not configure Testing Library");
}
