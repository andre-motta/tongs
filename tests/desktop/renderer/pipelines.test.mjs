import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test, { afterEach } from "node:test";
import { QueryCoordinator } from "../../../desktop/dist/src/renderer/core/query.js";
import {
  classifyMutationFailure,
  createPipelinesFeature,
  loadLogPages,
  sanitizeLogText,
} from "../../../desktop/dist/src/renderer/features/pipelines/index.js";
import { encodeReadFailure } from "../../../desktop/dist/src/shared/bridge.js";

const desktopRequire = createRequire(
  new URL("../../../desktop/package.json", import.meta.url),
);
const { JSDOM } = desktopRequire("jsdom");
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://app.invalid/",
});
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  HTMLInputElement: dom.window.HTMLInputElement,
  Node: dom.window.Node,
});
const { cleanup, fireEvent, render, waitFor } = desktopRequire(
  "@testing-library/react",
);
afterEach(cleanup);

test("initial pipeline failure does not claim that the review has no pipelines", async () => {
  const bridge = baseBridge({
    listReviewPipelines: () =>
      read(
        Promise.reject({
          code: "service_error",
          message: "safe pipeline failure",
          retryable: false,
        }),
      ),
  });
  const view = renderFeature(bridge);

  await view.findByText(
    /The local service could not complete this read: safe (pipeline|job) failure/,
  );
  assert.equal(
    view.queryByText("No pipelines are available for this review."),
    null,
  );
});

test("initial job failure does not claim that the pipeline has no jobs", async () => {
  const bridge = baseBridge({
    listJobs: () =>
      read(
        Promise.reject({
          code: "service_error",
          message: "safe job failure",
          retryable: false,
        }),
      ),
  });
  const view = renderFeature(bridge);

  await view.findByText(
    /The local service could not complete this read: safe (pipeline|job) failure/,
  );
  assert.equal(view.queryByText("This pipeline has no jobs."), null);
});

test("failed pipeline refresh preserves an empty state from a successful read", async () => {
  let reads = 0;
  const bridge = baseBridge({
    listReviewPipelines: () =>
      read(
        reads++ === 0
          ? { pipelines: [] }
          : Promise.reject(new Error("refresh failed")),
      ),
  });
  const view = renderFeature(bridge);

  await view.findByText("No pipelines are available for this review.");
  fireEvent.click(view.getByRole("button", { name: "Refresh CI" }));
  await view.findByText("Refresh failed. Showing the previous pipeline list.");
  assert.ok(view.getByText("No pipelines are available for this review."));
});

test("failed job refresh preserves an empty state from a successful read", async () => {
  let reads = 0;
  const bridge = baseBridge({
    listJobs: () =>
      read(
        reads++ === 0
          ? { jobs: [] }
          : Promise.reject(new Error("refresh failed")),
      ),
  });
  const view = renderFeature(bridge);

  await view.findByText("This pipeline has no jobs.");
  fireEvent.click(view.getByRole("button", { name: "Refresh jobs" }));
  await view.findByText("Refresh failed. Showing previous jobs.");
  assert.ok(view.getByText("This pipeline has no jobs."));
});

