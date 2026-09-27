// A scripted stand-in for the Python desktop service. Forge reads never run:
// the fake child answers only the handshake and shutdown frames.
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { SidecarTransport } from "../../../desktop/dist/src/main/sidecar.js";

export const caps = [
  "assets",
  "cancellation",
  "ci_mutations",
  "events",
  "opaque_handles",
  "paged_diffs",
  "paged_logs",
  "plugins",
  "review_mutations",
  "workspace_utilities",
];
export const methods = [
  "assets.list",
  "assets.read",
  "ci.capabilities",
  "ci.receipt",
  "commits.list",
  "diff.open",
  "diff.page",
  "discussions.list",
  "host.set_location",
  "jobs.list",
  "jobs.cancel",
  "jobs.retry",
  "logs.open",
  "logs.page",
  "pipelines.list",
  "pipelines.cancel",
  "pipelines.retry",
  "plugins.invoke",
  "plugins.list",
  "repositories.discover",
  "repositories.open",
  "review_actions.capabilities",
  "review_actions.close",
  "review_actions.merge",
  "review_actions.receipt",
  "review_actions.reopen",
  "review_actions.unapprove",
  "review_mutations.capabilities",
  "review_mutations.comment",
  "review_mutations.inline_comment",
  "review_mutations.reply",
  "review_mutations.resolve",
  "review_mutations.verdict",
  "review_pipelines.list",
  "review_submissions.list",
  "review_submissions.reconcile",
  "review_submissions.resume",
  "review_submissions.start",
  "review_submissions.status",
  "reviews.get",
  "reviews.list",
  "utilities.cache_clear",
  "utilities.job_log_export",
  "utilities.job_log_release",
  "utilities.review_url",
  "drafts.create",
  "drafts.discard",
  "drafts.get",
  "drafts.list",
  "drafts.save",
  "shutdown",
];

export function harness({
  autoHandshake = true,
  delayedKill = false,
  delayedShutdown = false,
  extraCapabilities = [],
  limitOverrides = {},
} = {}) {
  const children = [];
  const respond = (child, frame) => {
    const result = {
      protocol_major: 1,
      core_version: "1.0",
      session_id: "1234567890abcdef",
      capabilities: [...caps, ...extraCapabilities],
      accepted_capabilities: caps,
      methods,
      limits: {
        request_frame_bytes: 262144,
        response_frame_bytes: 8388608,
        event_frame_bytes: 65536,
        pending_requests: 64,
        queued_events: 256,
        asset_chunk_bytes: 524288,
        json_values: 20000,
        json_depth: 24,
        ...limitOverrides,
      },
    };
    child.stdout.write(
      `${JSON.stringify({ v: 1, type: "response", id: frame.id, result })}\n`,
    );
  };
  const spawn = (_executable, _arguments, options) => {
    const child = new EventEmitter();
    children.push(child);
    child.stdin = new PassThrough();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.pid = 4000 + children.length;
    child.killed = false;
    child.exitCode = null;
    child.signalCode = null;
    child.frames = [];
    child.spawnOptions = options;
    child.finish = (code = 0, signal = null) => {
      child.exitCode = code;
      child.signalCode = signal;
      child.emit("exit", code, signal);
    };
    child.kill = (signal) => {
      child.killed = true;
      if (!delayedKill) queueMicrotask(() => child.finish(null, signal));
      return true;
    };
    let input = "";
    child.stdin.on("data", (chunk) => {
      input += chunk;
      while (input.includes("\n")) {
        const newline = input.indexOf("\n");
        const frame = JSON.parse(input.slice(0, newline));
        input = input.slice(newline + 1);
        child.frames.push(frame);
        if (frame.method === "handshake") {
          child.handshake = frame;
          if (autoHandshake) respond(child, frame);
        }
        if (frame.method === "shutdown" && !delayedShutdown) {
          child.stdout.write(
            `${JSON.stringify({ v: 1, type: "response", id: frame.id, result: {} })}\n`,
          );
          queueMicrotask(() => child.finish(0, null));
        }
      }
    });
    return child;
  };
  return {
    spawn,
    children,
    respond: (child) => respond(child, child.handshake),
  };
}

export const launch = {
  pythonExecutable: process.execPath,
  coreVersion: "1.0",
  safeCwd: process.cwd(),
};

export function transportFor(fake, shutdownTimeout = 1_000) {
  return new SidecarTransport(
    launch,
    1_000,
    1_000,
    shutdownTimeout,
    fake.spawn,
  );
}
