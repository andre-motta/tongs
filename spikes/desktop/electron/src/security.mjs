import { URL } from "node:url";

export const RPC_CHANNEL = "tongs:invoke";
export const ALLOWED_METHODS = new Set([
  "health",
  "list_reviews",
  "get_diff",
  "list_plugins",
  "plugin_invoke",
  "plugin_help",
]);

const MAX_PARAMS_BYTES = 64 * 1024;

export function validateAssetUrl(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("Sidecar returned an invalid asset URL");
  }
  if (
    parsed.protocol !== "http:" ||
    parsed.hostname !== "127.0.0.1" ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("Sidecar asset URL must be an unadorned IPv4 loopback origin");
  }
  return parsed;
}

export function validateInvocation(method, params = {}) {
  if (typeof method !== "string" || !ALLOWED_METHODS.has(method)) {
    throw new Error("Unsupported desktop method");
  }
  if (
    params === null ||
    typeof params !== "object" ||
    Array.isArray(params) ||
    ![Object.prototype, null].includes(Object.getPrototypeOf(params))
  ) {
    throw new Error("Desktop method parameters must be a plain object");
  }
  let encoded;
  try {
    encoded = JSON.stringify(params);
  } catch {
    throw new Error("Desktop method parameters must be JSON-compatible");
  }
  if (encoded === undefined || Buffer.byteLength(encoded) > MAX_PARAMS_BYTES) {
    throw new Error("Desktop method parameters are too large");
  }
  return { method, params };
}

export function validateRenderer(event, expectedWebContents, expectedOrigin) {
  if (event.sender !== expectedWebContents) {
    throw new Error("Desktop request came from an unexpected renderer");
  }
  if (!event.senderFrame || event.senderFrame !== expectedWebContents.mainFrame) {
    throw new Error("Desktop request came from a subframe");
  }
  let frameUrl;
  try {
    frameUrl = new URL(event.senderFrame.url);
  } catch {
    throw new Error("Desktop request came from an invalid frame URL");
  }
  if (frameUrl.origin !== expectedOrigin) {
    throw new Error("Desktop request came from an unexpected origin");
  }
}

export function isAllowedNavigation(target, expectedOrigin) {
  try {
    const parsed = new URL(target);
    return parsed.origin === expectedOrigin && parsed.pathname === "/";
  } catch {
    return false;
  }
}

export const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "base-uri 'none'",
  "connect-src 'self'",
  "font-src 'self'",
  "form-action 'none'",
  "frame-ancestors 'none'",
  "img-src 'self' data:",
  "object-src 'none'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
].join("; ");
