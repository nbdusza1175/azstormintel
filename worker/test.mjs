// Offline test of the Worker with a mocked GitHub (node test.mjs)
import assert from "node:assert";
import fs from "node:fs";
import W, { validateRequest, parseMst, runId, areaSqmi } from "./src/index.js";

const root = new URL("../", import.meta.url);
const read = (p) => fs.readFileSync(new URL(p, root), "utf8");
const limits = JSON.parse(read("config/limits.json"));
const now = new Date("2026-11-16T16:00:00Z");
// --- validation
assert.equal(parseMst("2026-10-30T00:00").toISOString(), "2026-10-30T07:00:00.000Z");
assert.equal(parseMst("2026-02-30T00:00"), null);
const ids = ["fort_valley", "mountain_dell"];
const ok = validateRequest({ start_mst: "2026-10-30T00:00", end_mst: "2026-11-03T00:00", basins: ["mountain_dell", "fort_valley"] }, limits, ids, now);
assert.ok(!ok.error, ok.error); assert.deepEqual(ok.basins, ids);
assert.match(validateRequest({ start_mst: "2015-12-31T23:00", end_mst: "2016-01-01T05:00", basins: ids }, limits, ids, now).error, /Jan 1, 2016/);
assert.ok(!validateRequest({ start_mst: "2016-01-01T00:00", end_mst: "2016-01-02T00:00", basins: ids }, limits, ids, now).error);
assert.match(validateRequest({ start_mst: "2026-10-01T00:00", end_mst: "2026-10-09T00:00", basins: ids }, limits, ids, now).error, /at most 7/);
assert.match(validateRequest({ start_mst: "2026-11-16T07:00", end_mst: "2026-11-16T08:55", basins: ids }, limits, ids, now).error, /minutes ago/);
assert.match(validateRequest({ start_mst: "2026-10-30T00:03", end_mst: "2026-10-31T00:00", basins: ids }, limits, ids, now).error, /5-minute/);
assert.match(validateRequest({ start_mst: "2026-10-30T00:00", end_mst: "2026-10-31T00:00", basins: ["x"] }, limits, ids, now).error, /Unknown/);
const id1 = await runId(ok, "3.4.0"), id2 = await runId({ ...ok }, "3.4.0"), id3 = await runId(ok, "3.5.0");
assert.equal(id1, id2); assert.notEqual(id1, id3); assert.match(id1, /^r[0-9a-f]{14}$/);

// --- uploaded basin
const up = JSON.parse(read("tests/fixtures/upload_fv_auto_outlet.json"));
assert.ok(Math.abs(areaSqmi(up.geometry) - 29.0) < 0.6, "area " + areaSqmi(up.geometry));
const vb = validateRequest({ start_mst: "2026-11-01T05:00", end_mst: "2026-11-01T08:00", basin: up }, limits, ids, now);
assert.ok(!vb.error, vb.error); assert.deepEqual(vb.basins, ["upload"]); assert.ok(vb.custom.length < limits.max_basin_json_chars);
const vbOut = validateRequest({ start_mst: "2026-11-01T05:00", end_mst: "2026-11-01T08:00", basin: { ...up, geometry: { type: "Polygon", coordinates: [[[-100, 40], [-100.1, 40], [-100.1, 40.1], [-100, 40]]] } } }, limits, ids, now);
assert.match(vbOut.error, /Arizona/);
const vbPt = validateRequest({ start_mst: "2026-11-01T05:00", end_mst: "2026-11-01T08:00", basin: { geometry: { type: "Point", coordinates: [-111, 35] } } }, limits, ids, now);
assert.match(vbPt.error, /polygon/);
assert.notEqual(await runId(vb, "3.4.0"), await runId({ ...vb, custom: vb.custom.replace("Rio", "Rix") }, "3.4.0"));

