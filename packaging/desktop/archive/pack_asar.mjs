import { createRequire } from "node:module";
import { lstat, mkdir, readdir } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [desktopRootArgument, inputArgument, outputArgument] = process.argv.slice(2);
if (!desktopRootArgument || !inputArgument || !outputArgument) {
  throw new Error("usage: pack_asar.mjs DESKTOP_ROOT INPUT_ROOT OUTPUT_ASAR");
}

const desktopRoot = path.resolve(desktopRootArgument);
const inputRoot = path.resolve(inputArgument);
const output = path.resolve(outputArgument);
const require = createRequire(path.join(desktopRoot, "package.json"));
const asarEntry = require.resolve("@electron/asar");
const { createPackageFromFiles, listPackage } = await import(
  pathToFileURL(asarEntry).href
);

const runtimeRoots = ["dist/src/main", "dist/src/preload", "dist/src/shared"];
const excludedOutputSuffixes = [".cjs.map", ".d.cts", ".d.ts", ".js.map"];

function portable(relative) {
  return relative.split(path.sep).join("/");
}

function isRuntimeModule(relative) {
  return relative.endsWith(".cjs") || relative.endsWith(".js");
}

function isExcludedCompilerOutput(relative) {
  return excludedOutputSuffixes.some((suffix) => relative.endsWith(suffix));
}

async function collectRuntimeModules(root, { compiled }) {
  const modules = [];
  for (const relativeRoot of runtimeRoots) {
    const absoluteRoot = path.join(root, relativeRoot);
    let children;
    try {
      children = await readdir(absoluteRoot, { withFileTypes: true });
    } catch (error) {
      if (error?.code === "ENOENT") {
        throw new Error(`ASAR runtime module root is missing: ${relativeRoot}`);
      }
      throw error;
    }
    await collect(relativeRoot, children);
  }
  return modules.sort((left, right) => left.localeCompare(right, "en"));

  async function collect(relativeDirectory, children) {
    children.sort((left, right) => left.name.localeCompare(right.name, "en"));
    for (const child of children) {
      const relative = portable(path.join(relativeDirectory, child.name));
      const absolute = path.join(root, relative);
      const details = await lstat(absolute);
      if (details.isSymbolicLink()) {
        throw new Error(`ASAR runtime module tree contains a link: ${relative}`);
      }
      if (details.isDirectory()) {
        await collect(
          relative,
          await readdir(absolute, { withFileTypes: true }),
        );
      } else if (!details.isFile()) {
        throw new Error(
          `ASAR runtime module tree contains a special file: ${relative}`,
        );
      } else if (isRuntimeModule(relative)) {
        modules.push(relative);
      } else if (!compiled || !isExcludedCompilerOutput(relative)) {
        throw new Error(
          `ASAR runtime module tree contains an unsupported file: ${relative}`,
        );
      }
    }
  }
}

const compiledModules = await collectRuntimeModules(desktopRoot, {
  compiled: true,
});
const packagedModules = await collectRuntimeModules(inputRoot, {
  compiled: false,
});
const compiledSet = new Set(compiledModules);
const packagedSet = new Set(packagedModules);
const missing = compiledModules.filter((entry) => !packagedSet.has(entry));
const unexpected = packagedModules.filter((entry) => !compiledSet.has(entry));
if (missing.length || unexpected.length) {
  throw new Error(
    `ASAR runtime module closure mismatch: missing=${JSON.stringify(missing)} unexpected=${JSON.stringify(unexpected)}`,
  );
}

const entries = [];
async function walk(directory) {
  const children = await readdir(directory, { withFileTypes: true });
  children.sort((left, right) => left.name.localeCompare(right.name, "en"));
  for (const child of children) {
    const absolute = path.join(directory, child.name);
    const details = await lstat(absolute);
    if (details.isSymbolicLink()) throw new Error("ASAR input contains a link");
    if (!details.isDirectory() && !details.isFile()) {
      throw new Error("ASAR input contains a special file");
    }
    entries.push(absolute);
    if (details.isDirectory()) await walk(absolute);
  }
}

await walk(inputRoot);
await mkdir(path.dirname(output), { recursive: true });
await createPackageFromFiles(inputRoot, output, entries);
const listed = listPackage(output, { isPack: false })
  .map((entry) => entry.replaceAll("\\", "/"))
  .sort();
process.stdout.write(`${JSON.stringify(listed)}\n`);
