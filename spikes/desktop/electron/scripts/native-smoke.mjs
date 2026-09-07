import { access } from "node:fs/promises";
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

const frontend = option("--frontend");
if (!frontend) throw new Error("Pass --frontend ../frontend/dist (or fixture for adapter tests)");
const pythonOption = option("--python", process.env.TONGS_DESKTOP_PYTHON || "python3");
const python = pythonOption.includes(path.sep) ? path.resolve(pythonOption) : pythonOption;
const ozonePlatform = option("--ozone-platform", "wayland");
const uiProbe = option("--ui-probe");
const report = path.resolve(option("--report", path.join(electronDir, "evidence", "smoke.json")));
const screenshot = path.resolve(
  option("--screenshot", path.join(electronDir, "evidence", "native-window.png")),
);
const executable = path.join(electronDir, "node_modules", "electron", "dist", "electron");
const args = [
  `--ozone-platform=${ozonePlatform}`,
  "--disable-gpu",
  electronDir,
  "--frontend",
  path.resolve(frontend),
  "--smoke-report",
  report,
  "--screenshot",
  screenshot,
];
if (uiProbe) args.push("--ui-probe", path.resolve(uiProbe));

const child = spawn(executable, args, {
  env: { ...process.env, TONGS_DESKTOP_PYTHON: python },
  stdio: "inherit",
});
const timeout = setTimeout(() => child.kill("SIGTERM"), 30_000);
const code = await new Promise((resolve, reject) => {
  child.once("error", reject);
  child.once("exit", resolve);
});
clearTimeout(timeout);
if (code !== 0) throw new Error(`Electron smoke exited with status ${code}`);
await Promise.all([access(report), access(screenshot)]);
process.stdout.write(`${report}\n${screenshot}\n`);
