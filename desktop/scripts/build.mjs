import { cp, mkdir, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outputRoot = path.join(desktopRoot, "dist");

if (process.argv.includes("--clean")) {
  await rm(outputRoot, { recursive: true, force: true });
  process.exit(0);
}

await mkdir(outputRoot, { recursive: true });
await cp(path.join(desktopRoot, "src/main/shell"), path.join(outputRoot, "shell"), {
  recursive: true,
  force: true,
});
await cp(path.join(desktopRoot, "assets/icon.png"), path.join(outputRoot, "icon.png"));
await cp(path.join(desktopRoot, "assets/icon.png"), path.join(outputRoot, "shell/icon.png"));
