import { afterEach, describe, expect, it, vi } from "vitest";
import { waitForBridge } from "../src/bridge";
import type { Bridge } from "../src/types";

type Listener = () => void;

afterEach(() => {
  delete (globalThis as { window?: unknown }).window;
});

describe("delayed native bridge", () => {
  it("notifies the app when tongs-ready arrives after initial page load", () => {
    const listeners = new Map<string, Listener>();
    const fakeWindow: {
      tongs?: Bridge;
      addEventListener: (name: string, listener: Listener) => void;
      removeEventListener: ReturnType<typeof vi.fn>;
      location: { search: string };
    } = {
      tongs: undefined,
      addEventListener: (name: string, listener: Listener) => listeners.set(name, listener),
      removeEventListener: vi.fn(),
      location: { search: "" },
    };
    (globalThis as { window?: unknown }).window = fakeWindow;
    const onReady = vi.fn();
    const cleanup = waitForBridge(onReady);
    const bridge: Bridge = { invoke: vi.fn() };

    fakeWindow.tongs = bridge;
    listeners.get("tongs-ready")?.();

    expect(onReady).toHaveBeenCalledWith(bridge);
    cleanup();
    expect(fakeWindow.removeEventListener).toHaveBeenCalledWith("tongs-ready", expect.any(Function));
  });
});
