import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { lstat, mkdtemp, readFile, readdir, readlink, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { WorkspaceUtilities } from "../../../desktop/dist/src/main/utilities.js";

class FakeTransport {
  constructor() {
    this.reads = [];
    this.mutations = [];
    this.reviewUrl = "https://github.com/acme/widgets/pull/7";
    this.editorPlan = {
      status: "ready",
      message: "ready",
      job: "job-handle",
      job_id: 31,
      argv: ["code", "--wait"],
      content: "safe log\n",
      slot: 1,
      token: "a".repeat(32),
      export_name: `tongs-slot-1-job-31-${"a".repeat(32)}.log`,
    };
  }
  requestRead(method, params) {
    this.reads.push([method, params]);
    const value = method === "utilities.review_url"
      ? { review: params.review, url: this.reviewUrl }
      : this.editorPlan;
    return { result: Promise.resolve(value) };
  }
  requestMutation(method, params) {
    this.mutations.push([method, params]);
    return {
      result: Promise.resolve(
        method === "utilities.job_log_release"
          ? { released: true }
          : { cleared: true },
      ),
    };
  }
}

class FakeChild extends EventEmitter {
  unrefCalls = 0;
  unref() { this.unrefCalls += 1; }
}

async function fixture(t) {
  const parent = await rmRoot();
  const root = path.join(parent, "exports");
  const transport = new FakeTransport();
  const clipboard = { values: [], writeText(value) { this.values.push(value); } };
  const launches = [];
  const children = [];
  const launch = (command, args) => {
    launches.push([command, args]);
    const child = new FakeChild();
    children.push(child);
    queueMicrotask(() => child.emit("spawn"));
    return child;
  };
  t.after(() => rm(parent, { recursive: true, force: true }));
  return { parent, root, transport, clipboard, launches, children, utility: new WorkspaceUtilities(transport, clipboard, root, launch) };
}

async function rmRoot() {
  return mkdtemp(path.join(os.tmpdir(), "tongs-utilities-"));
}

test("copy URL resolves an admitted review and writes only its validated HTTPS URL", async (t) => {
  const { utility, transport, clipboard } = await fixture(t);

  assert.deepEqual(await utility.copyReviewUrl("review-handle"), {
    outcome: "copied",
    message: "Review URL copied to the clipboard.",
  });
  assert.deepEqual(transport.reads, [["utilities.review_url", { review: "review-handle" }]]);
  assert.deepEqual(clipboard.values, ["https://github.com/acme/widgets/pull/7"]);

  transport.reviewUrl = "https://github.com";
  assert.equal((await utility.copyReviewUrl("review-handle")).outcome, "copied");
  assert.equal(clipboard.values.at(-1), "https://github.com");

  transport.reviewUrl = "https://token@github.com/acme/widgets/pull/7";
  assert.equal((await utility.copyReviewUrl("review-handle")).outcome, "failed");
  assert.equal(clipboard.values.length, 2);
});

test("clipboard failure is reported without exposing or changing the URL", async (t) => {
  const { root, transport } = await fixture(t);
  const clipboard = { writeText() { throw new Error("clipboard unavailable"); } };
  const utility = new WorkspaceUtilities(transport, clipboard, root, () => { throw new Error("unused"); });

  const result = await utility.copyReviewUrl("review-handle");

  assert.equal(result.outcome, "failed");
  assert.match(result.message, /clipboard access/);
  assert.deepEqual(transport.reads, [["utilities.review_url", { review: "review-handle" }]]);
});

test("asynchronous clipboard failure cannot be reported as copied", async (t) => {
  const { root, transport } = await fixture(t);
  const clipboard = {
    async writeText() {
      throw new Error("asynchronous clipboard unavailable");
    },
  };
  const utility = new WorkspaceUtilities(transport, clipboard, root, () => {
    throw new Error("unused");
  });

  const result = await utility.copyReviewUrl("review-handle");

  assert.equal(result.outcome, "failed");
  assert.match(result.message, /clipboard access/);
  assert.deepEqual(transport.reads, [
    ["utilities.review_url", { review: "review-handle" }],
  ]);
});

test("clear cache has no renderer-selected target", async (t) => {
  const { utility, transport } = await fixture(t);

  assert.equal((await utility.clearCache({})).outcome, "cleared");
  assert.deepEqual(transport.mutations, [["utilities.cache_clear", {}]]);
  assert.equal((await utility.clearCache({ draft: true })).outcome, "failed");
  assert.equal(transport.mutations.length, 1);
});

test("editor export uses exact job, private file, safe argv, and exit cleanup", async (t) => {
  const { utility, transport, root, launches, children } = await fixture(t);
  let allowRelease;
  let descriptorsAtRelease;
  const releaseGate = new Promise((resolve) => {
    allowRelease = resolve;
  });
  transport.requestMutation = (method, params) => {
    transport.mutations.push([method, params]);
    if (method !== "utilities.job_log_release") {
      return { result: Promise.resolve({ cleared: true }) };
    }
    descriptorsAtRelease = exportDescriptors(root);
    return { result: releaseGate.then(() => ({ released: true })) };
  };

  const result = await utility.openJobLogInEditor("job-handle");

  assert.equal(result.outcome, "started");
  assert.match(result.message, /cannot confirm/);
  assert.deepEqual(transport.reads, [["utilities.job_log_export", { job: "job-handle" }]]);
  assert.equal(launches.length, 1);
  assert.deepEqual(launches[0][0], "code");
  assert.equal(launches[0][1][0], "--wait");
  const exported = launches[0][1][1];
  assert.equal(path.dirname(exported), root);
  assert.equal(
    path.basename(exported),
    `tongs-slot-1-job-31-${"a".repeat(32)}.log`,
  );
  assert.equal(await readFile(exported, "utf8"), "safe log\n");
  assert.equal((await lstat(root)).mode & 0o777, 0o700);
  assert.equal((await lstat(exported)).mode & 0o777, 0o600);
  assert.equal(children[0].unrefCalls, 1);

  children[0].emit("exit", 0);
  while (descriptorsAtRelease === undefined) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.deepEqual(await descriptorsAtRelease, []);
  allowRelease();
  await waitForEmptyDirectory(root);
  assert.deepEqual(await readdir(root), []);
  assert.deepEqual(transport.mutations, [[
    "utilities.job_log_release",
    { slot: 1, token: "a".repeat(32) },
  ]]);
  await waitForNoExportDescriptors(root);
});

async function waitForEmptyDirectory(directory) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if ((await readdir(directory)).length === 0) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

async function exportDescriptors(directory) {
  if (process.platform !== "linux") return [];
  const descriptors = await readdir("/proc/self/fd");
  const targets = await Promise.all(
    descriptors.map(async (descriptor) =>
      readlink(`/proc/self/fd/${descriptor}`).catch(() => "")
    ),
  );
  return targets.filter((target) => target.startsWith(directory));
}

async function waitForNoExportDescriptors(directory) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if ((await exportDescriptors(directory)).length === 0) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.fail(`export descriptor remained open for ${directory}`);
}

