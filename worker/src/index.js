/** azstormintel API Worker (v3.4.1)
 * azstormintel API — Cloudflare Worker.
 *
 * The site is static (GitHub Pages). This Worker turns a user's request into a GitHub Actions
 * run (.github/workflows/analyze.yml) and serves the results back from the `runs` branch.
 * It holds the only credential: a fine-grained GitHub token (secret GITHUB_TOKEN) limited to
 * this repository with "Actions: read and write" and "Contents: read".
 *
 *   GET  /api/config                     limits + preset basins (for the form)
 *   POST /api/runs                       {start_mst, end_mst, basins[]} -> {id, state}
 *   GET  /api/runs/:id                   {state: queued|running|publishing|done|failed|unknown, ...}
 *   GET  /api/runs/:id/files/<path>      a result file (meta.json, frames.json, model/..., soil.json)
 *
 * Same request => same run id, so repeated requests reuse finished results.
 */

const GH = "https://api.github.com";
const WORKFLOW = "analyze.yml";
const RUNS_BRANCH = "runs";
const MST_OFFSET_MIN = -7 * 60; // Arizona: no daylight saving

// ---------------------------------------------------------------- helpers
export function corsHeaders(req, env) {
  const origin = req.headers.get("Origin") || "";
  const allowed = (env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
  const ok = allowed.includes(origin) || /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(origin);
  return {
    "Access-Control-Allow-Origin": ok ? origin : allowed[0] || "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

const json = (obj, status, cors, extra = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...cors, ...extra } });

const TOKEN_HELP = "GitHub rejected the Worker's token: GITHUB_TOKEN is missing, mistyped or expired. " +
  "Check Worker → Settings → Variables and Secrets, then open /api/health.";

async function gh(env, path, init = {}) {
  if (!env.GITHUB_TOKEN) throw new Error("The Worker has no GITHUB_TOKEN secret. Add it under Worker → Settings → Variables and Secrets.");
  const r = await ghRaw(env, path, init);
  if (r.status === 401) throw new Error(TOKEN_HELP);
  return r;
}

function ghRaw(env, path, init = {}) {
  return fetch(GH + path, {
    ...init,
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "User-Agent": "azstormintel-worker",
      "X-GitHub-Api-Version": "2022-11-28",
      Accept: "application/vnd.github+json",
      ...(init.headers || {}),
    },
  });
}

/** Parse "YYYY-MM-DDTHH:MM" as MST -> Date (UTC instant). null if malformed. */
export function parseMst(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(String(s || ""));
  if (!m) return null;
  const [y, mo, d, h, mi] = m.slice(1).map(Number);
  const t = Date.UTC(y, mo - 1, d, h, mi) - MST_OFFSET_MIN * 60000;
  const back = new Date(t + MST_OFFSET_MIN * 60000);
  if (back.getUTCFullYear() !== y || back.getUTCMonth() !== mo - 1 || back.getUTCDate() !== d || back.getUTCHours() !== h) return null;
  return new Date(t);
}

/** Validate a request against config/limits.json. Returns {error} or {start, end, basins}. */
export function validateRequest(body, limits, basinIds, now = new Date()) {
  const start = parseMst(body.start_mst), end = parseMst(body.end_mst);
  if (!start || !end) return { error: "Start and end must look like 2026-10-30T00:00 (MST)." };
  if (start.getUTCMinutes() % 5 || end.getUTCMinutes() % 5) return { error: "Start and end must fall on 5-minute marks." };
  const hrs = (end - start) / 3.6e6;
  if (hrs <= 0) return { error: "End must be after start." };
  if (hrs < limits.min_hours) return { error: `The window must be at least ${limits.min_hours} hour(s).` };
  if (hrs > limits.max_days * 24) return { error: `The window can be at most ${limits.max_days} days.` };
  const nowMst = new Date(now.getTime() + MST_OFFSET_MIN * 60000);
  const minStart = parseMst(`${nowMst.getUTCFullYear() - limits.back_years}-01-01T00:00`);
  if (start < minStart) return { error: `Start must be on or after Jan 1, ${nowMst.getUTCFullYear() - limits.back_years}.` };
  if (end > new Date(now.getTime() - limits.latency_min * 60000))
    return { error: `End must be at least ${limits.latency_min} minutes ago (radar latency).` };
  if (body.basin) {
    const b = validateBasin(body.basin, limits);
    if (b.error) return b;
    return { start, end, basins: ["upload"], custom: b.json, start_mst: body.start_mst, end_mst: body.end_mst };
  }
  const basins = [...new Set((Array.isArray(body.basins) ? body.basins : String(body.basins || "").split(","))
    .map((b) => String(b).trim()).filter(Boolean))].sort();
  if (!basins.length) return { error: "Choose at least one basin." };
  const bad = basins.filter((b) => !basinIds.includes(b));
  if (bad.length) return { error: `Unknown basin(s): ${bad.join(", ")}` };
  return { start, end, basins, start_mst: body.start_mst, end_mst: body.end_mst };
}