test("pipeline panel renders hierarchy, inert paged logs, search, and keyboard selection", async () => {
  const jobReads = [];
  const bridge = baseBridge({
    listReviewPipelines: () => read({ pipelines: [pipeline(101), pipeline(102)] }),
    listJobs: (handle) => {
      jobReads.push(handle);
      return read({ jobs: [job(handle === "pipeline-101" ? 201 : 202)] });
    },
    openLog: ({ job: handle }) =>
      read(logPage(handle, 0, 1, "start\n\u001b[31mFA")),
    pageLog: ({ resource, cursor }) =>
      read(logPage(resource, cursor, null, "IL\u001b[0m\n<script>alert(1)</script>\n")),
  });
  const view = renderFeature(bridge);

  await view.findByText("build-201");
  assert.equal(view.container.querySelector("script"), null);
  assert.ok(await view.findByText("<script>alert(1)</script>"));
  assert.deepEqual(
    [...view.container.querySelectorAll(".ci-log-lines code")].map(
      (element) => element.textContent,
    ),
    ["1start\n", "2FAIL\n", "3<script>alert(1)</script>\n"],
  );

  const search = view.getByRole("searchbox", { name: "Search log" });
  fireEvent.change(search, { target: { value: "fail" } });
  // The filter itself is a synchronous useMemo over React state, but a slow
  // runner can still observe the pre-change render before the commit lands,
  // so wait for the status text rather than reading it right after the event.
  await waitFor(() => {
    assert.equal(
      view.container.querySelector(".ci-match-count").textContent,
      "1 of 1 matches",
    );
  });
  search.blur();
  fireEvent.keyDown(document.body, { key: "/" });
  assert.equal(document.activeElement, search);

  const pipelines = view.getByRole("navigation", { name: "Review pipelines" });
  const first = view.getByRole("button", { name: /Pipeline #101/ });
  first.focus();
  fireEvent.keyDown(pipelines, { key: "ArrowDown" });
  await view.findByText("build-202");
  assert.equal(document.activeElement.textContent.includes("Pipeline #102"), true);
  assert.deepEqual(jobReads, ["pipeline-101", "pipeline-102"]);
});

test("selected pipeline and job open only their service DTO forge URLs", async () => {
  const opened = [];
  const bridge = baseBridge({
    openExternal: async (url) => {
      opened.push(url);
      return true;
    },
  });
  const view = renderFeature(bridge);
  await view.findByText("build-201");

  fireEvent.click(view.getByRole("button", { name: "Open pipeline on forge" }));
  await view.findByText("Pipeline link sent to your browser.");
  fireEvent.click(view.getByRole("button", { name: "Open job on forge" }));
  await view.findByText("Job link sent to your browser.");

  assert.deepEqual(opened, [
    "https://example.invalid/pipeline/101",
    "https://example.invalid/job/201",
  ]);
});

test("loaded selected job opens in editor once and reports exact outcome", async () => {
  const jobs = [];
  const completion = deferred();
  const bridge = baseBridge({
    openJobLogInEditor: async (handle) => {
      jobs.push(handle);
      return completion.promise;
    },
  });
  const view = renderFeature(bridge);
  await view.findByText("safe output");
  const open = view.getByRole("button", { name: "Open log in editor" });

  fireEvent.click(open);
  fireEvent.click(open);
  assert.equal(jobs.length, 1);
  completion.resolve({
    outcome: "started",
    message: "Editor started. Tongs cannot confirm that the exported log was opened.",
  });

  await view.findByText(/Tongs cannot confirm/);
  assert.deepEqual(jobs, ["job-201"]);
});

test("editor and forge launch failures are actionable", async () => {
  const bridge = baseBridge({
    openExternal: async () => {
      throw new Error("denied");
    },
    openJobLogInEditor: async () => ({
      outcome: "terminal_unsupported",
      message: "Configure a wait-capable graphical editor.",
    }),
  });
  const view = renderFeature(bridge);
  await view.findByText("safe output");

  fireEvent.click(view.getByRole("button", { name: "Open pipeline on forge" }));
  await view.findByText(/Pipeline could not be opened/);
  fireEvent.click(view.getByRole("button", { name: "Open log in editor" }));
  await view.findByText("Configure a wait-capable graphical editor.");
});

test("confirmed job action sends one exact opaque target and keeps its receipt", async () => {
  const writes = [];
  const completion = deferred();
  const bridge = baseBridge({
    retryJob: async (params) => {
      writes.push(params);
      await completion.promise;
      return receipt(params.operation_id, "retry_job", "known", null, false);
    },
  });
  const view = renderFeature(bridge);
  await view.findByText("build-201");
  await view.findByRole("button", { name: "Retry job" });

  fireEvent.click(view.getByRole("button", { name: "Retry job" }));
  const dialog = view.getByRole("alertdialog");
  assert.match(dialog.textContent, /job build-201 \(#201\) in pipeline #101/);
  const confirm = view.getByRole("button", { name: "Confirm retry job" });
  fireEvent.click(confirm);
  fireEvent.click(confirm);
  assert.equal(writes.length, 1);
  completion.resolve();

  await view.findByText("CI action accepted");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].pipeline, "pipeline-101");
  assert.equal(writes[0].job, "job-201");
  assert.match(writes[0].operation_id, /^desktop:retry_job:/);
  assert.match(view.container.querySelector(".ci-mutation-result").textContent, /Operation desktop:retry_job:/);
});

test("capabilities for a different repository cannot enable actions", async () => {
  const bridge = baseBridge({
    getCICapabilities: () =>
      read({
        repository: "other-repository",
        capabilities: {
          retry_pipeline: true,
          cancel_pipeline: true,
          retry_job: true,
          cancel_job: true,
        },
      }),
  });
  const view = renderFeature(bridge);
  await view.findByText("build-201");
  await view.findByRole("button", { name: "Retry job" });

  assert.ok(view.getByText(/did not match this repository/));
  assert.equal(view.getByRole("button", { name: "Retry pipeline" }).disabled, true);
  assert.equal(view.getByRole("button", { name: "Retry job" }).disabled, true);
});

test("unsupported actions explain the capability and never invoke a fallback", async () => {
  let writes = 0;
  const bridge = baseBridge({
    getCICapabilities: () =>
      read({
        repository: "repo",
        capabilities: {
          retry_pipeline: false,
          cancel_pipeline: true,
          retry_job: true,
          cancel_job: false,
        },
      }),
    retryPipeline: async () => {
      writes += 1;
      throw new Error("must not run");
    },
    cancelJob: async () => {
      writes += 1;
      throw new Error("must not run");
    },
  });
  const view = renderFeature(bridge);
  await view.findByText("build-201");
  await view.findByRole("button", { name: "Cancel job" });

  const retryPipeline = view.getByRole("button", { name: "Retry pipeline" });
  const cancelJob = view.getByRole("button", { name: "Cancel job" });
  assert.equal(retryPipeline.disabled, true);
  assert.equal(retryPipeline.title, "Retry pipeline is not supported by this forge.");
  assert.equal(cancelJob.disabled, true);
  assert.equal(cancelJob.title, "Cancel job is not supported by this forge.");
  fireEvent.click(retryPipeline);
  fireEvent.click(cancelJob);
  assert.equal(writes, 0);
});

test("post-dispatch timeout reconciles by receipt without replay", async () => {
  let writes = 0;
  let receiptReads = 0;
  const bridge = baseBridge({
    retryPipeline: async () => {
      writes += 1;
      throw {
        code: "mutation_timeout",
        message: "outcome unknown",
        retryable: true,
      };
    },
    getCIReceipt: () => {
      receiptReads += 1;
      return read({ receipt: null });
    },
  });
  const view = renderFeature(bridge);
  await view.findByText("build-201");

  fireEvent.click(view.getByRole("button", { name: "Retry pipeline" }));
  fireEvent.click(view.getByRole("button", { name: "Confirm retry pipeline" }));
  await view.findByText("CI action needs reconciliation");
  assert.equal(writes, 1);

  fireEvent.click(view.getByRole("button", { name: "Check retained receipt" }));
  await view.findByText(/No retained receipt is available in this session/);
  assert.equal(receiptReads, 1);
  assert.equal(writes, 1);
  assert.equal(view.getByRole("button", { name: "Retry pipeline" }).disabled, true);
});

test("rapid pipeline switching discards late job and log reads", async () => {
  const late = deferred();
  const cancelled = [];
  let firstReadStarted = false;
  const bridge = baseBridge({
    cancelRead: async (token) => {
      cancelled.push(token);
      return true;
    },
    listReviewPipelines: () => read({ pipelines: [pipeline(101), pipeline(102)] }),
    listJobs: (handle) => {
      if (handle === "pipeline-101") {
        firstReadStarted = true;
        return read(late.promise, "late-jobs");
      }
      return read({ jobs: [job(202)] });
    },
  });
  const view = renderFeature(bridge);
  await view.findByRole("button", { name: /Pipeline #102/ });
  await waitFor(() => assert.equal(firstReadStarted, true));
  fireEvent.click(view.getByRole("button", { name: /Pipeline #102/ }));
  await view.findByText("build-202");
  late.resolve({ jobs: [job(201)] });
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(view.queryByText("build-201"), null);
  assert.ok(cancelled.includes("late-jobs"));
});

test("job log recovery advice survives a rejection from the main process", async () => {
  const failures = [
    ["snapshot_expired", "This log snapshot expired. Reload the job log to continue."],
    ["revision_changed", "The job log changed while loading. Reload the current log."],
  ];
  for (const [code, advice] of failures) {
    const bridge = baseBridge({
      openLog: () =>
        read(
          Promise.reject(
            new Error(
              `Error invoking remote method 'tongs:logs.open': Error: ${
                encodeReadFailure({
                  code,
                  message: "The resource snapshot is invalid or expired; refetch it.",
                  retryable: true,
                }).message
              }`,
            ),
          ),
        ),
    });

    const view = renderFeature(bridge);

    await view.findByText(advice);
    view.unmount();
  }
});

test("log reconstruction enforces identity, revision, line, and text bounds", async () => {
  const bridge = {
    cancelRead: async () => true,
    openLog: () => read(logPage("job", 0, 1, "one\r\n\u001b]0;title\u0007two\n")),
    pageLog: () =>
      read({
        ...logPage("job", 1, null, `${"x\n".repeat(50_001)}`),
        revision: { sha256: "hash", byte_count: 99 },
      }),
  };
  const loaded = await loadLogPages("job", bridge, new QueryCoordinator(bridge));
  assert.equal(loaded.lines[0], "one");
  assert.equal(loaded.lines[1], "two");
  assert.equal(loaded.lines.length, 50_000);
  assert.equal(loaded.partialError.code, "pagination_limit");
  assert.equal(sanitizeLogText("safe\u0000\u001b[31mred\u001b[0m"), "safe�red");

  const inconsistent = {
    cancelRead: async () => true,
    openLog: () => read(logPage("job", 0, 4, "a")),
    pageLog: () => read(logPage("job", 3, null, "b")),
  };
  await assert.rejects(
    loadLogPages("job", inconsistent, new QueryCoordinator(inconsistent)),
    (error) => error.code === "invalid_response",
  );
});

test("known pre-dispatch failures and ambiguous transport failures stay distinct", () => {
  assert.equal(classifyMutationFailure({ code: "invalid_handle" }), "known");
  assert.equal(classifyMutationFailure({ code: "mutation_timeout" }), "unknown");
  assert.equal(classifyMutationFailure({ code: "unexpected_eof" }), "unknown");
});

function renderFeature(bridge) {
  const feature = createPipelinesFeature();
  return render(feature.render(featureContext(bridge), reviewRoute()));
}

function featureContext(bridge) {
  return {
    bridge,
    queries: new QueryCoordinator(bridge),
    repositories: [],
    repositoriesReady: true,
    repositoryGeneration: 1,
    reviewPanels: [
      { id: "overview", label: "Overview", order: 10 },
      { id: "pipelines", label: "Pipelines", order: 40 },
    ],
    inlineAnchor: null,
    selectInlineAnchor: () => {},
    navigate: () => {},
  };
}

function reviewRoute() {
  return {
    kind: "review",
    panel: "pipelines",
    item: {
      handle: "review-47",
      repository: "repo",
      summary: {
        number: 47,
        title: "Desktop pipelines",
        author: { username: "andre", display_name: "Andre" },
        state: "open",
        is_draft: false,
        source_branch: "feature",
        target_branch: "feat/desktop-app",
        ci_status: "running",
        created_at: "2026-09-08T00:00:00Z",
        updated_at: "2026-09-08T00:00:00Z",
        web_url: "https://example.invalid/review/47",
        comment_count: 0,
        has_conflicts: false,
        labels: [],
        review_decision: null,
        additions: 1,
        deletions: 1,
      },
    },
  };
}

function pipeline(id) {
  return {
    handle: `pipeline-${id}`,
    value: {
      id,
      status: id === 101 ? "running" : "success",
      ref: `branch-${id}`,
      sha: `${id}`.repeat(20),
      web_url: `https://example.invalid/pipeline/${id}`,
      source: "push",
      created_at: "2026-09-08T00:00:00Z",
      finished_at: null,
      duration_seconds: null,
    },
  };
}

function job(id) {
  return {
    handle: `job-${id}`,
    value: {
      id,
      name: `build-${id}`,
      stage: "test",
      status: "running",
      web_url: `https://example.invalid/job/${id}`,
      started_at: "2026-09-08T00:00:00Z",
      finished_at: null,
      duration_seconds: null,
      allow_failure: false,
    },
  };
}

function logPage(resource, cursor, nextCursor, text) {
  return {
    snapshot_id: `snapshot-${resource}`,
    resource,
    revision: { sha256: "hash", byte_count: 99 },
    cursor,
    next_cursor: nextCursor,
    entries: [{ text }],
  };
}

function receipt(operationId, action, outcome, error, resyncRequired) {
  return {
    operation_id: operationId,
    action,
    outcome,
    error,
    resync_required: resyncRequired,
  };
}

function baseBridge(overrides = {}) {
  return {
    cancelRead: async () => true,
    onEvent: () => () => {},
    listReviewPipelines: () => read({ pipelines: [pipeline(101)] }),
    listJobs: () => read({ jobs: [job(201)] }),
    openLog: ({ job: handle }) => read(logPage(handle, 0, null, "safe output\n")),
    pageLog: () => {
      throw new Error("no next log page");
    },
    getCICapabilities: () =>
      read({
        repository: "repo",
        capabilities: {
          retry_pipeline: true,
          cancel_pipeline: true,
          retry_job: true,
          cancel_job: true,
        },
      }),
    retryPipeline: async (params) =>
      receipt(params.operation_id, "retry_pipeline", "known", null, false),
    cancelPipeline: async (params) =>
      receipt(params.operation_id, "cancel_pipeline", "known", null, false),
    retryJob: async (params) =>
      receipt(params.operation_id, "retry_job", "known", null, false),
    cancelJob: async (params) =>
      receipt(params.operation_id, "cancel_job", "known", null, false),
    getCIReceipt: () => read({ receipt: null }),
    openExternal: async () => true,
    openJobLogInEditor: async () => ({
      outcome: "started",
      message: "Editor started.",
    }),
    ...overrides,
  };
}

function read(value, requestToken = crypto.randomUUID()) {
  return { requestToken, result: Promise.resolve(value) };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
