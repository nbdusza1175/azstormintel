/* AZ Storm Intel — read an uploaded basin (StreamStats KMZ / KML / GeoJSON) in the browser.
 *
 *   const b = await AZBasin.read(file)   ->  { name, geometry, outlet: [lon, lat] | null, area_sqmi, vertices, notes[] }
 *   AZBasin.simplify(b, maxChars)        ->  same basin with a geometry small enough to send
 *
 * StreamStats exports hold the watershed polygon ("globalwatershed") and its pour point
 * ("globalwatershedpoint"); the point becomes the outlet. Coordinates must be lon/lat (WGS84). */
(function () {
  "use strict";

  // ---- KMZ = zip archive holding a .kml -------------------------------------------------
  async function unzipFirstKml(buf) {
    const dv = new DataView(buf), u8 = new Uint8Array(buf);
    let eocd = -1;
    for (let i = buf.byteLength - 22; i >= Math.max(0, buf.byteLength - 66000); i--) if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    if (eocd < 0) throw new Error("This KMZ is not a valid zip archive.");
    const count = dv.getUint16(eocd + 10, true);
    let p = dv.getUint32(eocd + 16, true);
    const td = new TextDecoder();
    const entries = [];
    for (let k = 0; k < count; k++) {
      if (dv.getUint32(p, true) !== 0x02014b50) break;
      const method = dv.getUint16(p + 10, true), csize = dv.getUint32(p + 20, true);
      const nlen = dv.getUint16(p + 28, true), xlen = dv.getUint16(p + 30, true), clen = dv.getUint16(p + 32, true);
      const off = dv.getUint32(p + 42, true);
      entries.push({ name: td.decode(u8.subarray(p + 46, p + 46 + nlen)), method, csize, off });
      p += 46 + nlen + xlen + clen;
    }
    const e = entries.find((x) => /\.kml$/i.test(x.name));
    if (!e) throw new Error("No .kml file found inside the KMZ.");
    const lh = e.off, start = lh + 30 + dv.getUint16(lh + 26, true) + dv.getUint16(lh + 28, true);
    const data = u8.subarray(start, start + e.csize);
    if (e.method === 0) return td.decode(data);
    if (e.method !== 8) throw new Error("Unsupported KMZ compression.");
    if (typeof DecompressionStream === "undefined") throw new Error("This browser can't open KMZ files; upload the KML or GeoJSON instead.");
    const ds = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
    return new Response(ds).text();
  }

  // ---- KML -> features ---------------------------------------------------------------------------
  function kmlFeatures(text) {
    const doc = new DOMParser().parseFromString(text, "application/xml");
    if (doc.getElementsByTagName("parsererror").length) throw new Error("The KML could not be read.");
    const coords = (el) => (el ? el.textContent.trim().split(/\s+/).map((t) => t.split(",").map(Number)).filter((c) => c.length >= 2 && isFinite(c[0]) && isFinite(c[1])).map((c) => [c[0], c[1]]) : []);
    const byTag = (el, tag) => Array.from(el.getElementsByTagNameNS("*", tag));
    const out = [];
    for (const pm of byTag(doc, "Placemark")) {
      const nm = (byTag(pm, "name")[0] || {}).textContent || "";
      for (const pg of byTag(pm, "Polygon")) {
        const outer = coords(byTag(byTag(pg, "outerBoundaryIs")[0] || pg, "coordinates")[0]);
        const holes = byTag(pg, "innerBoundaryIs").map((ib) => coords(byTag(ib, "coordinates")[0]));
        if (outer.length >= 4) out.push({ type: "Feature", properties: { name: nm.trim() }, geometry: { type: "Polygon", coordinates: [outer, ...holes] } });
      }
      for (const pt of byTag(pm, "Point")) {
        const c = coords(byTag(pt, "coordinates")[0])[0];
        if (c) out.push({ type: "Feature", properties: { name: nm.trim() }, geometry: { type: "Point", coordinates: c } });
      }
    }
    const docName = (byTag(doc, "Document")[0] && Array.from(byTag(doc, "Document")[0].children).find((c) => c.localName === "name")) || null;
    return { features: out, name: docName ? docName.textContent.trim() : "" };
  }

  // ---- GeoJSON (plain, Feature, FeatureCollection, or a StreamStats service response) -------------
  function geojsonFeatures(obj) {
    const out = [];
    const walk = (o, label) => {
      if (!o || typeof o !== "object") return;
      if (Array.isArray(o)) { o.forEach((x) => walk(x, label)); return; }
      if (o.type === "FeatureCollection") { (o.features || []).forEach((f) => walk(f, label)); return; }
      if (o.type === "Feature" && o.geometry) { out.push({ type: "Feature", properties: { ...(o.properties || {}), _label: label || o.id || "" }, geometry: o.geometry }); return; }
      if (["Polygon", "MultiPolygon", "Point"].includes(o.type) && o.coordinates) { out.push({ type: "Feature", properties: { _label: label || "" }, geometry: o }); return; }
      if (o.type === "GeometryCollection") { (o.geometries || []).forEach((g) => walk(g, label)); return; }
      for (const k of Object.keys(o)) walk(o[k], o.name || label);  // StreamStats: {featurecollection:[{name, feature}]}
    };
    walk(obj, "");
    const crs = obj && obj.crs && obj.crs.properties && obj.crs.properties.name;
    return { features: out, crs: crs || "" };
  }

  // ---- geometry helpers -----------------------------------------------------------------------------
  const R = 6378137, RAD = Math.PI / 180;
  function ringArea(c) { let a = 0; for (let i = 0; i < c.length - 1; i++) a += (c[i + 1][0] - c[i][0]) * RAD * (2 + Math.sin(c[i][1] * RAD) + Math.sin(c[i + 1][1] * RAD)); return Math.abs(a * R * R / 2); }
  function polysOf(g) { return g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : []; }
  function areaSqmi(g) { return polysOf(g).reduce((s, p) => s + ringArea(p[0]) - p.slice(1).reduce((t, h) => t + ringArea(h), 0), 0) / 2589988.11; }
  function vertexCount(g) { return polysOf(g).reduce((s, p) => s + p.reduce((t, r) => t + r.length, 0), 0); }
  function bbox(g) { let b = [180, 90, -180, -90]; polysOf(g).forEach((p) => p[0].forEach(([x, y]) => { b = [Math.min(b[0], x), Math.min(b[1], y), Math.max(b[2], x), Math.max(b[3], y)]; })); return b; }
  function pointInRing(pt, r) { let ins = false; for (let i = 0, j = r.length - 1; i < r.length; j = i++) { const [xi, yi] = r[i], [xj, yj] = r[j]; if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi) ins = !ins; } return ins; }
  function distToGeom(pt, g) {  // rough degrees distance to the polygon (0 if inside)
    let best = Infinity;
    for (const p of polysOf(g)) {
      if (pointInRing(pt, p[0])) return 0;
      const r = p[0];
      for (let i = 0; i < r.length - 1; i++) {
        const [x1, y1] = r[i], [x2, y2] = r[i + 1], dx = x2 - x1, dy = y2 - y1;
        const t = Math.max(0, Math.min(1, ((pt[0] - x1) * dx + (pt[1] - y1) * dy) / (dx * dx + dy * dy || 1)));
        best = Math.min(best, Math.hypot(pt[0] - x1 - t * dx, pt[1] - y1 - t * dy));
      }
    }
    return best;
  }

  // Douglas-Peucker on one ring (lon/lat, tolerance in degrees), keeps the ring closed and >= 4 points
  function dp(pts, tol) {
    if (pts.length <= 4) return pts;
    const keep = new Uint8Array(pts.length); keep[0] = keep[pts.length - 1] = 1;
    const stack = [[0, pts.length - 1]];
    while (stack.length) {
      const [a, b] = stack.pop(); let idx = -1, dmax = 0;
      const [x1, y1] = pts[a], [x2, y2] = pts[b], dx = x2 - x1, dy = y2 - y1, L = Math.hypot(dx, dy);
      for (let i = a + 1; i < b; i++) {
        const d = L ? Math.abs(dy * pts[i][0] - dx * pts[i][1] + x2 * y1 - y2 * x1) / L : Math.hypot(pts[i][0] - x1, pts[i][1] - y1);
        if (d > dmax) { dmax = d; idx = i; }
      }
      if (dmax > tol && idx > 0) { keep[idx] = 1; stack.push([a, idx], [idx, b]); }
    }
    const out = pts.filter((_, i) => keep[i]);
    return out.length >= 4 ? out : pts;
  }
  const round5 = (c) => [Math.round(c[0] * 1e5) / 1e5, Math.round(c[1] * 1e5) / 1e5];
  function simplifyGeom(g, tol) {
    const sp = (p) => p.map((r) => dp(r.map(round5), tol)).filter((r, i) => i === 0 || ringArea(r) > 2e4);
    return g.type === "Polygon" ? { type: "Polygon", coordinates: sp(g.coordinates) } : { type: "MultiPolygon", coordinates: g.coordinates.map(sp) };
  }

  /** Smallest tolerance (from ~1 m up) whose JSON fits maxChars. Returns {geometry, tol_m}. */
  function simplify(geometry, maxChars) {
    let tol = 0.00001;
    for (let k = 0; k < 18; k++) {
      const g = simplifyGeom(geometry, tol);
      if (JSON.stringify(g).length <= maxChars) return { geometry: g, tol_m: Math.round(tol * 111000) };
      tol *= 1.6;
    }
    throw new Error("The basin outline is too complex to send.");
  }

  async function read(file) {
    const nm = file.name || "basin";
    const ext = (nm.split(".").pop() || "").toLowerCase();
    let feats, label = "";
    if (ext === "kmz") { const k = kmlFeatures(await unzipFirstKml(await file.arrayBuffer())); feats = k.features; label = k.name; }
    else if (ext === "kml") { const k = kmlFeatures(await file.text()); feats = k.features; label = k.name; }
    else if (ext === "geojson" || ext === "json") {
      let obj; try { obj = JSON.parse(await file.text()); } catch (e) { throw new Error("The GeoJSON file could not be read."); }
      const g = geojsonFeatures(obj);
      if (g.crs && !/4326|CRS84|4269/i.test(g.crs)) throw new Error(`The GeoJSON is in ${g.crs}; export it in WGS84 (longitude/latitude).`);
      feats = g.features;
    } else throw new Error("Upload a .kmz, .kml or .geojson file.");

    const polys = feats.filter((f) => f.geometry.type === "Polygon" || f.geometry.type === "MultiPolygon");
    if (!polys.length) throw new Error("No basin polygon found in the file.");
    // prefer the StreamStats watershed; otherwise merge all polygons into one MultiPolygon
    const ws = polys.find((f) => /globalwatershed$/i.test(f.properties._label || f.properties.name || "")) || null;
    const coords = [];
    (ws ? [ws] : polys).forEach((f) => polysOf(f.geometry).forEach((p) => coords.push(p)));
    const geometry = coords.length === 1 ? { type: "Polygon", coordinates: coords[0] } : { type: "MultiPolygon", coordinates: coords };
    const b = bbox(geometry);
    if (b[0] < -180 || b[2] > 180 || b[1] < -90 || b[3] > 90 || Math.abs(b[0]) < 1e-9)
      throw new Error("The coordinates don't look like longitude/latitude; export the basin in WGS84.");

    const notes = [];
    const pts = feats.filter((f) => f.geometry.type === "Point");
    const pp = pts.find((f) => /globalwatershedpoint|pour|outlet/i.test(f.properties._label || f.properties.name || "")) || pts[0] || null;
    let outlet = null;
    if (pp) {
      const c = pp.geometry.coordinates;
      if (distToGeom(c, geometry) <= 0.01) outlet = [c[0], c[1]];
      else notes.push("The point in the file is more than about 1 km from the basin, so it was not used as the outlet.");
    }
    const props = (ws || polys[0]).properties;
    const fromProps = props.Name || props.name || props.NAME || "";
    let name = [fromProps, label].find((s) => s && !/^(globalwatershed|untitled|layer)/i.test(s)) || nm.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ");
    return { name: name.slice(0, 60), geometry, outlet, area_sqmi: areaSqmi(geometry), vertices: vertexCount(geometry), bbox: b, notes };
  }

  window.AZBasin = { read, simplify, areaSqmi, vertexCount };
})();