test("duplicate editor launch returns busy without replaying the job read", async (t) => {
  const { utility, transport, root, children } = await fixture(t);
  let release;
  transport.requestRead = (method, params) => {
    transport.reads.push([method, params]);
    return { result: new Promise((resolve) => { release = () => resolve(transport.editorPlan); }) };
  };

  const first = utility.openJobLogInEditor("job-handle");
  while (transport.reads.length === 0) await new Promise((resolve) => setImmediate(resolve));
  assert.equal((await utility.openJobLogInEditor("job-handle")).outcome, "busy");
  assert.equal(transport.reads.length, 1);
  release();
  await first;
  children[0].emit("exit", 0);
  await waitForEmptyDirectory(root);
  await waitForNoExportDescriptors(root);
});

test("disabled editor outcome does not create a file or launch", async (t) => {
  const { utility, transport, root, launches } = await fixture(t);
  transport.editorPlan = {
    status: "disabled",
    message: "External editor access is disabled in Tongs configuration.",
    job: "job-handle",
    job_id: 31,
    argv: [],
    content: null,
    slot: null,
    token: null,
    export_name: null,
  };

  const result = await utility.openJobLogInEditor("job-handle");

  assert.equal(result.outcome, "disabled");
  assert.equal(launches.length, 0);
  assert.deepEqual(await readdir(root), []);
});

