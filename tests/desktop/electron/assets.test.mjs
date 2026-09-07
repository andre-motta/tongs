import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { AssetCatalog } from "../../../desktop/dist/src/main/assets.js";
class FakeTransport {
  constructor(bytes) { this.bytes = bytes; }
  requestRead(method, params) {
    if (method === "assets.list") return { requestId: "1", result: Promise.resolve({ assets: [{ handle: "opaque", source: "plugin", asset_id: "entry", plugin_id: "sample", kind: "module", media_type: "text/javascript", byte_count: this.bytes.length, sha256: createHash("sha256").update(this.bytes).digest("hex") }] }) };
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
