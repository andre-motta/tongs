import assert from "node:assert/strict";
import test from "node:test";
import {
  assertRequiredHardwareGpu,
  evaluateGpuEvidence,
  finalizeGpuEvidence,
} from "../src/gpu.mjs";

function hardwareReport() {
  return {
    electron_pid: 100,
    disable_gpu: false,
    no_sandbox: false,
    disable_gpu_sandbox: false,
    disable_seccomp_filter_sandbox: false,
    child_process_failures: [],
    gpu: {
      hardware_acceleration_enabled: true,
      feature_status: {
        gpu_compositing: "enabled",
        rasterization: "enabled",
        webgl: "enabled",
      },
      info: {
        gpuDevice: [{ active: true, vendorId: 0x10de, deviceId: 0x2b85 }],
      },
      webgl: {
        available: true,
        vendor: "Google Inc. (NVIDIA Corporation)",
        renderer: "ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 5090, OpenGL)",
      },
      process: { pid: 101, type: "GPU" },
      process_sandbox: { NoNewPrivs: "1", Seccomp: "2", Seccomp_filters: "1" },
    },
    renderer_process: {
      sandbox: { NoNewPrivs: "1", Seccomp: "2", Seccomp_filters: "1" },
      security: { process_global: "undefined", require_global: "undefined" },
    },
  };
}

test("hardware evidence requires a physical renderer and sandboxed processes", () => {
  assert.deepEqual(evaluateGpuEvidence(hardwareReport()).failed, []);

  const software = hardwareReport();
  software.gpu.info.gpuDevice[0] = { active: true, vendorId: 0x1ae0, deviceId: 0xc0de };
  software.gpu.webgl.renderer = "ANGLE (Google, Vulkan 1.3 SwiftShader Device)";
  software.gpu.process_sandbox.Seccomp = "0";
  assert.deepEqual(evaluateGpuEvidence(software).failed, [
    "physical_device",
    "hardware_webgl",
    "gpu_process_sandbox",
  ]);
});

test("final verdict retains a GPU failure injected after the initial evidence", () => {
  const report = hardwareReport();
  report.gpu.acceptance = evaluateGpuEvidence(report);
  assert.equal(report.gpu.acceptance.passed, true);
  const graphics = {
    gpu: report.gpu,
    renderer_process: report.renderer_process,
    main_process: { pid: report.electron_pid, sandbox: { Seccomp: "0" } },
    process_metrics: [report.gpu.process],
  };
  const injectedFailure = {
    type: "GPU",
    reason: "killed",
    exit_code: 9,
    service_name: null,
    name: null,
  };

  finalizeGpuEvidence(report, graphics, [injectedFailure]);

  assert.deepEqual(report.gpu.acceptance.failed, ["no_child_process_failures"]);
  assert.throws(
    () => assertRequiredHardwareGpu(report, true),
    /Hardware GPU evidence failed: no_child_process_failures/,
  );
  assert.deepEqual(JSON.parse(JSON.stringify(report)).child_process_failures, [
    injectedFailure,
  ]);
});