/** Geodesic area (sq mi) of a GeoJSON Polygon/MultiPolygon (lon/lat, holes subtracted). */
export function areaSqmi(geom) {
  const R = 6378137, rad = Math.PI / 180;
  const ring = (c) => { let a = 0; for (let i = 0; i < c.length - 1; i++) {
    const [x1, y1] = c[i], [x2, y2] = c[i + 1]; a += (x2 - x1) * rad * (2 + Math.sin(y1 * rad) + Math.sin(y2 * rad)); }
    return Math.abs(a * R * R / 2); };
  const poly = (p) => ring(p[0]) - p.slice(1).reduce((s, h) => s + ring(h), 0);
  const polys = geom.type === "Polygon" ? [geom.coordinates] : geom.coordinates;
  return polys.reduce((s, p) => s + poly(p), 0) / 2589988.11;
}

/** Validate an uploaded basin {name, geometry, outlet, streamstats_csv}. Returns {error} or {json}. */
export function validateBasin(b, limits) {
  if (!b || typeof b !== "object") return { error: "Missing basin." };
  const g = b.geometry;
  if (!g || !["Polygon", "MultiPolygon"].includes(g.type) || !Array.isArray(g.coordinates))
    return { error: "The basin must be a polygon (StreamStats watershed)." };
  const polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates;
  const [W, S, E, N] = limits.region_bbox;
  let n = 0;
  for (const p of polys) for (const r of p) {
    if (!Array.isArray(r) || r.length < 4) return { error: "The basin polygon is malformed." };
    for (const pt of r) {
      n++;
      if (!Array.isArray(pt) || !isFinite(pt[0]) || !isFinite(pt[1])) return { error: "The basin polygon has bad coordinates." };
      if (pt[0] < W || pt[0] > E || pt[1] < S || pt[1] > N) return { error: `The basin must lie within ${limits.region_name}.` };
    }
  }
  const a = areaSqmi(g);
  if (a > limits.max_area_sqmi) return { error: `The basin is ${a.toFixed(1)} sq mi; the limit is ${limits.max_area_sqmi} sq mi.` };
  if (a < limits.min_area_sqmi) return { error: "The basin is too small to analyse." };
  let outlet = null;
  if (Array.isArray(b.outlet) && isFinite(b.outlet[0]) && isFinite(b.outlet[1])) outlet = [+(+b.outlet[0]).toFixed(6), +(+b.outlet[1]).toFixed(6)];
  const name = String(b.name || "Uploaded basin").replace(/[^\w .,()'&/-]/g, "").slice(0, 60).trim() || "Uploaded basin";
  const csv = String(b.streamstats_csv || "").slice(0, limits.max_csv_chars);
  const json = JSON.stringify({ name, geometry: g, outlet, streamstats_csv: csv });
  if (json.length > limits.max_basin_json_chars) return { error: "The basin outline is too detailed to send; simplify it and try again." };
  return { json, area: a, vertices: n };
}

/** Deterministic id: same window + basins + pipeline version -> same run. */
export async function runId(v, version) {
  const key = JSON.stringify([version, v.start_mst, v.end_mst, v.basins, v.custom || ""]);
  const h = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(key)));
  return "r" + [...h.slice(0, 7)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------- repo reads (cached)
async function repoJson(env, path, ref = "main", ttl = 600) {
  const url = `https://raw.githubusercontent.com/${env.REPO}/${ref}/${path}`;
  const r = await fetch(url, { cf: { cacheTtl: ttl, cacheEverything: true } });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

async function siteConfig(env) {
  const [limits, basins, model] = await Promise.all([
    repoJson(env, "config/limits.json"), repoJson(env, "config/basins.json"), repoJson(env, "config/model.json"),
  ]);
  return {
    limits, version: model.version,
    basins: basins.basins.map((b) => ({ id: b.id, name: b.name, outlet: b.outlet })),
  };
}

/** run.json from the runs branch via the contents API (fresh, not CDN-cached). null if absent. */
async function runRecord(env, id) {
  const r = await gh(env, `/repos/${env.REPO}/contents/runs/${id}/run.json?ref=${RUNS_BRANCH}`,
    { headers: { Accept: "application/vnd.github.raw" } });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`run.json: ${r.status}`);
  return r.json();
}

/** Most recent workflow run for this id (matched on run-name "analyze <id>"). */
async function findWorkflowRun(env, id) {
  const r = await gh(env, `/repos/${env.REPO}/actions/workflows/${WORKFLOW}/runs?event=workflow_dispatch&per_page=40`);
  if (!r.ok) throw new Error(`workflow runs: ${r.status}`);
  const runs = (await r.json()).workflow_runs || [];
  return runs.find((w) => w.display_title === `analyze ${id}`) || null;
}

async function activeRuns(env) {
  let n = 0;
  for (const st of ["queued", "in_progress"]) {
    const r = await gh(env, `/repos/${env.REPO}/actions/workflows/${WORKFLOW}/runs?status=${st}&per_page=1`);
    if (r.ok) n += (await r.json()).total_count || 0;
  }
  return n;
}

async function rateLimited(env, ip, perHour) {
  if (!env.RATE) return false; // optional KV namespace binding
  const key = `ip:${ip}:${new Date().toISOString().slice(0, 13)}`;
  const n = parseInt((await env.RATE.get(key)) || "0", 10);
  if (n >= perHour) return true;
  await env.RATE.put(key, String(n + 1), { expirationTtl: 3700 });
  return false;
}

// ---------------------------------------------------------------- handlers
async function status(env, id) {
  const rec = await runRecord(env, id);
  const wr = await findWorkflowRun(env, id).catch(() => null);
  const active = wr && wr.status !== "completed";
  if (rec && !(active && !rec.ok)) return { id, state: rec.ok ? "done" : "failed", run: rec };
  if (!wr) return { id, state: "unknown" };
  const out = { id, state: wr.status === "queued" ? "queued" : "running", started_utc: wr.run_started_at, log_url: wr.html_url };
  if (wr.status === "completed") out.state = "publishing";
  const j = await gh(env, `/repos/${env.REPO}/actions/runs/${wr.id}/jobs`);
  if (j.ok) {
    const job = ((await j.json()).jobs || [])[0];
    if (job) out.steps = (job.steps || [])
      .filter((s) => !/^(Set up job|Post |Complete job|Run actions\/)/.test(s.name))
      .map((s) => ({ name: s.name, status: s.status, conclusion: s.conclusion, started: s.started_at, completed: s.completed_at }));
  }
  if (wr.status === "completed" && wr.conclusion !== "success" && !rec) {
    out.state = "failed"; out.run = { ok: false, error: `The workflow ${wr.conclusion}.` };
  }
  return out;
}

const TYPES = { json: "application/json", png: "image/png", csv: "text/csv", txt: "text/plain; charset=utf-8", gz: "application/gzip" };

async function file(req, env, id, path, cors) {
  if (!/^[A-Za-z0-9_\-./]+$/.test(path) || path.includes("..")) return json({ error: "bad path" }, 400, cors);
  const cache = caches.default;
  const key = new Request(new URL(req.url).toString(), { method: "GET" });
  const hit = await cache.match(key);
  if (hit) {
    const h = new Headers(hit.headers); Object.entries(cors).forEach(([k, v]) => h.set(k, v));
    return new Response(hit.body, { status: hit.status, headers: h });
  }
  const r = await gh(env, `/repos/${env.REPO}/contents/runs/${id}/${path}?ref=${RUNS_BRANCH}`,
    { headers: { Accept: "application/vnd.github.raw" } });
  if (!r.ok) return json({ error: `not found: ${path}` }, r.status === 404 ? 404 : 502, cors);
  const ext = path.split(".").pop().toLowerCase();
  const res = new Response(r.body, {
    headers: { "Content-Type": TYPES[ext] || "application/octet-stream", "Cache-Control": "public, max-age=3600" },
  });
  await cache.put(key, res.clone());
  const h = new Headers(res.headers); Object.entries(cors).forEach(([k, v]) => h.set(k, v));
  return new Response(res.body, { headers: h });
}

export default {
  async fetch(req, env) {
    const cors = corsHeaders(req, env);
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    const url = new URL(req.url);
    const p = url.pathname.replace(/\/+$/, "");
    try {
      if (p === "/api/health" && req.method === "GET") {
        // Diagnoses the token without exposing it.
        const t = env.GITHUB_TOKEN || "";
        const out = { repo: env.REPO, token_set: !!t, token_length: t.length,
          token_has_whitespace_or_quotes: /[\s"']/.test(t), token_prefix: t.slice(0, 11) };
        if (t) {
          const a = await ghRaw(env, `/repos/${env.REPO}`);
          out.repo_access = a.status === 200 ? "ok" : `HTTP ${a.status}${a.status === 401 ? " (token rejected)" : a.status === 404 ? " (token cannot see this repo)" : ""}`;
          const w = await ghRaw(env, `/repos/${env.REPO}/actions/workflows/${WORKFLOW}`);
          out.actions_access = w.status === 200 ? "ok" : `HTTP ${w.status}`;
          const b = await ghRaw(env, `/repos/${env.REPO}/contents/config/limits.json?ref=main`);
          out.contents_access = b.status === 200 ? "ok" : `HTTP ${b.status}`;
        }
        out.ok = out.repo_access === "ok" && out.actions_access === "ok" && out.contents_access === "ok";
        return json(out, 200, cors, { "Cache-Control": "no-store" });
      }
      if (p === "/api/config" && req.method === "GET") {
        return json(await siteConfig(env), 200, cors, { "Cache-Control": "public, max-age=300" });
      }
      if (p === "/api/runs" && req.method === "POST") {
        let body;
        const txt = await req.text();
        if (txt.length > 200000) return json({ error: "Request too large." }, 413, cors);
        try { body = JSON.parse(txt); } catch { return json({ error: "Body must be JSON." }, 400, cors); }
        const cfg = await siteConfig(env);
        const v = validateRequest(body, cfg.limits, cfg.basins.map((b) => b.id));
        if (v.error) return json({ error: v.error }, 400, cors);
        const id = await runId(v, cfg.version);
        const st = await status(env, id);
        if (["done", "queued", "running", "publishing"].includes(st.state)) return json(st, 200, cors);
        const ip = req.headers.get("CF-Connecting-IP") || "unknown";
        if (await rateLimited(env, ip, parseInt(env.RUNS_PER_IP_HOUR || "6", 10)))
          return json({ error: "Too many new analyses from this address; try again within the hour." }, 429, cors);
        if ((await activeRuns(env)) >= cfg.limits.max_concurrent_runs)
          return json({ error: "The analysis queue is full. Please try again in a few minutes." }, 429, cors);
        const d = await gh(env, `/repos/${env.REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ref: env.REF || "main", inputs: { run_id: id, start_mst: v.start_mst, end_mst: v.end_mst, basins: v.custom ? "" : v.basins.join(","), custom_basin: v.custom || "" } }),
        });
        if (d.status !== 204 && d.status !== 200) return json({ error: `Could not start the analysis (GitHub ${d.status}).` }, 502, cors);
        return json({ id, state: "queued" }, 202, cors);
      }
      let m = /^\/api\/runs\/(r[0-9a-f]{14}|[a-z0-9][a-z0-9-]{5,47})$/.exec(p);
      if (m && req.method === "GET") return json(await status(env, m[1]), 200, cors, { "Cache-Control": "no-store" });
      m = /^\/api\/runs\/([a-z0-9][a-z0-9-]{5,47})\/files\/(.+)$/.exec(p);
      if (m && req.method === "GET") return file(req, env, m[1], m[2], cors);
      return json({ error: "not found" }, 404, cors);
    } catch (e) {
      return json({ error: String(e.message || e) }, 500, cors);
    }
  },
};
