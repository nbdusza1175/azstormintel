/* AZ Storm Intel viewer — static site, reads data/ produced by the GitHub Actions pipeline. */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const css = (v) => getComputedStyle(document.querySelector(".viz-root")).getPropertyValue(v).trim();
  const SERIES = { fort_valley: "--series-1", mountain_dell: "--series-2" };
  const seriesColor = (id, k) => css(SERIES[id] || (k % 2 ? "--series-2" : "--series-1"));
  const TZ = "America/Phoenix";
  const fmtT = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const fmtShort = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric", hour: "numeric" });
  const tmst = (iso) => fmtT.format(new Date(iso)) + " MST";
  const f2 = (x) => (x == null || !isFinite(x) ? "–" : (+x).toFixed(2));
  const fN = (x) => (x == null || !isFinite(x) ? "–" : Math.round(x).toLocaleString());

  // ---- colour scales ------------------------------------------------------
  const INT_CLASSES = [0.01, 0.1, 0.25, 0.5, 1, 2];               // in/hr
  const INT_COLORS = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#104281"];
  const DBZ_STOPS = [[5, "#04e9e7"], [10, "#019ff4"], [15, "#0300f4"], [20, "#02fd02"], [25, "#01c501"], [30, "#008e00"],
    [35, "#fdf802"], [40, "#e5bc00"], [45, "#fd9500"], [50, "#fd0000"], [55, "#d40000"], [60, "#bc0000"],
    [65, "#f800fd"], [70, "#9854c6"], [75, "#fdfdfd"]];
  const hex2rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const DBZ_LUT = new Uint8ClampedArray(256 * 3), DBZ_ON = new Uint8Array(256);
  for (let q = 1; q < 256; q++) {
    const d = q / 3; let c = null;
    for (const [lo, col] of DBZ_STOPS) if (d >= lo) c = col;
    if (c) { DBZ_LUT.set(hex2rgb(c), q * 3); DBZ_ON[q] = 1; }
  }
  const INT_LUT = new Uint8ClampedArray(256 * 3), INT_ON = new Uint8Array(256), INT_VAL = new Float32Array(256);
  for (let q = 0; q < 256; q++) {
    const v = 10 * Math.pow(q / 255, 2); INT_VAL[q] = v;
    let k = -1; INT_CLASSES.forEach((t, i) => { if (v >= t) k = i; });
    if (k >= 0) { INT_LUT.set(hex2rgb(INT_COLORS[k]), q * 3); INT_ON[q] = 1; }
  }
  const intColor = (v) => { let k = -1; INT_CLASSES.forEach((t, i) => { if (v >= t) k = i; }); return k < 0 ? null : INT_COLORS[k]; };

  // ---- normal distribution helpers (return periods) -----------------------
  function erf(x) { const s = Math.sign(x); x = Math.abs(x); const t = 1 / (1 + 0.3275911 * x);
    const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x); return s * y; }
  const ncdf = (z) => 0.5 * (1 + erf(z / Math.SQRT2));
  function nppf(p) { let lo = -8, hi = 8; for (let i = 0; i < 80; i++) { const m = (lo + hi) / 2; if (ncdf(m) < p) lo = m; else hi = m; } return (lo + hi) / 2; }
  function returnPeriod(q, rows) {           // rows: [{T, q}] ; log Q vs normal deviate
    const r = rows.filter((d) => d.q > 0).sort((a, b) => a.q - b.q);
    if (!r.length || !(q > 0)) return null;
    if (q < r[0].q) return { txt: `<${r[0].T}-yr` };
    if (q > r[r.length - 1].q) return { txt: `>${r[r.length - 1].T}-yr` };
    for (let i = 0; i < r.length - 1; i++) {
      if (q >= r[i].q && q <= r[i + 1].q) {
        const z0 = nppf(1 - 1 / r[i].T), z1 = nppf(1 - 1 / r[i + 1].T);
        const z = z0 + (z1 - z0) * (Math.log(q) - Math.log(r[i].q)) / (Math.log(r[i + 1].q) - Math.log(r[i].q));
        const T = 1 / (1 - ncdf(z)); return { T, txt: (T < 10 ? T.toFixed(1) : Math.round(T)) + "-yr" };
      }
    }
    return null;
  }

  // ---- state ---------------------------------------------------------------
  const S = { meta: null, dbz: null, int: null, step: 0, playing: null, radar: "dbz", models: {}, base: "" };
  let map, radarOverlay, cellLayers = {}, outlineLayer, gaugeLayer, layerCtl, modelOverlays = [];
  const canvas = document.createElement("canvas");

  async function getJSON(u) { const r = await fetch(u, { cache: "no-cache" }); if (!r.ok) throw new Error(u + " " + r.status); return r.json(); }
  async function unpack(b64) {          // base64(gzip(bytes)) -> Uint8Array
    const raw = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const ds = new Blob([raw]).stream().pipeThrough(new DecompressionStream("gzip"));
    return new Uint8Array(await new Response(ds).arrayBuffer());
  }

  // ---- map -----------------------------------------------------------------
  async function initMap() {
    map = L.map("map", { zoomSnap: 0.25, minZoom: 8, maxZoom: 16, preferCanvas: false });
    map.attributionControl.setPrefix(false);
    const bases = {};
    let tilesMeta = null;
    try { tilesMeta = await getJSON("tiles/tiles.json"); } catch (e) { /* no bundled tiles yet */ }
    const usgs = (svc) => `https://basemap.nationalmap.gov/arcgis/rest/services/${svc}/MapServer/tile/{z}/{y}/{x}`;
    const live = {
      "USGS Topo": L.tileLayer(usgs("USGSTopo"), { maxZoom: 16, attribution: "USGS The National Map" }),
      "USGS Imagery": L.tileLayer(usgs("USGSImageryTopo"), { maxZoom: 16, attribution: "USGS The National Map" }),
    };
    if (tilesMeta) {
      const b = tilesMeta.bbox, bounds = L.latLngBounds([b[1], b[0]], [b[3], b[2]]);
      (tilesMeta.layers || []).forEach((l) => {
        bases[(l === "topo" ? "Topo" : "Imagery") + " (offline)"] = L.tileLayer(`tiles/${l}/{z}/{x}/{y}.jpg`, {
          minZoom: tilesMeta.min_zoom, maxNativeZoom: tilesMeta.max_zoom, maxZoom: 16, bounds, attribution: "USGS The National Map" });
      });
    }
    const liveOK = await probe("https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/10/203/196");
    if (liveOK) Object.assign(bases, live);
    const grid = gridLayer();
    bases["Lat/lon grid only"] = grid;
    const first = liveOK ? live["USGS Topo"] : (bases["Topo (offline)"] || grid);
    first.addTo(map);
    layerCtl = L.control.layers(bases, {}, { position: "topright", collapsed: true }).addTo(map);
    radarOverlay = L.imageOverlay(canvas.toDataURL(), [[0, 0], [0, 0]], { opacity: 0.7, className: "pix", interactive: false }).addTo(map);
  }
  function probe(url) {
    return new Promise((res) => { const im = new Image(); const t = setTimeout(() => res(false), 3500);
      im.onload = () => { clearTimeout(t); res(true); }; im.onerror = () => { clearTimeout(t); res(false); }; im.src = url + "?probe=" + Date.now(); });
  }
  function gridLayer() {
    const g = L.layerGroup();
    for (let la = 34.9; la <= 35.5; la += 0.1) L.polyline([[la, -112.1], [la, -111.4]], { color: "#9a988f", weight: 0.5, opacity: 0.6, interactive: false }).addTo(g);
    for (let lo = -112.1; lo <= -111.4; lo += 0.1) L.polyline([[34.9, lo], [35.5, lo]], { color: "#9a988f", weight: 0.5, opacity: 0.6, interactive: false }).addTo(g);
    [["Flagstaff", 35.1983, -111.6513], ["Humphreys Peak", 35.3463, -111.6779], ["I-40 / I-17", 35.1803, -111.6627]].forEach(([t, la, lo]) => {
      L.circleMarker([la, lo], { radius: 2.5, color: "#52514e", weight: 1, fillOpacity: 1, interactive: false }).addTo(g);
      L.marker([la, lo], { icon: L.divIcon({ className: "pl", html: t, iconSize: [110, 14], iconAnchor: [-5, 7] }), interactive: false }).addTo(g);
    });
    return g;
  }

  // ---- event load -------------------------------------------------------------
  async function loadEvent(ev) {
    stop();
    $("side").innerHTML = '<div class="loading">Loading ' + ev.name + "…</div>";
    S.base = "data/" + ev.path.replace(/^data\//, "") + "/";
    const [meta, fr] = await Promise.all([getJSON(S.base + "meta.json"), getJSON(S.base + "frames.json")]);
    const [dbz, int] = await Promise.all([unpack(fr.dbz), unpack(fr.int)]);
    S.meta = meta; S.dbz = dbz; S.int = int;
    S.models = {};
    try {
      const idx = await getJSON(S.base + "model/index.json");
      await Promise.all(Object.entries(idx.basins || {}).filter(([, v]) => v.ok).map(async ([k, v]) => {
        try { S.models[k] = await getJSON(S.base + "model/" + v.path); S.models[k]._dir = S.base + "model/" + k + "/"; } catch (e) { /* ignore */ }
      }));
    } catch (e) { /* model not run for this event */ }
    const g = meta.grid; canvas.width = g.nx; canvas.height = g.ny;
    radarOverlay.setBounds([[g.lat0 - (g.ny - 1) * g.d - g.d / 2, g.lon0 - g.d / 2], [g.lat0 + g.d / 2, g.lon0 + (g.nx - 1) * g.d + g.d / 2]]);
    $("evinfo").textContent = `${tmst(meta.start_utc)} – ${tmst(meta.end_utc)} · ${meta.steps_utc.length} × ${meta.step_min}-min steps · updated ${tmst(meta.generated_utc)}`;
    buildBasinLayers();
    buildCards();
    buildChart();
    $("slider").max = meta.steps_utc.length - 1;
    const pk = peakStep();
    setStep(pk);
  }
  function peakStep() {
    let best = 0, v = -1;
    S.meta.basins.forEach((b) => b.ts.mean_inhr.forEach((x, i) => { if (x > v) { v = x; best = i; } }));
    return best;
  }

  function buildBasinLayers() {
    Object.values(cellLayers).forEach((l) => map.removeLayer(l));
    if (outlineLayer) map.removeLayer(outlineLayer);
    if (gaugeLayer) map.removeLayer(gaugeLayer);
    modelOverlays.forEach((o) => { layerCtl.removeLayer(o); map.removeLayer(o); });
    modelOverlays = []; cellLayers = {};
    const g = S.meta.grid; const n = S.meta.steps_utc.length;
    outlineLayer = L.layerGroup().addTo(map);
    gaugeLayer = L.layerGroup().addTo(map);
    const all = L.latLngBounds([]);
    S.meta.basins.forEach((b, k) => {
      const col = seriesColor(b.id, k);
      const gj = L.geoJSON(b.polygon, { filter: (f) => f.geometry.type !== "Point", style: { color: col, weight: 2.5, fill: false }, interactive: false });
      gj.addTo(outlineLayer); all.extend(gj.getBounds());
      L.circleMarker([b.outlet[1], b.outlet[0]], { radius: 6, color: "#fff", weight: 2, fillColor: col, fillOpacity: 1 })
        .bindTooltip(`<b>${b.name}</b><br>Outlet`).addTo(gaugeLayer);
      (b.rain_gauges || []).concat((b.stage_gauges || []).filter((s) => s.lat)).forEach((gg) => {
        L.circleMarker([gg.lat, gg.lon], { radius: 5, color: "#fff", weight: 1.5, fillColor: "#333", fillOpacity: 1 })
          .bindTooltip(`<b>Gauge ${gg.id}</b><br>${gg.name}${gg.regulated ? "<br>Regulated (pool level) – timing check only" : ""}`).addTo(gaugeLayer);
      });
      // per-cell running totals from the display codes
      const nc = b.cells.idx.length; const cum = new Float32Array(n * nc);
      for (let c = 0; c < nc; c++) { let s = 0; const id = b.cells.idx[c]; for (let t = 0; t < n; t++) { s += INT_VAL[S.int[t * g.ny * g.nx + id]] * S.meta.step_min / 60; cum[t * nc + c] = s; } }
      b._cum = cum;
      const grp = L.layerGroup();
      b._rects = b.cells.idx.map((id, c) => {
        const i = Math.floor(id / g.nx), j = id % g.nx;
        const la = g.lat0 - i * g.d, lo = g.lon0 + j * g.d;
        const r = L.rectangle([[la - g.d / 2, lo - g.d / 2], [la + g.d / 2, lo + g.d / 2]], { weight: 0.6, color: col, opacity: 0.6, fillOpacity: 0 });
        r.bindTooltip(() => {
          const v = INT_VAL[S.int[S.step * g.ny * g.nx + id]];
          return `<b>${b.name}</b> cell ${la.toFixed(3)}, ${lo.toFixed(3)}<br>Rate ${v.toFixed(2)} in/hr<br>So far ${b._cum[S.step * nc + c].toFixed(2)} in · event ${b.cells.total_in[c].toFixed(2)} in<br>Covers ${b.cells.area_ac[c].toFixed(0)} ac (${(b.cells.frac[c] * 100).toFixed(0)}% of cell in basin)`;
        }, { sticky: true });
        r.addTo(grp); return r;
      });
      cellLayers[b.id] = grp;
      if ($("cellsOn").checked) grp.addTo(map);
      const M = S.models[b.id];
      if (M && M.map && M.map.layers) {
        M.map.layers.forEach((l) => {
          const o = L.imageOverlay(M._dir + l.file, l.bounds, { opacity: 0.75, interactive: false });
          layerCtl.addOverlay(o, `${b.name}: ${l.name}`); modelOverlays.push(o);
        });
        if (M.map.longest_path) { const o = L.polyline(M.map.longest_path, { color: "#b3261e", weight: 2 }); layerCtl.addOverlay(o, `${b.name}: longest flowpath`); modelOverlays.push(o); }
      }
    });
    if (all.isValid()) map.fitBounds(all, { padding: [24, 24] });
    legend();
  }

  // ---- frames ------------------------------------------------------------------
  function drawFrame() {
    const g = S.meta.grid, N = g.nx * g.ny, off = S.step * N;
    const ctx = canvas.getContext("2d"); const im = ctx.createImageData(g.nx, g.ny);
    if (S.radar !== "none") {
      const src = S.radar === "dbz" ? S.dbz : S.int, lut = S.radar === "dbz" ? DBZ_LUT : INT_LUT, on = S.radar === "dbz" ? DBZ_ON : INT_ON;
      for (let p = 0; p < N; p++) { const q = src[off + p]; if (on[q]) { im.data[p * 4] = lut[q * 3]; im.data[p * 4 + 1] = lut[q * 3 + 1]; im.data[p * 4 + 2] = lut[q * 3 + 2]; im.data[p * 4 + 3] = 255; } }
    }
    ctx.putImageData(im, 0, 0);
    radarOverlay.setUrl(canvas.toDataURL());
    radarOverlay.setOpacity(+$("op").value);
    S.meta.basins.forEach((b) => {
      b._rects.forEach((r, c) => {
        const v = INT_VAL[S.int[off + b.cells.idx[c]]]; const col = intColor(v);
        r.setStyle({ fillColor: col || "#000", fillOpacity: col ? 0.85 : 0 });
      });
    });
  }

  function setStep(s) {
    const n = S.meta.steps_utc.length; S.step = Math.max(0, Math.min(n - 1, s));
    $("slider").value = S.step;
    $("tlabel").textContent = tmst(S.meta.steps_utc[S.step]);
    drawFrame(); updateNow(); moveCursor();
  }
  function stop() { if (S.playing) { clearInterval(S.playing); S.playing = null; $("play").textContent = "▶"; } }
  function play() {
    if (S.playing) return stop();
    $("play").textContent = "❚❚";
    S.playing = setInterval(() => { if (S.step >= S.meta.steps_utc.length - 1) return stop(); setStep(S.step + 1); }, +$("speed").value);
  }

  // ---- legend ----------------------------------------------------------------------
  function legend() {
    let h = "";
    if (S.radar === "dbz") h += `<b>Radar reflectivity (dBZ)</b><div class="row">${DBZ_STOPS.filter((_, i) => i % 2 === 0).map(([d, c]) => `<span><span class="sw" style="background:${c}"></span>${d}</span>`).join("")}</div>`;
    h += `<div style="margin-top:4px"><b>Rain rate, 5-min (in/hr)</b>${S.radar === "int" ? "" : " – basin cells"}</div><div class="row">${INT_CLASSES.map((t, i) => `<span><span class="sw" style="background:${INT_COLORS[i]}"></span>≥${t}</span>`).join("")}</div>`;
    h += `<div class="row">${S.meta.basins.map((b, k) => `<span class="key"><i style="background:${seriesColor(b.id, k)}"></i>${b.name}</span>`).join("")}</div>`;
    h += `<div style="margin-top:3px">NOAA MRMS · 1 km cells · Pass 2 gauge-corrected depth</div>`;
    $("legend").innerHTML = h;
  }

  // ---- cards -------------------------------------------------------------------------
  function freqRows(f) { return (f.rows || []).map((r) => ({ T: r.T, q: r.q })); }
  function buildCards() {
    const side = $("side"); side.innerHTML = "";
    S.meta.basins.forEach((b, k) => {
      const s = b.summary, col = seriesColor(b.id, k), M = S.models[b.id];
      const el = document.createElement("div"); el.className = "card"; el.id = "card-" + b.id;
      const durs = ["5", "15", "30", "60", "120", "180", "360", "720", "1440", "2880", "4320"].filter((d) => s.durations[d]);
      const lab = (d) => (+d < 60 ? d + " min" : +d / 60 + " hr");
      let h = `<h2><span class="dot" style="background:${col}"></span>${b.name}</h2>
        <div class="meta">${s.area_sqmi} sq mi in ${s.n_cells} radar cells${b.published_area_sqmi ? " · published " + Object.entries(b.published_area_sqmi).map(([kk, v]) => `${kk} ${v}`).join(", ") + " sq mi" : ""}</div>
        <div class="stats">
          <div class="stat"><div class="v">${f2(s.total_in)} in</div><div class="l">Storm total, basin avg (max cell ${f2(s.max_cell_total_in)})</div></div>
          <div class="stat"><div class="v">${f2(s.peak_5min_inhr)}</div><div class="l">Peak 5-min in/hr, basin avg · ${fmtShort.format(new Date(s.peak_5min_utc))}</div></div>
          <div class="stat"><div class="v">${f2(s.worst_cell_5min_inhr)}</div><div class="l">Worst cell 5-min in/hr${s.any_cell_ge_1 ? "" : " · no cell ≥ 1"}</div></div>
        </div>
        <div class="now" id="now-${b.id}"></div>`;
      // flood model
      h += `<h3>Flood intensity (ModClark model)</h3>`;
      if (M) {
        const fl = M.flood || {}, Q = fl.peak_cfs;
        h += `<div class="head" style="border-left-color:${col}"><div class="big">${fN(Q)} cfs peak</div><div>${M.runoff ? "at " + M.runoff.peak_time_local : ""} · natural (unregulated) flow</div>`;
        const refs = [];
        (b.frequency || []).forEach((f) => {
          if (f.basis === "streamstats" && f.rows) { const rp = returnPeriod(Q, freqRows(f)); refs.push(`${f.label}${f.factor && Math.abs(f.factor - 1) > 0.02 ? ` (×${f.factor} area transfer)` : ""}: <b>${rp ? rp.txt : "–"}</b>`); }
          if (f.basis === "point") Object.entries(f.points).forEach(([T, q]) => refs.push(`${f.label}: ${fN(q)} cfs → peak is <b>${Math.round(Q / q * 100)}%</b> of it`));
        });
        h += refs.length ? `<ul class="flags">${refs.map((r) => `<li>${r}</li>`).join("")}</ul>` : "";
        h += `<div class="muted">${fl.band_text || ""}</div></div>`;
        if (M.storm) h += `<div class="now"><b>Storm (Atlas 14):</b> ${M.storm.headline}</div>`;
        if (M.hydrograph) h += `<svg class="hydro" id="hy-${b.id}" role="img" aria-label="Modelled hydrograph"></svg>`;
        if (M.design_check) {
          const d = M.design_check;
          h += `<details><summary>Design-storm check: ${fN(d.peak_cfs)} cfs (${d.label})</summary><table class="t"><tr><th>Reference</th><th>cfs</th><th>model / ref</th></tr>${d.benchmarks.map((x) => `<tr><td>${x.label}</td><td>${fN(x.cfs)}</td><td>${Math.round(x.pct)}%</td></tr>`).join("")}</table><div class="muted">${d.note || ""}</div></details>`;
        }
        const t = M.timing || {};
        h += `<details><summary>Model details</summary><table class="t">${(M.details_table || []).map(([a, v]) => `<tr><td>${a}</td><td>${v}</td></tr>`).join("")}</table>
          ${t.by_arc ? `<div class="muted">Tc/R by ARC: ${Object.entries(t.by_arc).map(([a, v]) => `${a} ${v.Tc_hr.toFixed(2)}/${v.R_hr.toFixed(2)} h`).join(" · ")} (bankfull pass ${t.Tc_bankfull_hr.toFixed(2)}/${t.R_bankfull_hr.toFixed(2)} h)</div>` : ""}
          ${M.figures ? `<button class="link" data-figs="${b.id}">figures</button>` : ""} <button class="link" data-txt="${b.id}">full summary</button></details>`;
        if (M.flags && M.flags.length) h += `<details><summary>Flags (${M.flags.length})</summary><ul class="flags">${M.flags.map((x) => `<li>${x}</li>`).join("")}</ul></details>`;
      } else {
        h += `<div class="note">The flood model hasn't run for this event yet. It runs in the site's GitHub job (terrain, soils, land cover and NOAA Atlas 14 are fetched there). Radar results below are final.</div>`;
      }
      if (b.channel) {
        const c = b.channel;
        h += `<h3>Channel</h3><div class="muted"><b>${c.name}</b> — ${c.design_storm}; design ${Object.entries(c.design_flows_cfs).map(([kk, v]) => `${fN(v)} cfs ${kk}`).join(", ")}. ${c.note} <i>Source: ${c.source}.</i></div>`;
      }
      // radar durations table
      h += `<h3>Rain, basin average (max running window)</h3><table class="t"><tr><th>Duration</th><th>Depth in</th><th>in/hr</th><th>Worst cell in/hr</th><th>Ending</th></tr>
        ${durs.map((d) => { const x = s.durations[d]; return `<tr><td>${lab(d)}</td><td>${f2(x.depth_in)}</td><td>${f2(x.intensity_inhr)}</td><td>${x.worst_cell_inhr == null ? "" : f2(x.worst_cell_inhr)}</td><td>${fmtShort.format(new Date(x.end_utc))}</td></tr>`; }).join("")}</table>`;
      const fr = (b.frequency || []).find((f) => f.basis === "streamstats" && f.rows);
      if (fr) h += `<details><summary>Flood-frequency basis</summary><table class="t"><tr><th>T (yr)</th><th>Q cfs</th><th>90% PI</th></tr>${fr.rows.map((r) => `<tr><td>${Math.round(r.T)}</td><td>${fN(r.q)}</td><td>${fN(r.pil)}–${fN(r.piu)}</td></tr>`).join("")}</table>
        <div class="muted">${fr.label}, ${fr.region}; published for ${fr.source_area_sqmi} sq mi, transferred to ${fr.target_area_sqmi} sq mi by (A/As)^0.6 = ×${fr.factor}.</div></details>`;
      const notes = [];
      if (s.snow_steps) notes.push(`Snow flagged on up to ${s.snow_max_pct}% of the basin (${fmtShort.format(new Date(s.snow_first_utc))} – ${fmtShort.format(new Date(s.snow_last_utc))}).`);
      if (s.pass2_over_precip_rate) notes.push(`Gauge-corrected Pass 2 ran ${s.pass2_over_precip_rate}× the raw 2-min radar rate (hourly range ${s.factor_range ? s.factor_range.join("–") : "–"}).`);
      (b.notes || []).forEach((x) => notes.push(x));
      (b.stage_gauges || []).filter((x) => x.regulated).forEach((x) => notes.push(`Gauge ${x.id}: ${x.note}`));
      if (notes.length) h += `<details><summary>Notes</summary><ul class="flags">${notes.map((x) => `<li>${x}</li>`).join("")}</ul></details>`;
      el.innerHTML = h; side.appendChild(el);
      if (M && M.hydrograph) hydroChart($("hy-" + b.id), M, b, col);
    });
    const qa = S.meta.qa;
    const foot = document.createElement("div"); foot.className = "muted";
    foot.innerHTML = `Radar QA: ${qa.precip_rate_scans_missing} of ${qa.precip_rate_scans_listed} 2-min scans missing (forward-filled ≤ 6 min), ${qa.pass2_hours_found}/${qa.pass2_hours_expected} Pass 2 hours, ${qa.reflectivity_frames_missing} reflectivity frames missing. Display values are rounded; exact values are in <a href="${S.base}logs/cell_log.csv.gz">cell_log.csv.gz</a> and <a href="${S.base}logs/basin_timeseries.csv.gz">basin_timeseries.csv.gz</a>. Version ${S.meta.version}.`;
    side.appendChild(foot);
    side.querySelectorAll("[data-figs]").forEach((bt) => bt.onclick = () => { const M = S.models[bt.dataset.figs];
      modal(`<h3>${bt.dataset.figs.replace("_", " ")} – model figures</h3><div class="figs">${Object.values(M.figures).map((f) => `<img src="${M._dir}${f}" alt="${f}">`).join("")}</div>`); });
    side.querySelectorAll("[data-txt]").forEach((bt) => bt.onclick = () => { const M = S.models[bt.dataset.txt];
      modal(`<pre style="white-space:pre-wrap;font-size:11.5px">${(M.summary_text || "").replace(/[<&]/g, (c) => ({ "<": "&lt;", "&": "&amp;" }[c]))}</pre>`); });
  }
  function modal(html) { $("mbody").innerHTML = html; $("modal").hidden = false; }
  $("mclose").onclick = () => { $("modal").hidden = true; };
  $("modal").onclick = (e) => { if (e.target.id === "modal") $("modal").hidden = true; };

  function updateNow() {
    S.meta.basins.forEach((b) => {
      const t = b.ts, i = S.step, el = $("now-" + b.id); if (!el) return;
      el.innerHTML = `<b>${tmst(S.meta.steps_utc[i])}:</b> ${f2(t.mean_inhr[i])} in/hr basin avg (max cell ${f2(t.max_inhr[i])}) · ${f2(t.cum_in[i])} in so far · raining on ${t.wet_sqmi[i].toFixed(1)} sq mi, ≥0.5 in/hr on ${t.above_sqmi["0.5"][i].toFixed(1)}${t.snow_pct[i] > 0 ? ` · snow ${t.snow_pct[i]}%` : ""}${t.storm[i] && t.storm[i][2] != null ? ` · core moving ${t.storm[i][2]} mph toward ${t.storm[i][3]}°` : ""}`;
    });
  }

  // ---- event chart (SVG) ----------------------------------------------------------------
  const CH = { pad: { l: 38, r: 10, t: 8, b: 20 } };
  function buildChart() {
    const svg = $("chart"); const W = svg.clientWidth || 800, H = svg.clientHeight || 130; svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const n = S.meta.steps_utc.length, p = CH.pad;
    const ymax = Math.max(0.05, ...S.meta.basins.map((b) => Math.max(...b.ts.mean_inhr))) * 1.1;
    const x = (i) => p.l + (W - p.l - p.r) * i / Math.max(1, n - 1), y = (v) => H - p.b - (H - p.t - p.b) * v / ymax;
    Object.assign(CH, { W, H, x, y, n, ymax });
    let h = "";
    const ticks = niceTicks(ymax);
    ticks.forEach((v) => { h += `<line x1="${p.l}" x2="${W - p.r}" y1="${y(v)}" y2="${y(v)}" stroke="${css("--grid")}" stroke-width="1"/><text x="${p.l - 5}" y="${y(v) + 3}" text-anchor="end">${v}</text>`; });
    const t0 = new Date(S.meta.steps_utc[0]).getTime(), dtm = S.meta.step_min * 60000;
    for (let i = 0; i < n; i++) { const d = new Date(t0 + i * dtm); const hh = +new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", hour12: false }).format(d);
      if (hh % (n > 600 ? 12 : n > 200 ? 6 : 2) === 0 && d.getUTCMinutes() === 0) h += `<line x1="${x(i)}" x2="${x(i)}" y1="${H - p.b}" y2="${H - p.b + 4}" stroke="${css("--text-muted")}"/><text x="${x(i)}" y="${H - 5}" text-anchor="middle">${fmtShort.format(d)}</text>`; }
    S.meta.basins.forEach((b, k) => {
      const col = seriesColor(b.id, k);
      const d = b.ts.mean_inhr.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
      h += `<path d="${d}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round"/>`;
    });
    h += `<line id="cur" y1="${p.t}" y2="${H - p.b}" stroke="${css("--text-primary")}" stroke-width="1" stroke-dasharray="3 3"/>`;
    h += `<line id="hov" y1="${p.t}" y2="${H - p.b}" stroke="${css("--text-muted")}" stroke-width="1" visibility="hidden"/>`;
    svg.innerHTML = h;
    $("clegend").innerHTML = S.meta.basins.map((b, k) => `<span><i style="background:${seriesColor(b.id, k)}"></i>${b.name}</span>`).join("");
    moveCursor();
  }
  function niceTicks(max) { const st = [0.05, 0.1, 0.2, 0.25, 0.5, 1, 2].find((s) => max / s <= 4) || 1; const out = []; for (let v = 0; v <= max + 1e-9; v += st) out.push(+v.toFixed(2)); return out; }
  function moveCursor() { const c = document.getElementById("cur"); if (c && CH.x) { c.setAttribute("x1", CH.x(S.step)); c.setAttribute("x2", CH.x(S.step)); } }
  function stepAt(ev) { const r = $("chart").getBoundingClientRect(); const px = (ev.clientX - r.left) * CH.W / r.width; return Math.round((px - CH.pad.l) / (CH.W - CH.pad.l - CH.pad.r) * (CH.n - 1)); }
  $("chart").addEventListener("mousemove", (ev) => {
    if (!S.meta) return; const i = Math.max(0, Math.min(CH.n - 1, stepAt(ev))); const hv = document.getElementById("hov");
    hv.setAttribute("x1", CH.x(i)); hv.setAttribute("x2", CH.x(i)); hv.setAttribute("visibility", "visible");
    const tip = $("tip"); tip.hidden = false;
    tip.innerHTML = `<b>${tmst(S.meta.steps_utc[i])}</b><br>` + S.meta.basins.map((b, k) => `<span class="key"><i style="background:${seriesColor(b.id, k)}"></i>${b.name}: ${f2(b.ts.mean_inhr[i])} in/hr · ${f2(b.ts.cum_in[i])} in total</span>`).join("<br>");
    const r = $("chart").getBoundingClientRect(), wr = tip.parentElement.getBoundingClientRect();
    let left = ev.clientX - wr.left + 12; if (left + 260 > wr.width) left = ev.clientX - wr.left - 270;
    tip.style.left = left + "px"; tip.style.top = (r.top - wr.top + 6) + "px";
  });
  $("chart").addEventListener("mouseleave", () => { $("tip").hidden = true; const hv = document.getElementById("hov"); if (hv) hv.setAttribute("visibility", "hidden"); });
  $("chart").addEventListener("click", (ev) => { if (S.meta) { stop(); setStep(stepAt(ev)); } });

  function hydroChart(svg, M, b, col) {
    const W = svg.clientWidth || 360, H = svg.clientHeight || 120, p = { l: 40, r: 8, t: 8, b: 18 };
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const t = M.hydrograph.t_utc.map((s) => new Date(s).getTime()), q = M.hydrograph.Q_cfs;
    const refs = b.channel ? [{ v: b.channel.benchmark_cfs, l: "design " + fN(b.channel.benchmark_cfs) }] : [];
    const qmax = Math.max(...q, 1) * 1.15;
    const ymax = Math.max(qmax, ...refs.map((r) => (r.v < qmax * 2.5 ? r.v * 1.08 : 0)));
    const x = (v) => p.l + (W - p.l - p.r) * (v - t[0]) / (t[t.length - 1] - t[0]), y = (v) => H - p.b - (H - p.t - p.b) * v / ymax;
    let h = "";
    const st = [10, 20, 50, 100, 200, 250, 500, 1000, 2000, 5000].find((s) => ymax / s <= 4) || 5000;
    for (let v = 0; v <= ymax; v += st) h += `<line x1="${p.l}" x2="${W - p.r}" y1="${y(v)}" y2="${y(v)}" stroke="${css("--grid")}"/><text x="${p.l - 4}" y="${y(v) + 3}" text-anchor="end">${fN(v)}</text>`;
    refs.forEach((r) => { if (r.v <= ymax) h += `<line x1="${p.l}" x2="${W - p.r}" y1="${y(r.v)}" y2="${y(r.v)}" stroke="${css("--crit")}" stroke-dasharray="4 3"/><text x="${W - p.r}" y="${y(r.v) - 3}" text-anchor="end">${r.l} cfs</text>`; });
    h += `<path d="${q.map((v, i) => `${i ? "L" : "M"}${x(t[i]).toFixed(1)},${y(v).toFixed(1)}`).join("")}" fill="none" stroke="${col}" stroke-width="2"/>`;
    const k = q.indexOf(Math.max(...q));
    h += `<circle cx="${x(t[k])}" cy="${y(q[k])}" r="4" fill="${col}" stroke="${css("--surface-1")}" stroke-width="2"/><text x="${x(t[k]) + 6}" y="${y(q[k]) - 4}">${fN(q[k])} cfs</text>`;
    h += `<text x="${p.l}" y="${H - 4}">${fmtShort.format(new Date(t[0]))}</text><text x="${W - p.r}" y="${H - 4}" text-anchor="end">${fmtShort.format(new Date(t[t.length - 1]))}</text>`;
    svg.innerHTML = h;
    svg.onmousemove = (ev) => { const r = svg.getBoundingClientRect(); const tv = t[0] + ((ev.clientX - r.left) * W / r.width - p.l) / (W - p.l - p.r) * (t[t.length - 1] - t[0]);
      let i = 0; while (i < t.length - 1 && t[i] < tv) i++; svg.querySelector("title") || svg.insertAdjacentHTML("afterbegin", "<title></title>");
      svg.querySelector("title").textContent = `${tmst(new Date(t[i]).toISOString())}: ${fN(q[i])} cfs`; };
  }

  function showTable() {
    const rows = S.meta.steps_utc.map((ts, i) => `<tr><td>${tmst(ts)}</td>${S.meta.basins.map((b) => `<td>${f2(b.ts.mean_inhr[i])}</td><td>${f2(b.ts.cum_in[i])}</td>`).join("")}</tr>`).filter((_, i) => S.meta.basins.some((b) => b.ts.mean_inhr[i] > 0.005));
    modal(`<h3>Basin-average rain (rainy steps)</h3><table class="t"><tr><th>Time</th>${S.meta.basins.map((b) => `<th>${b.name} in/hr</th><th>cum in</th>`).join("")}</tr>${rows.join("")}</table>`);
  }

  // ---- wiring ---------------------------------------------------------------------------
  $("slider").addEventListener("input", (e) => { stop(); setStep(+e.target.value); });
  $("prev").onclick = () => { stop(); setStep(S.step - 1); };
  $("next").onclick = () => { stop(); setStep(S.step + 1); };
  $("play").onclick = play;
  $("speed").onchange = () => { if (S.playing) { stop(); play(); } };
  $("jumpPeak").onclick = () => { stop(); setStep(peakStep()); };
  $("tableBtn").onclick = showTable;
  $("op").oninput = () => radarOverlay.setOpacity(+$("op").value);
  $("cellsOn").onchange = (e) => Object.values(cellLayers).forEach((l) => e.target.checked ? l.addTo(map) : map.removeLayer(l));
  document.querySelectorAll(".seg button").forEach((b) => b.onclick = () => {
    document.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("on", x === b)); S.radar = b.dataset.r; drawFrame(); legend(); });
  document.addEventListener("keydown", (e) => { if (!S.meta || e.target.tagName === "SELECT" || e.target.tagName === "INPUT") return;
    if (e.key === "ArrowRight") { stop(); setStep(S.step + 1); } else if (e.key === "ArrowLeft") { stop(); setStep(S.step - 1); } else if (e.key === " ") { e.preventDefault(); play(); } });
  let rz; window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { if (S.meta) { buildChart(); } }, 200); });

  (async function main() {
    await initMap();
    let idx;
    try { idx = await getJSON("data/events.json"); } catch (e) { $("side").innerHTML = '<div class="loading">No data published yet.</div>'; return; }
    const sel = $("event");
    idx.events.forEach((e) => { const o = document.createElement("option"); o.value = e.id; o.textContent = e.live ? "● Live – " + e.name : e.name; sel.appendChild(o); });
    const pick = (location.hash || "").replace("#", "");
    const ev0 = idx.events.find((e) => e.id === pick) || idx.events.find((e) => !e.live) || idx.events[0];
    sel.value = ev0.id;
    sel.onchange = () => { const e = idx.events.find((x) => x.id === sel.value); try { location.hash = e.id.replace(/[^A-Za-z0-9-]/g, "-"); } catch (er) { /* sandboxed */ } loadEvent(e).catch(err); };
    loadEvent(ev0).catch(err);
  })();
  function err(e) { console.error(e); $("side").innerHTML = `<div class="note">Could not load data: ${e.message}</div>`; }
})();
