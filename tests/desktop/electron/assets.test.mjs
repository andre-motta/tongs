import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { AssetCatalog } from "../../../desktop/dist/src/main/assets.js";
class FakeTransport {
  constructor(bytes, descriptor = {}) { this.bytes = bytes; this.descriptor = descriptor; }
  requestRead(method, params) {
    if (method === "assets.list") return { requestId: "1", result: Promise.resolve({ assets: [{ handle: "opaque", source: "plugin", asset_id: "entry", plugin_id: "sample", kind: "module", media_type: "text/javascript", byte_count: this.bytes.length, sha256: createHash("sha256").update(this.bytes).digest("hex"), ...this.descriptor }] }) };
    const chunk = this.bytes.subarray(params.offset, params.offset + params.length);
    return { requestId: "2", result: Promise.resolve({ handle: "opaque", offset: params.offset, next_offset: params.offset + chunk.length === this.bytes.length ? null : params.offset + chunk.length, data_base64: chunk.toString("base64") }) };
  }
}
test("catalog exposes fixed URLs and verifies streamed bytes", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "tongs-assets-"));
  await Promise.all([writeFile(path.join(root, "index.html"), "safe"), writeFile(path.join(root, "app.js"), "safe"), writeFile(path.join(root, "styles.css"), "safe"), writeFile(path.join(root, "icon.png"), "safe")]);
  const bytes = Buffer.from("export const safe = true;", "utf8"); const catalog = new AssetCatalog(new FakeTransport(bytes), root); const refresh = catalog.beginRefresh();
  assert.equal(refresh.requestId, "1"); const descriptors = await refresh.result;
  assert.equal(descriptors[0].url, "tongs://app/assets/opaque"); assert.equal("handle" in descriptors[0], false);
  const served = await catalog.response(descriptors[0].url); assert.equal(await served.text(), bytes.toString()); assert.equal(served.headers.get("x-content-type-options"), "nosniff");
  assert.equal((await catalog.response("tongs://app/%2e%2e/secret")).status, 404); assert.equal((await catalog.response("https://app/index.html")).status, 404);
});
test("catalog rejects wrong MIME", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "tongs-assets-")); const transport = new FakeTransport(Buffer.from("x"));
  transport.requestRead = () => ({ requestId: "1", result: Promise.resolve({ assets: [{ handle: "x", source: "plugin", asset_id: "x", plugin_id: "p", kind: "module", media_type: "text/html", byte_count: 1, sha256: "0".repeat(64) }] }) });
  await assert.rejects(new AssetCatalog(transport, root).refresh());
});
test("catalog accepts only exact S4 plugin resource media types", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "tongs-assets-"));
  for (const [kind, media_type] of [["module", "text/javascript; charset=utf-8"], ["stylesheet", "text/css; charset=utf-8"], ["help", "text/markdown; charset=utf-8"]]) {
    const bytes = Buffer.from(`${kind} content`, "utf8");
    const catalog = new AssetCatalog(new FakeTransport(bytes, { kind, media_type }), root); const [descriptor] = await catalog.refresh(); const served = await catalog.response(descriptor.url);
    assert.equal(served.status, 200); assert.equal(served.headers.get("content-type"), media_type);
  }
  const bytes = Buffer.from("help", "utf8");
  await assert.rejects(new AssetCatalog(new FakeTransport(bytes, { kind: "help", media_type: "text/markdown" }), root).refresh());
});
test("bundled React renderer is served only through the fixed shell route", async () => {
  const shellRoot = path.resolve(import.meta.dirname, "../../../desktop/dist/shell");
  const catalog = new AssetCatalog(new FakeTransport(Buffer.from("unused")), shellRoot);
  const served = await catalog.response("tongs://app/app.js");
  assert.equal(served.status, 200);
  assert.match(served.headers.get("content-type"), /text\/javascript/);
  assert.match(await served.text(), /createRoot|createElement/);
  assert.equal((await catalog.response("tongs://app/renderer/index.js")).status, 404);
});
