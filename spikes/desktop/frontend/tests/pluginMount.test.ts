import { describe, expect, it, vi } from "vitest";
import { mountPluginModule, type PluginLoader } from "../src/App";
import type { PluginApi } from "../src/types";

const container = {} as HTMLElement;
const api: PluginApi = { invoke: vi.fn() };

describe("runtime plugin module mounting", () => {
  it("rejects a module without the required mount export", async () => {
    const loader: PluginLoader = vi.fn(async () => ({}));

    await expect(mountPluginModule("/plugins/missing.js", container, api, loader)).rejects.toThrow(
      "does not export mount",
    );
  });

  it("returns the plugin cleanup function after a successful mount", async () => {
    const cleanup = vi.fn();
    const mount = vi.fn(() => cleanup);
    const loader: PluginLoader = vi.fn(async () => ({ mount }));

    await expect(mountPluginModule("/plugins/ready.js", container, api, loader)).resolves.toBe(cleanup);
    expect(mount).toHaveBeenCalledWith(container, api);
  });
});
