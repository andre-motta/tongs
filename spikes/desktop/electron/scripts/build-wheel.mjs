import { cp, mkdir, rm } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { buildDistribution } from "./build-distribution.mjs";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const electronDir = path.dirname(scriptDir);

function option(name, fallback = null) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

function run(command, args, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, stdio: "inherit" });
    child.once("error", reject);
    child.once("exit", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`${command} exited with status ${code}`));
    });
  });
}

const frontendDir = option("--frontend");
if (!frontendDir) {
  throw new Error("Pass --frontend ../frontend/dist (or fixture for adapter tests)");
}
const pythonOption = option("--python", "python3");
const python = pythonOption.includes(path.sep) ? path.resolve(pythonOption) : pythonOption;
const buildRoot = path.join(electronDir, "build", "wheel");
const staging = path.join(buildRoot, "source");
const runtime = path.join(
  staging,
  "src",
  "tongs_electron_prototype",
  "runtime",
);
const wheelhouse = path.resolve(option("--output", path.join(electronDir, "dist")));

await rm(buildRoot, { recursive: true, force: true });
await mkdir(staging, { recursive: true });
await cp(path.join(electronDir, "wheel"), staging, { recursive: true });
await buildDistribution({ frontendDir, outputDir: runtime });
await mkdir(wheelhouse, { recursive: true });
await run(
  python,
  ["-m", "pip", "wheel", ".", "--no-build-isolation", "--no-deps", "--wheel-dir", wheelhouse],
  staging,
);
