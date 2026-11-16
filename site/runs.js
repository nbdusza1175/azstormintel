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

  // ---- form (side panel; the map shows the uploaded basin) ------------------------------------
  let preview = null, outletMarker = null, upload = null, clickHandler = null;
  function clearPreview() {
    const map = window.AZApp && window.AZApp.map && window.AZApp.map();
    if (map) { if (preview) map.removeLayer(preview); if (outletMarker) map.removeLayer(outletMarker); if (clickHandler) map.off("click", clickHandler); }
    preview = outletMarker = clickHandler = null;
  }
  function setOutlet(ll) {
    const map = window.AZApp.map();
    if (outletMarker) map.removeLayer(outletMarker);
    outletMarker = L.circleMarker([ll[1], ll[0]], { radius: 7, color: "#fff", weight: 2, fillColor: "#b3261e", fillOpacity: 1 })
      .bindTooltip("Outlet (pour point)").addTo(map);
    upload.outlet = ll;
  }
  function showUpload(b) {
    const map = window.AZApp.map();
    clearPreview();
    preview = L.geoJSON(b.geometry, { style: { color: "#2a78d6", weight: 2.5, fillOpacity: 0.08 }, interactive: false }).addTo(map);
    map.fitBounds(preview.getBounds(), { padding: [30, 30] });
    if (b.outlet) setOutlet(b.outlet);
    clickHandler = (e) => { if (upload) { setOutlet([+e.latlng.lng.toFixed(6), +e.latlng.lat.toFixed(6)]); renderUploadInfo(); } };
    map.on("click", clickHandler);
  }
  function renderUploadInfo() {
    const el = $("upInfo"); if (!el) return;
    if (!upload) { el.innerHTML = ""; return; }
    const L_ = apiCfg.limits;
    const tooBig = upload.area_sqmi > L_.max_area_sqmi;
    el.innerHTML = `
      <div class="upcard">
        <label class="frow2">Basin name<input id="upName" maxlength="60" value="${esc(upload.name)}"></label>
        <div class="muted">${upload.area_sqmi.toFixed(2)} sq mi · ${upload.vertices.toLocaleString()} vertices</div>
        <div class="${upload.outlet ? "ok" : "muted"}">${upload.outlet
          ? `Outlet: ${upload.outlet[1].toFixed(5)}, ${upload.outlet[0].toFixed(5)}${upload.outlet_from_file ? " (pour point from file)" : " (set on map)"}`
          : "No pour point in the file — click the map to set the outlet, or leave it and the model picks the lowest edge."}</div>
        <div class="muted small">Click the map to move the outlet.</div>
        ${upload.notes.map((n) => `<div class="note">${esc(n)}</div>`).join("")}
        ${tooBig ? `<div class="note">This basin is larger than the ${L_.max_area_sqmi} sq mi limit.</div>` : ""}
      </div>`;
    $("upName").oninput = (e) => { upload.name = e.target.value; };
  }

  async function openForm() {
    if (window.AZApp && window.AZApp.hasEvent()) window.AZApp.blank();
    clearTimeout(pollTimer); clearInterval(tickTimer);
    const side = (html) => window.AZApp.setSide(html);
    side('<div class="loading">Loading…</div>');
    let c;
    try { c = await config(); } catch (e) { side(`<div class="card"><h2>New analysis</h2><div class="note">${esc(e.message)}</div></div>`); return; }
    const L_ = c.limits, now = new Date();
    const maxEnd = floor5(new Date(now.getTime() - L_.latency_min * 60e3));
    const minStart = `${new Date(now.getTime() - 7 * 3600e3).getUTCFullYear() - L_.back_years}-01-01T00:00`;
    const end0 = mstString(new Date(Math.floor(maxEnd.getTime() / 3600e3) * 3600e3));
    const start0 = mstString(new Date(parseMst(end0).getTime() - 24 * 3600e3));
    upload = null;
    side(`
      <div class="card runform-card">
        <h2>New analysis</h2>
        <form id="runForm" class="runform">
          <div class="step"><span class="num">1</span><b>Basin</b></div>
          <div class="seg2" role="tablist">
            <button type="button" class="on" data-mode="upload">Upload a basin</button>
            <button type="button" data-mode="preset">Saved basins</button>
          </div>
          <div id="modeUpload">
            <label class="drop" id="drop">
              <input type="file" id="upFile" accept=".kmz,.kml,.geojson,.json">
              <span><b>Choose a StreamStats basin</b><br><span class="muted">KMZ or GeoJSON · up to ${L_.max_area_sqmi} sq mi · Arizona</span></span>
            </label>
            <div id="upInfo"></div>
            <label class="drop small-drop">
              <input type="file" id="ssFile" accept=".csv,.txt">
              <span><b>Optional:</b> StreamStats flow report (CSV) <span class="muted">— adds the flood return period</span></span>
            </label>
            <div id="ssInfo" class="muted small"></div>
          </div>
          <div id="modePreset" hidden>
            <div id="rBasins">${c.basins.map((b) => `<label class="chk"><input type="checkbox" value="${esc(b.id)}" checked> ${esc(b.name)}</label>`).join("")}</div>
          </div>

          <div class="step"><span class="num">2</span><b>Storm window</b> <span class="muted">(Arizona time, MST)</span></div>
          <div class="frow"><label for="rStart">Start</label><input type="datetime-local" id="rStart" step="300" min="${minStart}" max="${mstString(maxEnd)}" value="${start0}" required></div>
          <div class="frow"><label for="rEnd">End</label><input type="datetime-local" id="rEnd" step="300" min="${minStart}" max="${mstString(maxEnd)}" value="${end0}" required></div>
          <div class="quick">
            <button type="button" class="link" data-h="24">last 24 h</button>
            <button type="button" class="link" data-h="72">last 3 days</button>
            <button type="button" class="link" data-h="${L_.max_days * 24}">last ${L_.max_days} days</button></div>
          <div id="rInfo" class="muted small"></div>
          <div id="rErr" class="note" hidden></div>
          <button type="submit" class="primary wide" id="rGo">Run analysis</button>
          <p class="muted small">The analysis pulls MRMS radar for the window plus the basin's terrain (3DEP), land cover and
            impervious area for that year (NLCD), soils (SSURGO), NOAA Atlas 14 and NASA SMAP soil moisture, then runs the
            flood model. About 5–20 minutes. Windows back to ${esc(minStart.slice(0, 10))}, up to ${L_.max_days} days.</p>
        </form>
      </div>`);
    let mode = "upload";
    const sideEl = $("side");
    sideEl.querySelectorAll(".seg2 button").forEach((b) => b.onclick = () => {
      mode = b.dataset.mode;
      sideEl.querySelectorAll(".seg2 button").forEach((x) => x.classList.toggle("on", x === b));
      $("modeUpload").hidden = mode !== "upload"; $("modePreset").hidden = mode !== "preset";
      if (mode === "preset") clearPreview(); else if (upload) showUpload(upload);
    });
    $("upFile").onchange = async (e) => {
      const f = e.target.files[0]; if (!f) return;
      $("upInfo").innerHTML = '<div class="muted">Reading…</div>';
      try {
        const b = await window.AZBasin.read(f);
        upload = { ...b, outlet_from_file: !!b.outlet };
        $("drop").querySelector("b").textContent = f.name;
        showUpload(upload); renderUploadInfo();
      } catch (er) { upload = null; clearPreview(); $("upInfo").innerHTML = `<div class="note">${esc(er.message)}</div>`; }
    };
    let ssText = "";
    $("ssFile").onchange = async (e) => {
      const f = e.target.files[0]; ssText = "";
      if (!f) { $("ssInfo").textContent = ""; return; }
      const t = await f.text();
      const n = (t.match(/percent AEP flood|year peak/gi) || []).length;
      if (!n) { $("ssInfo").innerHTML = '<span class="err">That file has no peak-flow statistics (look for the StreamStats "Peak-Flow Statistics" report).</span>'; return; }
      ssText = t.slice(0, L_.max_csv_chars);
      $("ssInfo").textContent = `${n} flood statistics found.`;
    };
    const info = () => {
      const s = parseMst($("rStart").value), e = parseMst($("rEnd").value);
      if (!s || !e) { $("rInfo").textContent = ""; return; }
      const h = (e - s) / 3600e3;
      $("rInfo").textContent = h > 0 ? `Window: ${h >= 48 ? (h / 24).toFixed(1) + " days" : h.toFixed(h % 1 ? 2 : 0) + " h"} · ${Math.round(h * 12).toLocaleString()} five-minute steps` : "";
    };
    $("rStart").oninput = $("rEnd").oninput = info; info();
    sideEl.querySelectorAll(".quick button").forEach((b) => b.onclick = () => {
      $("rEnd").value = mstString(maxEnd);
      $("rStart").value = mstString(new Date(maxEnd.getTime() - (+b.dataset.h) * 3600e3)); info();
    });
    $("runForm").onsubmit = async (ev) => {
      ev.preventDefault();
      const fail = (m) => { $("rErr").textContent = m; $("rErr").hidden = false; };
      $("rErr").hidden = true;
      const body = { start_mst: $("rStart").value.slice(0, 16), end_mst: $("rEnd").value.slice(0, 16) };
      if (mode === "upload") {
        if (!upload) return fail("Choose a basin file first.");
        if (upload.area_sqmi > L_.max_area_sqmi) return fail(`The basin is larger than ${L_.max_area_sqmi} sq mi.`);
        let g;
        try { g = window.AZBasin.simplify(upload.geometry, L_.max_basin_json_chars - L_.max_csv_chars - 600).geometry; } catch (er) { return fail(er.message); }
        body.basin = { name: upload.name, geometry: g, outlet: upload.outlet, streamstats_csv: ssText };
      } else {
        body.basins = [...sideEl.querySelectorAll("#rBasins input:checked")].map((i) => i.value);
        if (!body.basins.length) return fail("Choose at least one basin.");
      }
      $("rGo").disabled = true; $("rGo").textContent = "Starting…";
      try {
        const r = await api("/api/runs", { method: "POST", body: JSON.stringify(body) });
        track(r.id, { start_mst: body.start_mst, end_mst: body.end_mst, basins: body.basins, basin: body.basin,
          label: body.basin ? body.basin.name : null });
      } catch (e) { fail(e.message); $("rGo").disabled = false; $("rGo").textContent = "Run analysis"; }
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
    const what = req ? `${req.label ? esc(req.label) + " · " : ""}${esc(req.start_mst.replace("T", " "))} – ${esc(req.end_mst.replace("T", " "))} MST` : "";
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
    if (!req && run.custom_basin) { $("rRetry").hidden = true; }  // the upload isn't kept; start a new analysis
    $("rRetry").onclick = async () => {
      const r = req || run;
      try { await api("/api/runs", { method: "POST", body: JSON.stringify({ start_mst: r.start_mst, end_mst: r.end_mst, basins: r.basins, basin: r.basin }) }); track(id, req); }
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

  window.AZRuns = { saved, base, resume: (id) => track(id, null), openForm, clearPreview };
  document.addEventListener("DOMContentLoaded", () => {
    const b = $("newRun");
    if (b) { b.onclick = openForm; b.hidden = !API; }
  });
})();
