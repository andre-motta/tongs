import { cp, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const electronDir = path.dirname(scriptDir);

function option(name, fallback = null) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

function assertSafeOutput(output) {
  const allowedRoots = ["build", "dist"].map((name) => path.join(electronDir, name));
  if (!allowedRoots.some((root) => output.startsWith(`${root}${path.sep}`))) {
    throw new Error("Distribution output must be below electron/build or electron/dist");
  }
}

export async function buildDistribution({ frontendDir, outputDir }) {
  const runtimeDir = path.join(electronDir, "node_modules", "electron", "dist");
  const backendDir = path.resolve(electronDir, "..", "backend");
  const frontend = path.resolve(frontendDir);
  const output = path.resolve(outputDir);
  assertSafeOutput(output);
  await readFile(path.join(runtimeDir, "electron"));
  await readFile(path.join(frontend, "index.html"));
  await readFile(path.join(backendDir, "__main__.py"));

  await rm(output, { recursive: true, force: true });
  await mkdir(output, { recursive: true });
  await cp(runtimeDir, output, { recursive: true, preserveTimestamps: true });
  await rename(path.join(output, "electron"), path.join(output, "tongs-electron"));
  await rm(path.join(output, "resources", "default_app.asar"), { force: true });

  const packagedApp = path.join(output, "resources", "app");
  await mkdir(packagedApp, { recursive: true });
  await cp(path.join(electronDir, "src"), path.join(packagedApp, "src"), {
    recursive: true,
  });
  await cp(path.join(electronDir, "assets"), path.join(packagedApp, "assets"), {
    recursive: true,
  });
  await cp(frontend, path.join(packagedApp, "frontend"), { recursive: true });
  await mkdir(path.join(packagedApp, "python"), { recursive: true });
  await cp(backendDir, path.join(packagedApp, "python", "backend"), {
    recursive: true,
  });
  await writeFile(
    path.join(packagedApp, "package.json"),
    `${JSON.stringify(
      {
        name: "tongs-electron-prototype-runtime",
        version: "0.0.1",
        private: true,
        type: "module",
        main: "src/main.mjs",
      },
      null,
      2,
    )}\n`,
  );
  await writeFile(
    path.join(output, "DISTRIBUTION.txt"),
    "Tongs Electron prototype for Linux x86_64. Fixture-only, unsupported.\n",
  );
  return output;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const frontendDir = option("--frontend");
  if (!frontendDir) {
    throw new Error("Pass --frontend ../frontend/dist (or fixture for adapter tests)");
  }
  const outputDir = option(
    "--output",
    path.join(electronDir, "dist", "tongs-electron-linux-x64"),
  );
  const output = await buildDistribution({ frontendDir, outputDir });
  process.stdout.write(`${output}\n`);
}
