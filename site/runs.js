/* AZ Storm Intel — on-demand analyses ("New analysis").
 * Talks to the API Worker (worker/), which starts a GitHub Actions run and serves its results.
 * Loaded before app.js; uses window.AZApp (set by app.js) once the page is up. */
(function () {
  "use strict";
  const CFG = window.AZSTORM_CONFIG || {};
  const API = (CFG.api || "").replace(/\/+$/, "");
  const $ = (id) => document.getElementById(id);
  const KEY = "azstorm.runs";
  const esc = (s) => String(s == null ? "" : s).replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
  let apiCfg = null, pollTimer = null, tickTimer = null;

  // ---- saved runs (this browser only) ----------------------------------------------------
  function saved() { try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return []; } }
  function remember(id, name) {
    try {
      const list = saved().filter((r) => r.id !== id);
      list.unshift({ id, name, saved_utc: new Date().toISOString() });
      localStorage.setItem(KEY, JSON.stringify(list.slice(0, 25)));
    } catch (e) { /* storage unavailable */ }
  }
  const base = (id) => `${API}/api/runs/${id}/files/`;

  // ---- MST helpers (Arizona has no daylight saving) ----------------------------------------
  const pad = (n) => String(n).padStart(2, "0");
  function mstString(d) { const m = new Date(d.getTime() - 7 * 3600e3); return `${m.getUTCFullYear()}-${pad(m.getUTCMonth() + 1)}-${pad(m.getUTCDate())}T${pad(m.getUTCHours())}:${pad(m.getUTCMinutes())}`; }
  function parseMst(s) { const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s || ""); return m ? new Date(Date.UTC(+m[1], m[2] - 1, +m[3], +m[4] + 7, +m[5])) : null; }
  function floor5(d) { return new Date(Math.floor(d.getTime() / 300e3) * 300e3); }

  async function api(path, init) {
    if (!API) throw new Error("The analysis service is not configured yet (site/config.js).");
    const r = await fetch(API + path, { ...init, headers: { "Content-Type": "application/json" } });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `Service error ${r.status}`);
    return j;
  }
  async function config() { if (!apiCfg) apiCfg = await api("/api/config"); return apiCfg; }

  // ---- form ---------------------------------------------------------------------------------
  async function openForm() {
    const mb = $("mbody");
    $("modal").hidden = false;
    mb.innerHTML = '<div class="loading">Loading…</div>';
    let c;
    try { c = await config(); } catch (e) { mb.innerHTML = `<h3>New analysis</h3><div class="note">${esc(e.message)}</div>`; return; }
    const L = c.limits, now = new Date();
    const maxEnd = floor5(new Date(now.getTime() - L.latency_min * 60e3));
    const minStart = `${new Date(now.getTime() - 7 * 3600e3).getUTCFullYear() - L.back_years}-01-01T00:00`;
    const end0 = mstString(new Date(Math.floor(maxEnd.getTime() / 3600e3) * 3600e3));
    const start0 = mstString(new Date(parseMst(end0).getTime() - 24 * 3600e3));
    mb.innerHTML = `
      <h3>New analysis</h3>
      <p class="muted">Pick a storm window (Arizona time, MST). The site pulls MRMS radar for the window and the
        ground conditions for each basin — 3DEP terrain, NLCD land cover and impervious area for that year,
        SSURGO soils, NOAA Atlas 14 and NASA SMAP soil moisture — then runs the flood model.
        A run takes about 5–20 minutes.</p>
      <form id="runForm" class="runform">
        <div class="frow"><label for="rStart">Start</label><input type="datetime-local" id="rStart" step="300" min="${minStart}" max="${mstString(maxEnd)}" value="${start0}" required></div>
        <div class="frow"><label for="rEnd">End</label><input type="datetime-local" id="rEnd" step="300" min="${minStart}" max="${mstString(maxEnd)}" value="${end0}" required></div>
        <div class="frow"><span></span><span class="quick">
          <button type="button" class="link" data-h="24">last 24 h</button>
          <button type="button" class="link" data-h="72">last 3 days</button>
          <button type="button" class="link" data-h="${L.max_days * 24}">last ${L.max_days} days</button></span></div>
        <fieldset class="frow"><legend>Basins</legend><div id="rBasins">
          ${c.basins.map((b) => `<label class="chk"><input type="checkbox" value="${esc(b.id)}" checked> ${esc(b.name)}</label>`).join("")}
        </div></fieldset>
        <div id="rInfo" class="muted"></div>
        <div id="rErr" class="note" hidden></div>
        <div class="factions"><button type="submit" class="primary" id="rGo">Run analysis</button></div>
        <p class="muted small">Limits: start on or after ${esc(minStart.slice(0, 10))} (${L.back_years} years back), up to ${L.max_days} days per run,
          ending at least ${L.latency_min} min ago. Radar before Oct 14, 2020 comes from the Iowa Environmental Mesonet
          archive (gauge-corrected hourly QPE; reflectivity may be missing).</p>
      </form>`;
    const info = () => {
      const s = parseMst($("rStart").value), e = parseMst($("rEnd").value);
      if (!s || !e) { $("rInfo").textContent = ""; return; }
      const h = (e - s) / 3600e3;
      $("rInfo").textContent = h > 0 ? `Window: ${h >= 48 ? (h / 24).toFixed(1) + " days" : h.toFixed(h % 1 ? 2 : 0) + " h"} · ${Math.round(h * 12).toLocaleString()} five-minute steps` : "";
    };
    $("rStart").oninput = $("rEnd").oninput = info; info();
    mb.querySelectorAll(".quick button").forEach((b) => b.onclick = () => {
      $("rEnd").value = mstString(maxEnd);
      $("rStart").value = mstString(new Date(maxEnd.getTime() - (+b.dataset.h) * 3600e3)); info();
    });
    $("runForm").onsubmit = async (ev) => {
      ev.preventDefault();
      const basins = [...mb.querySelectorAll("#rBasins input:checked")].map((i) => i.value);
      const body = { start_mst: $("rStart").value.slice(0, 16), end_mst: $("rEnd").value.slice(0, 16), basins };
      $("rErr").hidden = true; $("rGo").disabled = true; $("rGo").textContent = "Starting…";
      try {
        const r = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
        $("modal").hidden = true;
        track(r.id, body);
      } catch (e) {
        $("rErr").textContent = e.message; $("rErr").hidden = false;
      } finally { $("rGo").disabled = false; $("rGo").textContent = "Run analysis"; }
    };
  }

  // ---- progress -------------------------------------------------------------------------------
  const STEP_ORDER = ["Validate request", "Radar rainfall", "Flood model", "Soil moisture", "Finalize", "Publish"];
  function stepIcon(s) {
    if (s.status === "completed") return s.conclusion === "success" ? "✓" : s.conclusion === "skipped" ? "–" : "✕";
    return s.status === "in_progress" ? "●" : "○";
  }
  function renderProgress(id, st, req) {
    const t0 = st.started_utc ? new Date(st.started_utc) : null;
    const el = t0 ? Math.max(0, Math.round((Date.now() - t0) / 1000)) : 0;
    const steps = (st.steps || []).filter((s) => STEP_ORDER.some((n) => s.name.startsWith(n)));
    const what = req ? `${esc(req.start_mst.replace("T", " "))} – ${esc(req.end_mst.replace("T", " "))} MST` : "";
    const label = { queued: "Queued", running: "Running", publishing: "Publishing results", unknown: "Starting" }[st.state] || st.state;
    const link = location.href.split("#")[0] + "#run=" + id;
    window.AZApp.setSide(`
      <div class="card runcard">
        <div class="chead2"><b>Analysis ${esc(label.toLowerCase())}…</b><span class="muted">${t0 ? `${Math.floor(el / 60)}:${pad(el % 60)}` : ""}</span></div>
        ${what ? `<div class="muted">${what}</div>` : ""}
        <ol class="steps">${(steps.length ? steps : STEP_ORDER.map((n) => ({ name: n }))).map((s) =>
          `<li class="${s.status || "pending"} ${s.conclusion || ""}"><span class="ic">${stepIcon(s)}</span>${esc(s.name)}</li>`).join("")}</ol>
        <p class="muted small">Runs take about 5–20 minutes. You can leave and come back with this link:
          <br><a href="${esc(link)}">${esc(link)}</a>${st.log_url ? ` · <a href="${esc(st.log_url)}" target="_blank" rel="noopener">workflow log</a>` : ""}</p>
      </div>`);
  }
  function renderFailed(id, st, req) {
    const run = st.run || {};
    const stages = Object.entries(run.stages || {}).map(([k, v]) => `<li>${v.ok ? "✓" : "✕"} ${esc(k)}${v.note ? ` – ${esc(v.note)}` : ""}</li>`).join("");
    window.AZApp.setSide(`<div class="card runcard"><b>The analysis did not finish.</b>
      <div class="note">${esc(run.error || "One of the data sources or the model failed.")}</div>
      ${stages ? `<ul class="steps">${stages}</ul>` : ""}
      <p>${st.log_url ? `<a href="${esc(st.log_url)}" target="_blank" rel="noopener">Workflow log</a> · ` : ""}
      <button class="link" id="rRetry">Try again</button> · <button class="link" id="rNew">New analysis</button></p></div>`);
    $("rNew").onclick = openForm;
    $("rRetry").onclick = async () => {
      const r = req || run;
      try { await api("/api/runs", { method: "POST", body: JSON.stringify({ start_mst: r.start_mst, end_mst: r.end_mst, basins: r.basins }) }); track(id, req); }
      catch (e) { alert(e.message); }
    };
  }

  function track(id, req) {
    clearTimeout(pollTimer); clearInterval(tickTimer);
    let last = { state: "unknown" }, unknownSince = Date.now();
    renderProgress(id, last, req);
    tickTimer = setInterval(() => { if (["queued", "running", "publishing", "unknown"].includes(last.state)) renderProgress(id, last, req); }, 1000);
    const poll = async () => {
      try {
        const st = await api(`/api/runs/${id}`);
        last = st;
        if (st.state === "done") {
          clearInterval(tickTimer);
          const name = (st.run && st.run.name) || id;
          remember(id, name);
          await window.AZApp.openRun(id, name, base(id));
          return;
        }
        if (st.state === "failed") { clearInterval(tickTimer); renderFailed(id, st, req); return; }
        if (st.state === "unknown" && Date.now() - unknownSince > 180e3) {
          clearInterval(tickTimer);
          window.AZApp.setSide(`<div class="card runcard"><b>Analysis not found.</b><p class="muted">No run with id ${esc(id)} is queued or saved (results are kept about ${apiCfg ? apiCfg.limits.runs_keep_days : 45} days).</p><button class="link" id="rNew">New analysis</button></div>`);
          $("rNew").onclick = openForm; return;
        }
        if (st.state !== "unknown") unknownSince = Date.now();
        renderProgress(id, st, req);
      } catch (e) { /* transient; keep polling */ }
      pollTimer = setTimeout(poll, last.state === "publishing" ? 5000 : 10000);
    };
    poll();
  }

  window.AZRuns = { saved, base, resume: (id) => track(id, null), openForm };
  document.addEventListener("DOMContentLoaded", () => {
    const b = $("newRun");
    if (b) { b.onclick = openForm; b.hidden = !API; }
  });
})();