test("missing editor executable has an actionable failure and cleans the export", async (t) => {
  const { root, transport, clipboard } = await fixture(t);
  const launch = () => {
    const child = new FakeChild();
    queueMicrotask(() => child.emit("error", new Error("ENOENT")));
    return child;
  };
  const utility = new WorkspaceUtilities(transport, clipboard, root, launch);

  const result = await utility.openJobLogInEditor("job-handle");

  assert.equal(result.outcome, "failed");
  assert.match(result.message, /executable is unavailable/);
  assert.deepEqual(await readdir(root), []);
  await waitForNoExportDescriptors(root);
});

test("editor root rejects a symlink without requesting a reservation", async (t) => {
  const { root, parent, transport } = await fixture(t);
  const symlinkRoot = path.join(parent, "link-root");
  await symlink(root, symlinkRoot);
  const guarded = new WorkspaceUtilities(transport, { writeText() {} }, symlinkRoot, () => { throw new Error("must not launch"); });
  assert.equal((await guarded.openJobLogInEditor("job-handle")).outcome, "failed");
  assert.equal(transport.reads.length, 0);
});

test("two utility instances sharing seven occupied slots admit one delayed export", async (t) => {
  const parent = await rmRoot();
  const root = path.join(parent, "exports");
  t.after(() => rm(parent, { recursive: true, force: true }));
  const authority = { occupied: 7, nextToken: 1 };
  let releaseFetch;
  const fetchGate = new Promise((resolve) => { releaseFetch = resolve; });
  const children = [];
  const launch = () => {
    const child = new FakeChild();
    children.push(child);
    queueMicrotask(() => child.emit("spawn"));
    return child;
  };
  const makeTransport = () => ({
    reads: [],
    mutations: [],
    requestRead(method, params) {
      this.reads.push([method, params]);
      if (authority.occupied >= 8) {
        return { result: Promise.resolve({
          status: "capacity_exceeded",
          message: "The private editor export limit was reached.",
          job: params.job,
          job_id: 31,
          argv: [],
          content: null,
          slot: null,
          token: null,
          export_name: null,
        }) };
      }
      authority.occupied += 1;
      const token = authority.nextToken.toString(16).padStart(32, "0");
      authority.nextToken += 1;
      return { result: fetchGate.then(() => ({
        status: "ready",
        message: "ready",
        job: params.job,
        job_id: 31,
        argv: ["code", "--wait"],
        content: "bounded\n",
        slot: 8,
        token,
        export_name: `tongs-slot-8-job-31-${token}.log`,
      })) };
    },
    requestMutation(method, params) {
      this.mutations.push([method, params]);
      return { result: Promise.resolve({ released: true }) };
    },
  });
  const firstTransport = makeTransport();
  const secondTransport = makeTransport();
  const first = new WorkspaceUtilities(firstTransport, { writeText() {} }, root, launch);
  const second = new WorkspaceUtilities(secondTransport, { writeText() {} }, root, launch);

  const firstResult = first.openJobLogInEditor("job-one");
  while (firstTransport.reads.length === 0)
    await new Promise((resolve) => setImmediate(resolve));
  const secondResult = await second.openJobLogInEditor("job-two");
  assert.equal(secondResult.outcome, "capacity_exceeded");
  releaseFetch();
  assert.equal((await firstResult).outcome, "started");
  assert.equal(children.length, 1);
  assert.equal((await readdir(root)).length, 1);
  children[0].emit("exit", 0);
  await waitForEmptyDirectory(root);
});

