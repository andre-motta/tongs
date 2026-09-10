import { accessSync, constants, realpathSync, statSync } from "node:fs";
import path from "node:path";

export interface DesktopLaunchConfig {
  readonly pythonExecutable: string;
  readonly coreVersion: string;
  readonly safeCwd: string;
  readonly utilityExportRoot?: string;
}

export const SIDECAR_ARGUMENTS = Object.freeze([
  "-E",
  "-P",
  "-m",
  "tongs.desktop.sidecar",
] as const);

export function validateLaunchConfig(
  value: DesktopLaunchConfig,
): DesktopLaunchConfig {
  if (!path.isAbsolute(value.pythonExecutable)) {
    throw new Error("The desktop Python interpreter must be an absolute path");
  }
  accessSync(value.pythonExecutable, constants.X_OK);
  if (!statSync(value.pythonExecutable).isFile()) {
    throw new Error(
      "The desktop Python interpreter must be an executable file",
    );
  }
  if (!value.coreVersion || value.coreVersion.length > 200) {
    throw new Error("The desktop core version is invalid");
  }
  if (!path.isAbsolute(value.safeCwd)) {
    throw new Error("The desktop safe working directory must be absolute");
  }
  const safeCwd = realpathSync(value.safeCwd);
  if (!statSync(safeCwd).isDirectory()) {
    throw new Error("The desktop safe working directory must exist");
  }
  if (
    value.utilityExportRoot !== undefined &&
    (!path.isAbsolute(value.utilityExportRoot) ||
      value.utilityExportRoot.length > 4096 ||
      value.utilityExportRoot.includes("\0"))
  ) {
    throw new Error("The desktop utility export root is invalid");
  }
  return Object.freeze({ ...value, safeCwd });
}

export function parseLaunchArguments(
  argv: readonly string[],
): DesktopLaunchConfig {
  const values = new Map<string, string>();
  const allowed = new Set([
    "--tongs-python-executable",
    "--tongs-core-version",
    "--tongs-safe-cwd",
    "--tongs-smoke-report",
    "--tongs-smoke-review-number",
    "--tongs-smoke-source-commit",
  ]);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!argument?.startsWith("--tongs-")) continue;
    const separator = argument.indexOf("=");
    const name = separator >= 0 ? argument.slice(0, separator) : argument;
    if (!allowed.has(name) || values.has(name))
      throw new Error("Invalid desktop launch argument");
    if (separator >= 0) {
      values.set(name, argument.slice(separator + 1));
      continue;
    }
    const next = argv[index + 1];
    if (!next || next.startsWith("--"))
      throw new Error("Missing desktop launch value");
    values.set(argument, next);
    index += 1;
  }
  const pythonExecutable = values.get("--tongs-python-executable");
  const coreVersion = values.get("--tongs-core-version");
  const safeCwd = values.get("--tongs-safe-cwd");
  if (!pythonExecutable || !coreVersion || !safeCwd) {
    throw new Error("The trusted desktop launch configuration is incomplete");
  }
  return validateLaunchConfig({ pythonExecutable, coreVersion, safeCwd });
}
