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
    gpu.process_sandbox?.NoNewPrivs === "1" && gpu.process_sandbox?.Seccomp === "2";
  checks.renderer_process_sandbox =
    report.renderer_process?.sandbox?.NoNewPrivs === "1" &&
    report.renderer_process?.sandbox?.Seccomp === "2" &&
    report.renderer_process?.security?.process_global === "undefined" &&
    report.renderer_process?.security?.require_global === "undefined";
  checks.no_child_process_failures = report.child_process_failures?.length === 0;

  const failed = Object.entries(checks)
    .filter(([, passed]) => !passed)
    .map(([name]) => name);
  return { passed: failed.length === 0, checks, failed };
}