test("exit cleanup preserves a replacement inode and its reservation", async (t) => {
  const { utility, root, transport, launches, children } = await fixture(t);

  assert.equal((await utility.openJobLogInEditor("job-handle")).outcome, "started");
  const exported = launches[0][1].at(-1);
  await rm(exported);
  await writeFile(exported, "replacement", { mode: 0o600 });

  if (process.platform === "linux") {
    assert.deepEqual(await exportDescriptors(root), [`${exported} (deleted)`]);
  }

  children[0].emit("exit", 0);
  await new Promise((resolve) => setTimeout(resolve, 25));

  assert.equal(await readFile(exported, "utf8"), "replacement");
  assert.deepEqual(transport.mutations, []);
  await waitForNoExportDescriptors(root);
});

test("completion resolved during cleanup handoff is replayed exactly once", async (t) => {
  const { root, transport, clipboard } = await fixture(t);
  let child;
  let cancellations = 0;
  const launch = () => {
    child = new FakeChild();
    queueMicrotask(() => child.emit("spawn"));
    return child;
  };
  const scheduleExpiry = () => {
    child.emit("exit", 0);
    return () => {
      cancellations += 1;
    };
  };
  const utility = new WorkspaceUtilities(
    transport,
    clipboard,
    root,
    launch,
    scheduleExpiry,
  );

  assert.equal((await utility.openJobLogInEditor("job-handle")).outcome, "started");
  await waitForEmptyDirectory(root);
  await waitForNoExportDescriptors(root);

  assert.equal(cancellations, 1);
  assert.deepEqual(transport.mutations, [[
    "utilities.job_log_release",
    { slot: 1, token: "a".repeat(32) },
  ]]);
  child.emit("exit", 9);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(transport.mutations.length, 1);
});

test("live export deadline closes only the descriptor and ignores late exit", async (t) => {
  const { root, transport, clipboard } = await fixture(t);
  let child;
  let expire;
  const launch = () => {
    child = new FakeChild();
    queueMicrotask(() => child.emit("spawn"));
    return child;
  };
  const utility = new WorkspaceUtilities(
    transport,
    clipboard,
    root,
    launch,
    (scheduled) => {
      expire = scheduled;
      return () => undefined;
    },
  );

  assert.equal((await utility.openJobLogInEditor("job-handle")).outcome, "started");
  const [exportName] = await readdir(root);
  const exportPath = path.join(root, exportName);
  assert.equal(await readFile(exportPath, "utf8"), "safe log\n");

  expire();
  await waitForNoExportDescriptors(root);

  assert.equal(await readFile(exportPath, "utf8"), "safe log\n");
  assert.deepEqual(transport.mutations, []);
  assert.equal(child.listenerCount("exit"), 0);
  assert.equal(child.listenerCount("error"), 0);
  child.emit("exit", 0);
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(await readFile(exportPath, "utf8"), "safe log\n");
  assert.deepEqual(transport.mutations, []);
});

test("early nonzero editor exit is distinct and removes the export", async (t) => {
  const { root, transport, clipboard } = await fixture(t);
  const launch = () => {
    const child = new FakeChild();
    queueMicrotask(() => {
      child.emit("spawn");
      child.emit("exit", 9);
    });
    return child;
  };
  const utility = new WorkspaceUtilities(transport, clipboard, root, launch);

  const result = await utility.openJobLogInEditor("job-handle");

  assert.equal(result.outcome, "failed");
  assert.match(result.message, /exited with an error/);
  assert.deepEqual(await readdir(root), []);
  await waitForNoExportDescriptors(root);
});

test("editor error after spawn is distinct and removes the export", async (t) => {
  const { root, transport, clipboard } = await fixture(t);
  const launch = () => {
    const child = new FakeChild();
    queueMicrotask(() => {
      child.emit("spawn");
      child.emit("error", new Error("launch failed"));
    });
    return child;
  };
  const utility = new WorkspaceUtilities(transport, clipboard, root, launch);

  const result = await utility.openJobLogInEditor("job-handle");

  assert.equal(result.outcome, "failed");
  assert.match(result.message, /reported an error/);
  assert.deepEqual(await readdir(root), []);
  await waitForNoExportDescriptors(root);
});
