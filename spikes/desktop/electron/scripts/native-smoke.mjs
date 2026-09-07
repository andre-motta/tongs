import { access, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const electronDir = path.dirname(scriptDir);

function option(name, fallback = null) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

function hasFlag(name) {
  return process.argv.includes(name);
}

const frontend = option("--frontend");
if (!frontend) throw new Error("Pass --frontend ../frontend/dist (or fixture for adapter tests)");
const pythonOption = option("--python", process.env.TONGS_DESKTOP_PYTHON || "python3");
const python = pythonOption.includes(path.sep) ? path.resolve(pythonOption) : pythonOption;
const ozonePlatform = option("--ozone-platform", "x11");
const uiProbe = option("--ui-probe");
const report = path.resolve(option("--report", path.join(electronDir, "evidence", "smoke.json")));
const screenshot = path.resolve(
  option("--screenshot", path.join(electronDir, "evidence", "native-window.png")),
);
const executable = path.join(electronDir, "node_modules", "electron", "dist", "electron");
const args = [
  electronDir,
  "--frontend",
  path.resolve(frontend),
  "--smoke-report",
  report,
  "--screenshot",
  screenshot,
];
args.unshift(`--ozone-platform=${ozonePlatform}`);
if (hasFlag("--disable-vulkan")) args.unshift("--disable-vulkan");
if (hasFlag("--disable-gpu")) args.unshift("--disable-gpu");
if (hasFlag("--enable-gpu-sandbox")) args.unshift("--enable-gpu-sandbox");
if (hasFlag("--gpu-sandbox-start-early")) args.unshift("--gpu-sandbox-start-early");
const useAngle = option("--use-angle");
if (useAngle) args.unshift(`--use-angle=${useAngle}`);
const useGl = option("--use-gl");
if (useGl) args.unshift(`--use-gl=${useGl}`);
if (uiProbe) args.push("--ui-probe", path.resolve(uiProbe));
if (hasFlag("--require-hardware-gpu")) args.push("--require-hardware-gpu");

const child = spawn(executable, args, {
  env: { ...process.env, TONGS_DESKTOP_PYTHON: python },
  stdio: "inherit",
});
let timedOut = false;
const timeout = setTimeout(() => {
  timedOut = true;
  child.kill("SIGTERM");
}, 30_000);
const code = await new Promise((resolve, reject) => {
  child.once("error", reject);
  child.once("exit", resolve);
});
clearTimeout(timeout);
let evidence = null;
try {
  evidence = JSON.parse(await readFile(report, "utf8"));
  evidence.launch = { exit_code: code, timed_out: timedOut };
  await writeFile(report, `${JSON.stringify(evidence, null, 2)}\n`);
} catch (error) {
  if (code === 0) throw error;
}
if (code !== 0) throw new Error(`Electron smoke exited with status ${code}`);
await Promise.all([access(report), access(screenshot)]);
if (hasFlag("--require-hardware-gpu")) {
  if (!evidence.gpu?.acceptance?.passed) {
    throw new Error(
      `Hardware GPU evidence failed: ${evidence.gpu?.acceptance?.failed?.join(", ")}`,
    );
  }
}
process.stdout.write(`${report}\n${screenshot}\n`);
