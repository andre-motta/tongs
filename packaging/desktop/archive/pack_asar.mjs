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
