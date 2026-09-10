import { EventEmitter } from "node:events";
import { spawn } from "node:child_process";
import { validateAssetUrl, validateInvocation } from "./security.mjs";

// The 20,000-line comparison fixture is about 2.3 MiB as one NDJSON frame.
const MAX_LINE_BYTES = 16 * 1024 * 1024;
const STDERR_LIMIT = 8 * 1024;

export class Sidecar extends EventEmitter {
  constructor({
    python,
    frontendDir,
    backendDir,
    startupTimeoutMs = 10_000,
    requestTimeoutMs = 10_000,
    shutdownTimeoutMs = 3_000,
  }) {
    super();
    this.python = python;
    this.frontendDir = frontendDir;
    this.backendDir = backendDir;
    this.startupTimeoutMs = startupTimeoutMs;
    this.requestTimeoutMs = requestTimeoutMs;
    this.shutdownTimeoutMs = shutdownTimeoutMs;
    this.child = null;
    this.pending = new Map();
    this.nextId = 1;
    this.stdoutBuffer = "";
    this.stderr = "";
    this.url = null;
    this.started = false;
    this.stopping = false;
    this.startPromise = null;
  }

  async start() {
    if (this.started) return this.url;
    if (this.startPromise) return this.startPromise;

    this.startPromise = new Promise((resolve, reject) => {
      let settled = false;
      const finish = (error, url) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (error) reject(error);
        else resolve(url);
      };
      const timer = setTimeout(() => {
        finish(new Error("Python sidecar did not become ready in time"));
        this._terminate();
      }, this.startupTimeoutMs);

      let child;
      try {
        child = spawn(
          this.python,
          ["-m", "backend", "--frontend", this.frontendDir],
          {
            cwd: this.backendDir,
            stdio: ["pipe", "pipe", "pipe"],
            windowsHide: true,
          },
        );
      } catch {
        finish(new Error("Unable to start the Python sidecar"));
        return;
      }
      this.child = child;
      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", (chunk) => this._consumeStdout(chunk, finish));
      child.stderr.on("data", (chunk) => {
        this.stderr = (this.stderr + chunk).slice(-STDERR_LIMIT);
      });
      child.once("error", () => {
        finish(new Error("Unable to start the Python sidecar"));
      });
      child.once("exit", (code, signal) => {
        this.child = null;
        this.started = false;
        const error = new Error(
          this.stopping
            ? "Python sidecar stopped"
            : `Python sidecar exited unexpectedly (${signal ?? code ?? "unknown"})`,
        );
        finish(error);
        for (const { reject: rejectRequest, timer: requestTimer } of this.pending.values()) {
          clearTimeout(requestTimer);
          rejectRequest(error);
        }
        this.pending.clear();
        if (!this.stopping) this.emit("crash", error);
      });
    });

    try {
      return await this.startPromise;
    } finally {
      this.startPromise = null;
    }
  }

  request(method, params = {}) {
    const invocation = validateInvocation(method, params);
    if (!this.started || !this.child?.stdin.writable) {
      return Promise.reject(new Error("Python sidecar is not running"));
    }
    const id = this.nextId;
    this.nextId = this.nextId === Number.MAX_SAFE_INTEGER ? 1 : this.nextId + 1;
    if (this.pending.has(id)) {
      return Promise.reject(new Error("Python sidecar request ID space is exhausted"));
    }

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("Python sidecar request timed out"));
      }, this.requestTimeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      const frame = `${JSON.stringify({ id, ...invocation })}\n`;
      this.child.stdin.write(frame, (error) => {
        if (!error) return;
        const pending = this.pending.get(id);
        if (!pending) return;
        clearTimeout(pending.timer);
        this.pending.delete(id);
        pending.reject(new Error("Unable to write to the Python sidecar"));
      });
    });
  }

  async stop() {
    if (!this.child) return;
    this.stopping = true;
    const child = this.child;
    const exited = new Promise((resolve) => child.once("exit", resolve));
    child.stdin.end();
    let shutdownTimer;
    const timeout = new Promise((resolve) => {
      shutdownTimer = setTimeout(() => resolve("timeout"), this.shutdownTimeoutMs);
    });
    const shutdownResult = await Promise.race([exited, timeout]);
    clearTimeout(shutdownTimer);
    if (shutdownResult === "timeout" && child.exitCode === null) {
      child.kill("SIGTERM");
      let killTimer;
      const killTimeout = new Promise((resolve) => {
        killTimer = setTimeout(resolve, 1_000);
      });
      await Promise.race([exited, killTimeout]);
      clearTimeout(killTimer);
      if (child.exitCode === null) child.kill("SIGKILL");
    }
    this.child = null;
  }

  _consumeStdout(chunk, finishStart) {
    this.stdoutBuffer += chunk;
    if (Buffer.byteLength(this.stdoutBuffer) > MAX_LINE_BYTES) {
      finishStart(new Error("Python sidecar emitted an oversized protocol frame"));
      this._terminate();
      return;
    }
    let newline;
    while ((newline = this.stdoutBuffer.indexOf("\n")) >= 0) {
      const line = this.stdoutBuffer.slice(0, newline);
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
      if (!line) continue;
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        finishStart(new Error("Python sidecar emitted invalid JSON"));
        this._terminate();
        return;
      }
      if (!this.started) {
        try {
          if (message?.event !== "ready") throw new Error();
          const parsed = validateAssetUrl(message.url);
          this.url = parsed.href;
          this.started = true;
          finishStart(null, this.url);
        } catch {
          finishStart(new Error("Python sidecar emitted an invalid readiness frame"));
          this._terminate();
        }
        continue;
      }
      this._handleResponse(message);
    }
  }

  _handleResponse(message) {
    if (!Number.isSafeInteger(message?.id)) return;
    const pending = this.pending.get(message.id);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pending.delete(message.id);
    if (Object.hasOwn(message, "result")) {
      pending.resolve(message.result);
    } else {
      pending.reject(new Error(message?.error?.message || "Python sidecar request failed"));
    }
  }

  _terminate() {
    if (this.child?.exitCode === null) this.child.kill("SIGTERM");
  }
}
