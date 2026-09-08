import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile, realpath, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { SidecarTransport } from "../../../desktop/dist/src/main/sidecar.js";

const MAX_EVIDENCE_BYTES = 8 * 1024 * 1024;
const TIMEOUT_MS = 10_000;
const expectedLedger = {
  action: "add_comment",
  body: "one controlled installed-core mutation",
  project: "acceptance/shared-drafts",
  remote_id: "remote-comment-106",
  review_number: 106,
  sequence: 1,
  stage: "remote_accepted_before_ack",
};

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function requiredAbsolute(name) {
  const value = required(name);
  if (!path.isAbsolute(value) || value.includes("\0")) {
    throw new Error(`${name} must be an absolute path`);
  }
  return path.resolve(value);
}

async function sha256(file) {
  const bytes = await readFile(file);
  if (bytes.length > MAX_EVIDENCE_BYTES) throw new Error(`${file} is too large`);
  return createHash("sha256").update(bytes).digest("hex");
}

async function readJson(file) {
  const bytes = await readFile(file);
  if (bytes.length > MAX_EVIDENCE_BYTES) throw new Error(`${file} is too large`);
  return JSON.parse(bytes.toString("utf8"));
}

async function readJsonl(file) {
  try {
    const bytes = await readFile(file);
    if (bytes.length > MAX_EVIDENCE_BYTES) throw new Error(`${file} is too large`);
    return bytes
      .toString("utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line));
  } catch (error) {
    if (error?.code === "ENOENT") return [];
    throw error;
  }
}

async function cgroupLimits() {
  const membership = (await readFile("/proc/self/cgroup", "utf8")).trim().split("\n");
  const unified = membership.find((line) => line.startsWith("0::"));
  if (!unified) throw new Error("unified cgroup membership is unavailable");
  const root = path.join("/sys/fs/cgroup", unified.slice(3));
  const limits = {
    memory_max: (await readFile(path.join(root, "memory.max"), "utf8")).trim(),
    memory_swap_max: (await readFile(path.join(root, "memory.swap.max"), "utf8")).trim(),
    path: unified.slice(3),
    tasks_max: (await readFile(path.join(root, "pids.max"), "utf8")).trim(),
  };
  assert.equal(limits.memory_max, "1073741824");
  assert.equal(limits.memory_swap_max, "0");
  assert.equal(limits.tasks_max, "64");
  assert.equal(process.env.NODE_OPTIONS, "--max-old-space-size=512");
  return limits;
}

async function eventually(read, predicate) {
  const deadline = Date.now() + TIMEOUT_MS;
  while (Date.now() < deadline) {
    const value = await read();
    if (predicate(value)) return value;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error("bounded evidence condition was not reached");
}

async function rejectionCode(promise) {
  try {
    await promise;
  } catch (error) {
    return error?.code;
  }
  throw new Error("request unexpectedly succeeded");
}

test("installed core crosses an actual transport and server restart once", async () => {
  const guard = await cgroupLimits();
  const terminalReportPath = requiredAbsolute("TONGS_INSTALLED_CORE_TERMINAL_REPORT");
  const evidenceRoot = requiredAbsolute("TONGS_INSTALLED_CORE_EVIDENCE_ROOT");
  const sourceRoot = requiredAbsolute("TONGS_INSTALLED_CORE_SOURCE_ROOT");
  const wrapper = requiredAbsolute("TONGS_INSTALLED_CORE_SIDECAR_WRAPPER");
  const fixture = requiredAbsolute("TONGS_INSTALLED_CORE_DRAFT_FIXTURE");
  const expectedCommit = required("TONGS_INSTALLED_CORE_SOURCE_COMMIT");
  const expectedFixtureHash = required("TONGS_INSTALLED_CORE_DRAFT_FIXTURE_SHA256");
  const reportPath = path.join(evidenceRoot, "installed-core-sidecar.json");
  const ledgerPath = path.join(evidenceRoot, "mock-forge-ledger.jsonl");
  const eventPath = path.join(evidenceRoot, "installed-core-process-events.jsonl");
  const terminal = await readJson(terminalReportPath);
  assert.equal(terminal.status, "pass");
  assert.equal(terminal.source.commit, expectedCommit);
  assert.equal(terminal.controlled_fixture.path, fixture);
  assert.equal(terminal.controlled_fixture.sha256, expectedFixtureHash);
  assert.equal(terminal.sidecar_wrapper.path, wrapper);
  assert.equal(terminal.sidecar_wrapper.sha256, await sha256(wrapper));
  assert.equal(await sha256(fixture), expectedFixtureHash);
  assert.equal(terminal.installed_identity.editable, false);
  assert.equal(terminal.installed_identity.mcp_available, false);

  const candidatePython = terminal.installed_identity.executable;
  const packageRoot = terminal.installed_identity.package_root;
  const coreVersion = terminal.installed_identity.tongs_version;
  assert.equal(path.isAbsolute(candidatePython), true);
  assert.equal(path.isAbsolute(packageRoot), true);
  assert.equal(await realpath(candidatePython), terminal.installed_identity.proc_self_exe);
  assert.equal((await stat(candidatePython)).isFile(), true);

  const launches = [];
  const stderr = [];
  const spawnInstalled = (executable, args, options) => {
    assert.equal(executable, candidatePython);
    assert.deepEqual(args, ["-E", "-P", "-m", "tongs.desktop.sidecar"]);
    assert.equal(options.cwd, evidenceRoot);
    const generation = launches.length + 1;
    const forgeMode = generation === 1 ? "block_after_accept" : "acknowledge";
    const environment = {
      ...options.env,
      TONGS_INSTALLED_CORE_DRAFT_FIXTURE: fixture,
      TONGS_INSTALLED_CORE_DRAFT_FIXTURE_SHA256: expectedFixtureHash,
      TONGS_INSTALLED_CORE_EVIDENCE_ROOT: evidenceRoot,
      TONGS_INSTALLED_CORE_FORGE_MODE: forgeMode,
      TONGS_INSTALLED_CORE_PACKAGE_ROOT: packageRoot,
      TONGS_INSTALLED_CORE_SOURCE_COMMIT: expectedCommit,
      TONGS_INSTALLED_CORE_SOURCE_ROOT: sourceRoot,
      TONGS_INSTALLED_CORE_VERSION: coreVersion,
    };
    delete environment.PYTHONHOME;
    delete environment.PYTHONPATH;
    const child = spawn(executable, ["-E", "-P", wrapper], {
      ...options,
      env: environment,
    });
    const chunks = [];
    let size = 0;
    child.stderr.on("data", (chunk) => {
      size += chunk.length;
      if (size <= MAX_EVIDENCE_BYTES) chunks.push(chunk);
    });
    child.once("exit", () => {
      stderr[generation - 1] = Buffer.concat(chunks).toString("utf8");
    });
    launches.push({
      args: ["-E", "-P", wrapper],
      cwd: options.cwd,
      executable,
      forgeMode,
      pid: child.pid,
    });
    return child;
  };

  const transport = new SidecarTransport(
    { pythonExecutable: candidatePython, coreVersion, safeCwd: evidenceRoot },
    TIMEOUT_MS,
    TIMEOUT_MS,
    2_000,
    spawnInstalled,
  );
  let outcome;
  try {
    await transport.start();
    assert.equal(transport.sessionGeneration, 1);
    const firstPid = transport.processId;
    assert.equal(firstPid, launches[0].pid);

    const discovered = await transport.requestRead("repositories.discover", {}).result;
    assert.equal(discovered.repositories.length, 1);
    const oldRepository = discovered.repositories[0].handle;
    const opened = await transport.requestRead("repositories.open", {
      hostname: "acceptance.example",
      project_path: "acceptance/shared-drafts",
    }).result;
    assert.equal(opened.handle, oldRepository);
    const listed = await transport.requestRead("reviews.list", {
      repository: oldRepository,
      scope: "all_open",
    }).result;
    assert.equal(listed.failures.length, 0);
    assert.equal(listed.items.length, 1);
    const oldReview = listed.items[0].handle;
    const oldSnapshot = await transport.requestRead("reviews.get", {
      review: oldReview,
    }).result;
    assert.equal(oldSnapshot.handle, oldReview);

    const mutation = transport.requestMutation("review_mutations.comment", {
      operation_id: "12300000-0000-4000-8000-000000000001",
      review: oldReview,
      body: expectedLedger.body,
    }).result;
    const acceptedLedger = await eventually(
      () => readJsonl(ledgerPath),
      (records) => records.length === 1,
    );
    assert.deepEqual(acceptedLedger, [expectedLedger]);

    const restart = transport.restart();
    const interruptedCode = await rejectionCode(mutation);
    await restart;
    assert.equal(interruptedCode, "shutting_down");
    assert.equal(transport.sessionGeneration, 2);
    const secondPid = transport.processId;
    assert.notEqual(secondPid, firstPid);
    assert.equal(secondPid, launches[1].pid);

    const oldRepositoryCode = await rejectionCode(
      transport.requestRead("reviews.list", {
        repository: oldRepository,
        scope: "all_open",
      }).result,
    );
    const oldReviewCode = await rejectionCode(
      transport.requestRead("reviews.get", { review: oldReview }).result,
    );
    assert.equal(oldRepositoryCode, "invalid_handle");
    assert.equal(oldReviewCode, "invalid_handle");

    const rediscovered = await transport.requestRead("repositories.discover", {}).result;
    assert.equal(rediscovered.repositories.length, 1);
    const newRepository = rediscovered.repositories[0].handle;
    assert.notEqual(newRepository, oldRepository);
    const reopened = await transport.requestRead("repositories.open", {
      hostname: "acceptance.example",
      project_path: "acceptance/shared-drafts",
    }).result;
    assert.equal(reopened.handle, newRepository);
    const relisted = await transport.requestRead("reviews.list", {
      repository: newRepository,
      scope: "all_open",
    }).result;
    assert.equal(relisted.items.length, 1);
    const newReview = relisted.items[0].handle;
    assert.notEqual(newReview, oldReview);
    const newSnapshot = await transport.requestRead("reviews.get", {
      review: newReview,
    }).result;
    assert.equal(newSnapshot.handle, newReview);
    assert.deepEqual(await readJsonl(ledgerPath), [expectedLedger]);
    assert.equal(launches.length, 2);

    const processEvents = await readJsonl(eventPath);
    const wrapperEvents = processEvents.filter(
      (record) => record.event === "installed_core_process_started",
    );
    assert.equal(wrapperEvents.length, 2);
    assert.deepEqual(
      wrapperEvents.map((record) => record.forge_mode),
      ["block_after_accept", "acknowledge"],
    );
    assert.deepEqual(
      wrapperEvents.map((record) => record.pid),
      [firstPid, secondPid],
    );
    for (const record of wrapperEvents) {
      assert.equal(record.package_root, packageRoot);
      assert.equal(record.fixture_path, fixture);
      assert.equal(record.fixture_sha256, expectedFixtureHash);
      assert.equal(record.source_commit, expectedCommit);
      assert.equal(record.wrapper_path, wrapper);
      assert.equal(record.wrapper_sha256, terminal.sidecar_wrapper.sha256);
      assert.deepEqual(record.cmdline, [candidatePython, "-E", "-P", wrapper]);
      assert.equal(record.proc_self_exe, terminal.installed_identity.proc_self_exe);
      assert.equal(
        record.sys_path.some((entry) => {
          const relative = path.relative(sourceRoot, entry);
          return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
        }),
        false,
      );
    }
    outcome = {
      fixture: terminal.controlled_fixture,
      generations: [
        { generation: 1, pid: firstPid, repository: oldRepository, review: oldReview },
        { generation: 2, pid: secondPid, repository: newRepository, review: newReview },
      ],
      guard,
      interrupted_mutation: {
        operation_id: "12300000-0000-4000-8000-000000000001",
        rejection_code: interruptedCode,
      },
      ledger: [expectedLedger],
      launches,
      old_handle_rejections: {
        repository: oldRepositoryCode,
        review: oldReviewCode,
      },
      process_events: processEvents,
      source_commit: expectedCommit,
      status: "pass",
      terminal_report: {
        path: terminalReportPath,
        sha256: await sha256(terminalReportPath),
      },
      wrapper: terminal.sidecar_wrapper,
    };
  } finally {
    await transport.stop();
  }
  if (stderr.some((value) => value)) {
    throw new Error(`sidecar stderr was not empty: ${JSON.stringify(stderr)}`);
  }
  await writeFile(reportPath, `${JSON.stringify(outcome, null, 2)}\n`, { mode: 0o600 });
  assert.equal((await readJson(reportPath)).status, "pass");
});
