import { createHash } from "node:crypto";
import { lstat, readFile, realpath } from "node:fs/promises";
import path from "node:path";
import type { AssetDescriptor, JsonObject, JsonValue } from "../shared/bridge.js";
import type { SidecarTransport } from "./sidecar.js";
import { CONTENT_SECURITY_POLICY } from "./security.js";

const MAX_ASSET_BYTES = 16 * 1024 * 1024;
const ASSET_CHUNK_BYTES = 512 * 1024;
const SHELL_FILES = new Map([
  ["/", ["index.html", "text/html; charset=utf-8"]],
  ["/index.html", ["index.html", "text/html; charset=utf-8"]],
  ["/app.js", ["app.js", "text/javascript; charset=utf-8"]],
  ["/styles.css", ["styles.css", "text/css; charset=utf-8"]],
  ["/icon.png", ["icon.png", "image/png"]],
] as const);
const ALLOWED_MEDIA = new Set(["text/css", "text/javascript", "application/javascript", "image/png", "image/svg+xml", "font/woff2", "application/json"]);

interface WireAsset extends Omit<AssetDescriptor, "url"> { readonly handle: string; }
interface AssetChunk extends JsonObject { readonly handle: string; readonly offset: number; readonly next_offset: number | null; readonly data_base64: string; }
interface AssetRefresh { readonly requestId: string; readonly result: Promise<readonly AssetDescriptor[]>; }

export class AssetCatalog {
  private byPath = new Map<string, WireAsset>();
  constructor(private readonly transport: SidecarTransport, private readonly shellRoot: string) {}

  beginRefresh(): AssetRefresh {
    const request = this.transport.requestRead<JsonObject>("assets.list", {});
    return {
      requestId: request.requestId,
      result: request.result.then((result) => this.install(result)),
    };
  }

  async refresh(): Promise<readonly AssetDescriptor[]> {
    return this.beginRefresh().result;
  }

  private install(result: JsonObject): readonly AssetDescriptor[] {
    if (!Array.isArray(result.assets) || result.assets.length > 1024) throw new Error("Invalid asset catalog");
    const next = new Map<string, WireAsset>();
    const publicItems: AssetDescriptor[] = [];
    for (const value of result.assets) {
      const asset = validateDescriptor(value);
      const route = `/assets/${encodeURIComponent(asset.handle)}`;
      if (next.has(route)) throw new Error("Duplicate asset handle");
      next.set(route, asset);
      publicItems.push(Object.freeze({
        source: asset.source, asset_id: asset.asset_id, plugin_id: asset.plugin_id,
        kind: asset.kind, media_type: asset.media_type, byte_count: asset.byte_count,
        sha256: asset.sha256, url: `tongs://app${route}`,
      }));
    }
    this.byPath = next;
    return Object.freeze(publicItems);
  }

  async response(url: string): Promise<Response> {
    const parsed = new URL(url);
    if (parsed.protocol !== "tongs:" || parsed.host !== "app" || parsed.search || parsed.hash || /%2f|%5c|%00/i.test(parsed.pathname)) return response("Not found", 404, "text/plain");
    const shell = (SHELL_FILES as ReadonlyMap<string, readonly [string, string]>).get(parsed.pathname);
    if (shell) return this.shellResponse(shell[0], shell[1]);
    const asset = this.byPath.get(parsed.pathname);
    if (!asset) return response("Not found", 404, "text/plain");
    const bytes = await this.readAsset(asset);
    return response(bytes, 200, asset.media_type);
  }

  private async shellResponse(name: string, mediaType: string): Promise<Response> {
    const root = await realpath(this.shellRoot);
    const candidate = path.join(root, name);
    const info = await lstat(candidate);
    const resolved = await realpath(candidate);
    const relative = path.relative(root, resolved);
    if (!info.isFile() || info.isSymbolicLink() || resolved !== path.resolve(candidate) || relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("Invalid bundled shell asset");
    return response(await readFile(resolved), 200, mediaType);
  }

  private async readAsset(asset: WireAsset): Promise<Buffer> {
    const chunks: Buffer[] = [];
    let offset = 0;
    while (offset < asset.byte_count) {
      const request = this.transport.requestRead<AssetChunk>("assets.read", { asset: asset.handle, offset, length: Math.min(ASSET_CHUNK_BYTES, asset.byte_count - offset) });
      const chunk = await request.result;
      if (chunk.handle !== asset.handle || chunk.offset !== offset || typeof chunk.data_base64 !== "string") throw new Error("Invalid asset chunk");
      const bytes = Buffer.from(chunk.data_base64, "base64");
      if (bytes.toString("base64") !== chunk.data_base64 || bytes.length === 0 || bytes.length > ASSET_CHUNK_BYTES) throw new Error("Invalid asset encoding");
      chunks.push(bytes);
      offset += bytes.length;
      if (chunk.next_offset !== (offset === asset.byte_count ? null : offset)) throw new Error("Invalid asset offset");
    }
    const value = Buffer.concat(chunks);
    if (value.length !== asset.byte_count || createHash("sha256").update(value).digest("hex") !== asset.sha256) throw new Error("Asset integrity check failed");
    return value;
  }
}

function validateDescriptor(value: JsonValue): WireAsset {
  if (!isRecord(value)) throw new Error("Invalid asset descriptor");
  const text = (key: string, max: number): string => {
    const item = value[key]; if (typeof item !== "string" || item.length === 0 || item.length > max) throw new Error("Invalid asset descriptor"); return item;
  };
  const source = text("source", 20);
  const media_type = text("media_type", 100);
  const byte_count = value.byte_count;
  const plugin = value.plugin_id;
  if ((source !== "core" && source !== "plugin") || !ALLOWED_MEDIA.has(media_type) || !Number.isSafeInteger(byte_count) || Number(byte_count) < 0 || Number(byte_count) > MAX_ASSET_BYTES || (plugin !== null && (typeof plugin !== "string" || plugin.length > 80))) throw new Error("Invalid asset descriptor");
  const sha256 = text("sha256", 64);
  if (!/^[a-f0-9]{64}$/.test(sha256)) throw new Error("Invalid asset digest");
  return Object.freeze({ handle: text("handle", 100), source, asset_id: text("asset_id", 160), plugin_id: plugin as string | null, kind: text("kind", 40), media_type, byte_count: Number(byte_count), sha256 }) as WireAsset;
}

function response(body: BodyInit | Buffer, status: number, mediaType: string): Response {
  const safeBody = Buffer.isBuffer(body) ? new Uint8Array(body) : body;
  return new Response(safeBody, { status, headers: { "content-type": mediaType, "content-security-policy": CONTENT_SECURITY_POLICY, "x-content-type-options": "nosniff", "cache-control": "no-store" } });
}
function isRecord(value: unknown): value is Record<string, JsonValue> { return value !== null && typeof value === "object" && !Array.isArray(value); }
