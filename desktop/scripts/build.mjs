import { cp, mkdir, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const outputRoot = path.join(desktopRoot, "dist");

if (process.argv.includes("--clean") || process.argv.includes("--prepare")) {
  await rm(outputRoot, { recursive: true, force: true });
  process.exit(0);
}

await mkdir(outputRoot, { recursive: true });
await rm(path.join(outputRoot, "shell"), { recursive: true, force: true });
await cp(path.join(desktopRoot, "src/main/shell"), path.join(outputRoot, "shell"), {
  recursive: true,
  force: true,
});
await build({
  entryPoints: [path.join(desktopRoot, "src/renderer/app.tsx")],
  outfile: path.join(outputRoot, "shell/app.js"),
  bundle: true,
  format: "esm",
  platform: "browser",
  target: "chrome142",
  minify: true,
  sourcemap: false,
  define: { "process.env.NODE_ENV": '"production"' },
});
await cp(path.join(desktopRoot, "assets/icon.png"), path.join(outputRoot, "icon.png"));
await cp(path.join(desktopRoot, "assets/icon.png"), path.join(outputRoot, "shell/icon.png"));
