const SOFTWARE_RENDERERS = /swiftshader|llvmpipe|lavapipe|software rasterizer/i;
const SOFTWARE_DEVICE_IDS = new Set(["1ae0:c0de"]);

export function evaluateGpuEvidence(report) {
  const checks = {};
  const gpu = report.gpu ?? {};
  const features = gpu.feature_status ?? {};
  const webgl = gpu.webgl ?? {};
  const activeDevice = gpu.info?.gpuDevice?.find((device) => device.active) ?? null;
  const deviceKey = activeDevice
    ? `${activeDevice.vendorId.toString(16)}:${activeDevice.deviceId.toString(16)}`
    : null;
  const renderer = `${webgl.vendor ?? ""} ${webgl.renderer ?? ""}`;

  checks.acceleration_enabled = gpu.hardware_acceleration_enabled === true;
  checks.gpu_not_disabled = report.disable_gpu === false;
  checks.no_sandbox_disabling_switches =
    report.no_sandbox === false &&
    report.disable_gpu_sandbox === false &&
    report.disable_seccomp_filter_sandbox === false;
  checks.hardware_features =
    features.gpu_compositing === "enabled" &&
    features.rasterization === "enabled" &&
    features.webgl === "enabled";
  checks.physical_device =
    activeDevice !== null &&
    activeDevice.vendorId !== 0 &&
    activeDevice.deviceId !== 0 &&
    !SOFTWARE_DEVICE_IDS.has(deviceKey);
  checks.hardware_webgl =
    webgl.available === true && renderer.trim().length > 0 && !SOFTWARE_RENDERERS.test(renderer);
  checks.separate_gpu_process =
    gpu.process?.type === "GPU" && gpu.process.pid !== report.electron_pid;
  checks.gpu_process_sandbox =
    gpu.process_sandbox?.NoNewPrivs === "1" &&
    gpu.process_sandbox?.Seccomp === "2" &&
    Number(gpu.process_sandbox?.Seccomp_filters) >= 1 &&
    gpu.parent_sandbox?.Seccomp === "0" &&
    Number(gpu.parent_sandbox?.Seccomp_filters) === 0;
  checks.renderer_process_sandbox =
    report.renderer_process?.sandbox?.NoNewPrivs === "1" &&
    report.renderer_process?.sandbox?.Seccomp === "2" &&
    Number(report.renderer_process?.sandbox?.Seccomp_filters) >= 1 &&
    report.renderer_process?.security?.process_global === "undefined" &&
    report.renderer_process?.security?.require_global === "undefined";
  checks.no_child_process_failures = report.child_process_failures?.length === 0;

  const failed = Object.entries(checks)
    .filter(([, passed]) => !passed)
    .map(([name]) => name);
  return { passed: failed.length === 0, checks, failed };
}

export function finalizeGpuEvidence(report, graphics, childProcessFailures) {
  report.gpu = graphics.gpu;
  report.renderer_process = graphics.renderer_process;
  report.main_process = graphics.main_process;
  report.process_metrics = graphics.process_metrics;
  report.child_process_failures = childProcessFailures.map((failure) => ({ ...failure }));
  report.gpu.acceptance = evaluateGpuEvidence(report);
  return report;
}

export function assertRequiredHardwareGpu(report, required) {
  if (required && !report.gpu?.acceptance?.passed) {
    throw new Error(
      `Hardware GPU evidence failed: ${report.gpu?.acceptance?.failed?.join(", ")}`,
    );
  }
}
