import { fixtureBridge } from "./fixture";
import type { Bridge } from "./types";

export function isBrowserDemoEnabled(): boolean {
  return typeof window !== "undefined" && new URLSearchParams(window.location.search).get("demo") === "1";
}

export function getAvailableBridge(): Bridge | undefined {
  return typeof window !== "undefined" ? window.tongs : undefined;
}

export function invoke(
  method: string,
  params?: Record<string, unknown>,
  bridge: Bridge | undefined = getAvailableBridge(),
): Promise<unknown> {
  if (bridge) return bridge.invoke(method, params);
  if (isBrowserDemoEnabled()) return fixtureBridge.invoke(method, params);
  return Promise.reject(new Error("Desktop bridge is not ready. Start a shell or open with ?demo=1."));
}

export function waitForBridge(onReady: (bridge: Bridge) => void): () => void {
  const existing = getAvailableBridge();
  if (existing) onReady(existing);

  const listener = () => {
    const ready = getAvailableBridge();
    if (ready) onReady(ready);
  };
  window.addEventListener("tongs-ready", listener);
  return () => window.removeEventListener("tongs-ready", listener);
}