// --- request flow with mocked GitHub
const files = { "config/limits.json": read("config/limits.json"), "config/basins.json": read("config/basins.json"), "config/model.json": read("config/model.json") };
let dispatched = null, published = {}, wruns = [];
globalThis.caches = { default: { match: async () => null, put: async () => {} } };
globalThis.fetch = async (url, init = {}) => {
  url = String(url);
  let m = /raw\.githubusercontent\.com\/[^/]+\/[^/]+\/main\/(.+)$/.exec(url);
  if (m) return new Response(files[m[1]], { status: 200 });
  m = /\/contents\/runs\/([^/]+)\/(.+)\?ref=runs$/.exec(url);
  if (m) { const k = m[1] + "/" + m[2]; return k in published ? new Response(published[k]) : new Response("{}", { status: 404 }); }
  if (/\/dispatches$/.test(url)) { dispatched = JSON.parse(init.body); wruns.unshift({ id: 9, display_title: "analyze " + dispatched.inputs.run_id, status: "queued", html_url: "x" }); return new Response(null, { status: 204 }); }
  if (/\/runs\?status=/.test(url)) return Response.json({ total_count: 0 });
  if (/\/runs\?event=/.test(url)) return Response.json({ workflow_runs: wruns });
  if (/\/runs\/9\/jobs$/.test(url)) return Response.json({ jobs: [{ steps: [{ name: "Set up job", status: "completed" }, { name: "Radar rainfall (MRMS)", status: "in_progress", conclusion: null }] }] });
  throw new Error("unexpected " + url);
};
const env = { REPO: "o/r", GITHUB_TOKEN: "t", ALLOWED_ORIGINS: "https://azstormintel.com" };
const call = (path, init) => W.fetch(new Request("https://api.example" + path, { headers: { Origin: "https://azstormintel.com", "Content-Type": "application/json" }, ...init }), env);
let r = await call("/api/config"); let j = await r.json();
assert.equal(j.limits.max_days, 7); assert.equal(j.basins.length, 2);
assert.equal(r.headers.get("Access-Control-Allow-Origin"), "https://azstormintel.com");
const body = JSON.stringify({ start_mst: "2026-10-30T00:00", end_mst: "2026-11-03T00:00", basins: ["fort_valley"] });
r = await call("/api/runs", { method: "POST", body }); j = await r.json();
assert.equal(r.status, 202, JSON.stringify(j)); const id = j.id; assert.equal(dispatched.inputs.basins, "fort_valley");
r = await call("/api/runs", { method: "POST", body }); j = await r.json();
assert.equal(j.state, "queued"); // dedupe: no second dispatch
wruns[0].status = "in_progress";
j = await (await call("/api/runs/" + id)).json(); assert.equal(j.state, "running"); assert.equal(j.steps.length, 1);
published[id + "/run.json"] = JSON.stringify({ ok: true, name: "x" }); published[id + "/meta.json"] = '{"a":1}'; wruns[0].status = "completed"; wruns[0].conclusion = "success";
j = await (await call("/api/runs/" + id)).json(); assert.equal(j.state, "done");
r = await call(`/api/runs/${id}/files/meta.json`); assert.equal(r.headers.get("Content-Type"), "application/json"); assert.deepEqual(await r.json(), { a: 1 });
r = await call(`/api/runs/${id}/files/../secret`); assert.equal(r.status, 404);
r = await call("/api/runs", { method: "POST", body: JSON.stringify({ start_mst: "2026-11-01T05:00", end_mst: "2026-11-01T08:00", basin: up }) });
j = await r.json(); assert.equal(r.status, 202, JSON.stringify(j)); assert.equal(dispatched.inputs.basins, ""); assert.ok(JSON.parse(dispatched.inputs.custom_basin).geometry);
r = await call("/api/runs", { method: "POST", body: JSON.stringify({ start_mst: "2010-01-01T00:00", end_mst: "2010-01-02T00:00", basins: ["fort_valley"] }) });
assert.equal(r.status, 400);
console.log("worker tests passed; id", id);
