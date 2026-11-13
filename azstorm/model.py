#!/usr/bin/env python3
"""
coconino_storm.py  -  Coconino County Storm & Flood Intensity Tool
====================================================================
v3.3.0  (azstormintel pipeline build, Nov 2026)

v3.3 changes vs v3.1.1:
  * channel velocity at the routed discharge (kinematic loop, v3.2 method note)
  * rain ceiling cross-check i(Tc)*A
  * StreamStats "N-percent AEP" CSV parsing
  * design check vs a list of benchmarks (Mountain Dell: 100-yr 24-hr vs IEA/FIS)
  * site outputs (JSON + PNG layers) instead of folium

Method: ModClark  =  distributed SCS Curve Number losses
                   + time-area translation on a DEM travel-time grid
                   + one Clark linear reservoir (regional R, frozen)

    excess_cell(t) = dQ_cn( cumulative radar rain on that cell, CN_cell(AMC) )
    I(t)           = sum_cells excess_cell(t - t_cell) * A_cell        (translation)
    O(t)           = O(t-1) + cA * (I(t) - O(t-1)),  cA = dt/(R + dt/2) (attenuation)

Principles (restart brief):
  * radar + ground conditions -> answer. Nothing is fitted to an event.
  * gauges are checks only: they are reported/flagged, never used to adjust.
  * no structure modelling (natural, unregulated response at the outlet).
  * any calibration is regional, offline and frozen in the parameter files.

Modules (sections below):
  0 utilities / config        1 grid + terrain (priority-flood, D8, travel time)
  2 ground conditions (NLCD, SSURGO HSG, CN, AMC)
  3 radar (MRMS fetch/decode, rain fields, antecedent API, snow flag)
  4 runoff + routing (ModClark, cross-checks, design storm)
  5 frequency (Atlas 14 + TP-29 ARF, StreamStats peak-flow)
  6 gauge checks (ALERT parsing, rain vs radar, stage timing)
  7 outputs (summary text/JSON, Leaflet map, figures)
  8 driver (run_basin / run_event / CLI)
"""

import os, io, re, json, gzip, math, glob, heapq, ast, time, csv, base64, tempfile, warnings
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd

VERSION = "3.3.0"

warnings.filterwarnings("ignore", category=RuntimeWarning)

# =============================================================================
# 0. UTILITIES / CONFIG
# =============================================================================
_LOG = []


def log(msg):
    print(msg, flush=True)
    _LOG.append(str(msg))


def deep_merge(a, b):
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path="basins.json"):
    with open(path) as f:
        cfg = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    cfg["_base"] = base
    with open(os.path.join(base, cfg["common"]["cn_table"])) as f:
        cfg["_cn"] = json.load(f)
    return cfg


def find_file(base, pattern, exclude=None):
    """First file (case-insensitive glob) in base, cwd, base/data or base/gauges, else None."""
    import fnmatch
    if not pattern:
        return None
    pats = pattern if isinstance(pattern, list) else [pattern]
    exc = [e.lower() for e in (exclude if isinstance(exclude, list) else ([exclude] if exclude else []))]
    for p in pats:
        pl = p.lower()
        for d in (base, os.getcwd(), os.path.join(base, "data"), os.path.join(base, "gauges"),
                  os.path.join(base, "..", "data", "basins"), os.path.join(base, "..", "data", "streamstats"),
                  os.path.join(base, "..", "data", "gauges")):
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                f = fn.lower()
                if fnmatch.fnmatch(f, pl) and not any(fnmatch.fnmatch(f, e) for e in exc) and os.path.isfile(os.path.join(d, fn)):
                    return os.path.join(d, fn)
    return None


def _http():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = f"coconino_storm/{VERSION} (Coconino County FCD research)"
    return s


def http_get(url, params=None, timeout=120, tries=4, binary=True):
    s = _http()
    last = None
    for k in range(tries):
        try:
            r = s.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.content if binary else r.text
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404):
                break
        except Exception as e:  # network error
            last = str(e)
        time.sleep(3.0 * (k + 1))
    raise IOError(f"GET failed {url}: {last}")


def fmt_T(T, q=""):
    if T is None or (isinstance(T, float) and not np.isfinite(T)):
        return "n/a"
    if q == "<":
        return f"<{T:g}-yr"
    if q == ">":
        return f">{T:g}-yr"
    if T < 10:
        return f"{T:.1f}-yr"
    return f"{T:.0f}-yr"


def norm_ppf(p):
    """Inverse standard normal (Acklam), no scipy dependency."""
    p = np.clip(np.asarray(p, float), 1e-12, 1 - 1e-12)
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    pl = 0.02425
    out = np.empty_like(p)
    lo = p < pl
    hi = p > 1 - pl
    mid = ~(lo | hi)
    q = np.sqrt(-2 * np.log(p[lo]))
    out[lo] = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p[mid] - 0.5
    r = q * q
    out[mid] = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = np.sqrt(-2 * np.log(1 - p[hi]))
    out[hi] = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    return out


def _z_of_T(T):
    """Normal deviate of non-exceedance for return period T (annual-max style)."""
    T = np.asarray(T, float)
    return norm_ppf(1.0 - 1.0 / T)


def return_period(value, Ts, vals):
    """Interpolate return period: log(value) vs normal deviate of AEP.
    Returns (T, qualifier) with qualifier '', '<' or '>'."""
    Ts = np.asarray(Ts, float)
    vals = np.asarray(vals, float)
    ok = np.isfinite(vals) & (vals > 0)
    Ts, vals = Ts[ok], vals[ok]
    if value is None or not np.isfinite(value) or value <= 0 or len(vals) < 2:
        return (float("nan"), "")
    o = np.argsort(vals)
    Ts, vals = Ts[o], vals[o]
    if value < vals[0]:
        return (float(Ts[0]), "<")
    if value > vals[-1]:
        return (float(Ts[-1]), ">")
    z = _z_of_T(np.maximum(Ts, 1.0001))
    zt = np.interp(np.log(value), np.log(vals), z)
    # invert z -> T
    lo, hi = 1.0001, 1e5
    for _ in range(80):
        m = math.sqrt(lo * hi)
        if _z_of_T(m) < zt:
            lo = m
        else:
            hi = m
    return (float(math.sqrt(lo * hi)), "")


# =============================================================================
# 1. GRID + TERRAIN
# =============================================================================
class Grid:
    """North-up raster grid in a projected CRS (metres)."""

    def __init__(self, x0, y0, res, nx, ny, crs="EPSG:26912"):
        self.x0, self.y0, self.res, self.nx, self.ny, self.crs = float(x0), float(y0), float(res), int(nx), int(ny), crs

    @classmethod
    def from_bounds(cls, xmin, ymin, xmax, ymax, res, crs):
        xmin = math.floor(xmin / res) * res
        ymax = math.ceil(ymax / res) * res
        nx = int(math.ceil((xmax - xmin) / res))
        ny = int(math.ceil((ymax - ymin) / res))
        return cls(xmin, ymax, res, nx, ny, crs)

    @property
    def transform(self):
        from rasterio.transform import Affine
        return Affine(self.res, 0, self.x0, 0, -self.res, self.y0)

    @property
    def bounds(self):
        return (self.x0, self.y0 - self.ny * self.res, self.x0 + self.nx * self.res, self.y0)

    def centers(self, flat_idx=None):
        if flat_idx is None:
            r, c = np.indices((self.ny, self.nx))
            r, c = r.ravel(), c.ravel()
        else:
            r, c = np.divmod(np.asarray(flat_idx), self.nx)
        return self.x0 + (c + 0.5) * self.res, self.y0 - (r + 0.5) * self.res

    def rowcol(self, x, y):
        return int((self.y0 - y) // self.res), int((x - self.x0) // self.res)


def transformer(src, dst):
    from pyproj import Transformer
    return Transformer.from_crs(src, dst, always_xy=True)


def load_polygon(path, crs):
    """Read a GeoJSON polygon (any CRS declared, default WGS84) -> shapely in `crs`."""
    from shapely.geometry import shape
    from shapely.ops import unary_union, transform as stransform
    with open(path) as f:
        gj = json.load(f)
    feats = gj["features"] if gj.get("type") == "FeatureCollection" else [gj if gj.get("type") == "Feature" else {"geometry": gj}]
    geom = unary_union([shape(ft["geometry"]) for ft in feats if ft.get("geometry")])
    src = "EPSG:4326"
    try:
        name = gj.get("crs", {}).get("properties", {}).get("name", "")
        m = re.search(r"EPSG::?(\d+)", name)
        if m and m.group(1) not in ("4326", "4269"):
            src = f"EPSG:{m.group(1)}"
    except Exception:
        pass
    tr = transformer(src, crs)
    return stransform(lambda x, y, z=None: tr.transform(x, y), geom)


def warp_to_grid(arr, src_transform, src_crs, grid, nearest=True, nodata=None):
    from rasterio.warp import reproject, Resampling
    dst = np.full((grid.ny, grid.nx), np.nan if not nearest else (nodata if nodata is not None else 0),
                  dtype=np.float64 if not nearest else arr.dtype)
    reproject(arr, dst, src_transform=src_transform, src_crs=src_crs, dst_transform=grid.transform,
              dst_crs=grid.crs, resampling=Resampling.nearest if nearest else Resampling.bilinear,
              src_nodata=nodata, dst_nodata=nodata)
    return dst


def read_tif_to_grid(path, grid, nearest=True):
    import rasterio
    with rasterio.open(path) as ds:
        a = ds.read(1)
        nd = ds.nodata
        return warp_to_grid(a, ds.transform, ds.crs, grid, nearest=nearest, nodata=nd)


def get_dem(grid, bcfg, ctx):
    """USGS 3DEP DEM (m) on the grid. Local GeoTIFF override via bcfg['dem_tif']."""
    p = find_file(ctx["base"], bcfg.get("dem_tif"))
    if p:
        log(f"  DEM: local {os.path.basename(p)}")
        z = read_tif_to_grid(p, grid, nearest=False).astype(float)
        z[z < -500] = np.nan
        return z
    cache = os.path.join(ctx["cache"], f"dem_{grid.x0:.0f}_{grid.y0:.0f}_{grid.res:g}_{grid.nx}x{grid.ny}.npy")
    if os.path.exists(cache):
        return np.load(cache)
    z, src = None, ""
    try:
        z, src = _dem_exportimage(grid), "3DEP ImageServer"
    except Exception as e:
        log(f"  3DEP ImageServer failed ({str(e)[:80]}); using TNM 1/3 arc-second COG tiles (AWS)")
    if z is None:
        z, src = _dem_cog(grid), "TNM 1/3 arc-second COG (AWS)"
    np.save(cache, z)
    log(f"  DEM: USGS 3DEP {grid.res:g} m via {src}, {grid.nx}x{grid.ny}")
    return z


def _dem_exportimage(grid, tile=1000):
    from rasterio.io import MemoryFile
    epsg = grid.crs.split(":")[1]
    url = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
    z = np.full((grid.ny, grid.nx), np.nan)
    for r0 in range(0, grid.ny, tile):
        for c0 in range(0, grid.nx, tile):
            h, w = min(tile, grid.ny - r0), min(tile, grid.nx - c0)
            xmin = grid.x0 + c0 * grid.res
            ymax = grid.y0 - r0 * grid.res
            params = dict(bbox=f"{xmin},{ymax - h * grid.res},{xmin + w * grid.res},{ymax}", bboxSR=epsg,
                          imageSR=epsg, size=f"{w},{h}", format="tiff", pixelType="F32",
                          interpolation="RSP_BilinearInterpolation", noDataInterpretation="esriNoDataMatchAny",
                          f="image")
            raw = http_get(url, params, timeout=180, tries=3)
            with MemoryFile(raw) as mf, mf.open() as ds:
                a = ds.read(1).astype(float)
            a[a < -500] = np.nan
            z[r0:r0 + h, c0:c0 + w] = a[:h, :w]
    return z


DEM_COG_URL = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/{t}/USGS_13_{t}.tif"


def _dem_cog(grid, url_fmt=None):
    """USGS 3DEP 1/3 arc-second (~10 m) 1x1-degree COG tiles, windowed read + warp to grid."""
    import rasterio
    from rasterio.vrt import WarpedVRT
    from rasterio.enums import Resampling
    url_fmt = url_fmt or DEM_COG_URL
    tr = transformer(grid.crs, "EPSG:4326")
    xmin, ymin, xmax, ymax = grid.bounds
    lons, lats = tr.transform([xmin, xmin, xmax, xmax], [ymin, ymax, ymin, ymax])
    z = np.full((grid.ny, grid.nx), np.nan)
    env = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MAX_RETRY="4", GDAL_HTTP_RETRY_DELAY="3",
               CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif")
    with rasterio.Env(**env):
        for la in range(int(math.floor(min(lats))), int(math.floor(max(lats))) + 1):
            for lo in range(int(math.floor(min(lons))), int(math.floor(max(lons))) + 1):
                t = f"n{la + 1:02d}w{-lo:03d}"
                u = url_fmt.format(t=t)
                with rasterio.open(u) as ds:
                    with WarpedVRT(ds, crs=grid.crs, transform=grid.transform, width=grid.nx, height=grid.ny,
                                   resampling=Resampling.bilinear, src_nodata=ds.nodata, nodata=np.nan,
                                   dtype="float32") as vrt:
                        a = vrt.read(1).astype(float)
                a[a < -500] = np.nan
                z = np.where(np.isnan(z), a, z)
    if np.isnan(z).mean() > 0.5:
        raise IOError("DEM COG read returned mostly nodata")
    return z


NBR = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def priority_flood(z, valid, seeds, nx, eps=1e-4):
    """Priority-flood + epsilon (Barnes 2014). Pure python (no pysheds/numba).
    Returns filled surface and 'discoverer' receiver (drainage tree)."""
    n = z.size
    zl = z.ravel().tolist()
    vl = valid.ravel().tolist()
    filled = list(zl)
    closed = bytearray(n)
    rcv = [-1] * n
    heap = []
    for s in seeds:
        if not closed[s]:
            closed[s] = 1
            heap.append((zl[s], s))
    heapq.heapify(heap)
    ny = n // nx
    pop, push = heapq.heappop, heapq.heappush
    offs = NBR
    while heap:
        zc, c = pop(heap)
        r, cc = divmod(c, nx)
        for dr, dc in offs:
            rr = r + dr
            c2 = cc + dc
            if rr < 0 or rr >= ny or c2 < 0 or c2 >= nx:
                continue
            k = rr * nx + c2
            if closed[k] or not vl[k]:
                continue
            closed[k] = 1
            rcv[k] = c
            zk = zl[k]
            if zk <= zc:
                zk = zc + eps
            filled[k] = zk
            push(heap, (zk, k))
    return np.array(filled).reshape(z.shape), np.array(rcv, dtype=np.int64), np.frombuffer(bytes(closed), np.uint8).astype(bool)


def d8_receivers(filled, valid, discoverer, seeds, res):
    """Steepest-descent D8 on the epsilon-filled surface (every non-seed cell has
    a strictly lower neighbour). Falls back to the flood discoverer."""
    ny, nx = filled.shape
    best = np.zeros((ny, nx))
    rcv = discoverer.reshape(ny, nx).copy()
    idx = np.arange(ny * nx).reshape(ny, nx)
    F = np.where(valid, filled, np.inf)
    for dr, dc in NBR:
        dist = res * (math.sqrt(2) if dr and dc else 1.0)
        nb = np.full((ny, nx), np.inf)
        ni = np.full((ny, nx), -1)
        rs = slice(max(0, -dr), ny - max(0, dr))
        cs = slice(max(0, -dc), nx - max(0, dc))
        rd = slice(max(0, dr), max(0, dr) + (rs.stop - rs.start))
        cd = slice(max(0, dc), max(0, dc) + (cs.stop - cs.start))
        nb[rs, cs] = F[rd, cd]
        ni[rs, cs] = idx[rd, cd]
        s = (F - nb) / dist
        upd = (s > best) & valid & np.isfinite(nb)
        best[upd] = s[upd]
        rcv[upd] = ni[upd]
    rcv = rcv.ravel()
    rcv[np.asarray(seeds)] = -1
    rcv[~valid.ravel()] = -1
    return rcv


def jump(rcv, steps):
    """Cell index `steps` downstream (stops at terminal)."""
    P = np.arange(rcv.size)
    for _ in range(steps):
        nxt = rcv[P]
        P = np.where(nxt >= 0, nxt, P)
    return P


def terrain_model(grid, z, bcfg, ctx, polygon=None, outlet_xy=None):
    """Delineate (or constrain to polygon), route, return terrain dict."""
    ny, nx, res = grid.ny, grid.nx, grid.res
    valid = np.isfinite(z)
    zf = np.where(valid, z, 0.0)
    mode = bcfg.get("_mode")
    if mode == "polygon":
        from rasterio.features import rasterize
        pm = rasterize([(polygon, 1)], out_shape=(ny, nx), transform=grid.transform, all_touched=False).astype(bool)
        pm &= valid
        # outlet = lowest cell inside polygon within snap radius of the point
        r0, c0 = grid.rowcol(*outlet_xy)
        rad = max(2, int(bcfg["snap_radius_m"] / res))
        best, oc = np.inf, None
        for r in range(max(0, r0 - rad), min(ny, r0 + rad + 1)):
            for c in range(max(0, c0 - rad), min(nx, c0 + rad + 1)):
                if pm[r, c] and zf[r, c] < best:
                    best, oc = zf[r, c], r * nx + c
        if oc is None:
            raise ValueError("outlet not inside polygon")
        filled, disc, _ = priority_flood(zf, pm, [oc], nx)
        rcv = d8_receivers(filled, pm, disc, [oc], res)
        valid_dom = pm
        outlet = oc
    else:
        border = np.zeros((ny, nx), bool)
        border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
        nb_nan = ~valid
        if nb_nan.any():
            from scipy.ndimage import binary_dilation
            border |= binary_dilation(nb_nan) & valid
        seeds = np.flatnonzero(border & valid)
        filled, disc, _ = priority_flood(zf, valid, seeds.tolist(), nx)
        rcv = d8_receivers(filled, valid, disc, seeds, res)
        valid_dom = valid
        outlet = None

    order_up = np.argsort(-filled.ravel(), kind="stable")  # upstream first
    rl = rcv.tolist()
    ol = order_up.tolist()
    acc = [1] * rcv.size
    for c in ol:
        r = rl[c]
        if r >= 0:
            acc[r] += acc[c]
    acc = np.array(acc)

    if outlet is None:  # snap to max accumulation near outlet point
        r0, c0 = grid.rowcol(*outlet_xy)
        rad = max(2, int(bcfg["snap_radius_m"] / res))
        sub = acc.reshape(ny, nx)[max(0, r0 - rad):r0 + rad + 1, max(0, c0 - rad):c0 + rad + 1]
        rr, cc = np.unravel_index(np.argmax(sub), sub.shape)
        outlet = (max(0, r0 - rad) + rr) * nx + max(0, c0 - rad) + cc

    # basin membership (downstream-first loop)
    inb = [False] * rcv.size
    inb[outlet] = True
    for c in reversed(ol):
        r = rl[c]
        if r >= 0 and inb[r]:
            inb[c] = True
    inb = np.array(inb)
    rcv_b = np.where(inb, rcv, -1)
    rcv_b[outlet] = -1

    # segment distance to receiver
    seg = np.zeros(rcv.size)
    has = rcv_b >= 0
    rr_, cc_ = np.divmod(np.arange(rcv.size), nx)
    rr2, cc2 = np.divmod(np.where(has, rcv_b, 0), nx)
    diag = has & (rr_ != rr2) & (cc_ != cc2)
    seg[has] = res
    seg[diag] = res * math.sqrt(2)

    # flow length to outlet (m) and longest upstream length (m)
    L = [0.0] * rcv.size
    segl = seg.tolist()
    rbl = rcv_b.tolist()
    for c in reversed(ol):
        r = rbl[c]
        if r >= 0:
            L[c] = segl[c] + L[r]
    L = np.array(L)
    Lup = [0.0] * rcv.size
    for c in ol:
        r = rbl[c]
        if r >= 0 and inb[c]:
            v = Lup[c] + segl[c]
            if v > Lup[r]:
                Lup[r] = v
    Lup = np.array(Lup)

    touches_edge = False
    m2 = inb.reshape(ny, nx)
    if m2[0, :].any() or m2[-1, :].any() or m2[:, 0].any() or m2[:, -1].any():
        touches_edge = True
    return dict(rcv=rcv_b, acc=acc, inb=inb, outlet=int(outlet), seg=seg, L=L, Lup=Lup,
                filled=filled, z=zf, touches_edge=touches_edge, order_up=order_up)


def travel_time(grid, T, imp, p2_24, tt, q_peak=None, A_total_mi=None):
    """Per-cell travel time to outlet (hr) using TR-55 sheet / shallow and Manning
    channel velocities along D8 paths. Returns dict with t (hr) and regime grid."""
    res = grid.res
    rcv, inb, seg, L, Lup, z = T["rcv"], T["inb"], T["seg"], T["L"], T["Lup"], T["z"].ravel()
    n = rcv.size
    acc_ac = T["acc"] * res * res / 4046.856
    # reach-averaged slope over k cells downstream (reduces 10 m DEM noise)
    k = max(1, int(tt["slope_reach_m"] / res))
    P = jump(rcv, k)
    dL = L - L[P]
    S = np.where(dL > 0, (z - z[P]) / np.maximum(dL, 1e-9), 0.0)
    # 10 m DEM depressions (road embankments / culverts / ponds the DEM cannot see through)
    # are filled flat by the priority flood. Route through them at the slope over a long
    # reach instead of the epsilon slope, and count the time spent there (diagnostic).
    dep = ((T["filled"].ravel() - z) > tt.get("depression_tol_m", 0.3)) & inb
    kL = max(1, int(tt.get("depression_reach_m", 1000) / res))
    P2 = jump(rcv, kL)
    dL2 = L - L[P2]
    S2 = np.where(dL2 > 0, (z - z[P2]) / np.maximum(dL2, 1e-9), 0.0)
    flat = S < tt["min_channel_slope"]
    S = np.where(dep | flat, np.maximum(S, S2), S)
    S_over = np.maximum(S, tt["min_slope"])
    S_ch = np.maximum(S, tt["min_channel_slope"])
    seg_ft = seg * 3.28084
    Lup_ft = Lup * 3.28084
    chan = acc_ac >= tt["channel_threshold_ac"]
    paved = imp.ravel() >= 0.5
    # sheet flow (TR-55): Tt = 0.007 (nL)^0.8 / (P2^0.5 S^0.4)  [hr]
    Ls = tt["sheet_max_ft"]
    nsh = tt["sheet_n"]

    def tsheet(Lft):
        return 0.007 * (nsh * Lft) ** 0.8 / (math.sqrt(p2_24) * S_over ** 0.4)

    sheet_part = np.clip(Ls - Lup_ft, 0, None)
    sheet_part = np.minimum(sheet_part, seg_ft)
    sheet_part[chan] = 0
    t_sheet = np.where(sheet_part > 0, tsheet(np.minimum(Lup_ft + sheet_part, Ls)) - tsheet(np.minimum(Lup_ft, Ls)), 0.0)
    rest = seg_ft - sheet_part
    v_sh = np.where(paved, 20.3282, 16.1345) * np.sqrt(S_over)
    A_mi = acc_ac / 640.0
    Rh = tt["hg_R_coef"] * np.power(np.maximum(A_mi, 1e-6), tt["hg_R_exp"])
    n_ch = np.where(imp.ravel() >= tt.get("developed_imp_frac", 0.3), tt.get("channel_n_developed", tt["channel_n"]), tt["channel_n"])
    if q_peak is not None and A_total_mi:
        # v3.2 kinematic: wide rectangular Manning at the discharge being routed.
        # W = a*A^b ft (generic semiarid curve - verify), Q_cell = Qpeak*(A/A_total)^0.6
        W = tt.get("width_coef", 12.0) * np.power(np.maximum(A_mi, 1e-6), tt.get("width_exp", 0.4))
        Qc = q_peak * np.power(np.clip(A_mi / A_total_mi, 1e-9, 1.0), tt.get("q_area_exp", 0.6))
        y = np.power(np.maximum(Qc / W, 1e-6) * n_ch / (1.49 * np.sqrt(S_ch)), 0.6)
        v_ch = Qc / np.maximum(W * y, 1e-9)
    else:
        v_ch = 1.49 / n_ch * Rh ** (2 / 3) * np.sqrt(S_ch)
    v = np.where(chan, v_ch, v_sh)
    v = np.clip(v, tt["vmin_fps"], tt["vmax_fps"])
    t_seg = t_sheet + rest / v / 3600.0
    t_seg[~inb] = 0
    t_seg[rcv < 0] = 0
    # accumulate downstream-first
    tl = [0.0] * n
    ts = t_seg.tolist()
    rl = rcv.tolist()
    for c in reversed(T["order_up"].tolist()):
        r = rl[c]
        if r >= 0:
            tl[c] = ts[c] + tl[r]
    t = np.array(tl)
    regime = np.where(chan, 3, np.where(sheet_part > 0, 1, 2))
    regime[~inb] = 0
    return dict(t=t, regime=regime, chan=chan & inb, slope_reach=S, acc_ac=acc_ac, t_seg=t_seg, dep=dep, v=v)


# =============================================================================
# 2. GROUND CONDITIONS
# =============================================================================
NLCD_COLORS = {11: (70, 107, 159), 12: (209, 222, 248), 21: (222, 197, 197), 22: (217, 146, 130),
               23: (235, 0, 0), 24: (171, 0, 0), 31: (179, 172, 159), 41: (104, 171, 95),
               42: (28, 95, 44), 43: (181, 197, 143), 52: (204, 184, 121), 71: (223, 223, 194),
               81: (220, 217, 57), 82: (171, 108, 40), 90: (184, 217, 235), 95: (108, 159, 184)}


def _fetch_mrlc_wcs(grid, coverage):
    from rasterio.io import MemoryFile
    tr = transformer(grid.crs, "EPSG:5070")
    xmin, ymin, xmax, ymax = grid.bounds
    xs, ys = tr.transform([xmin, xmin, xmax, xmax], [ymin, ymax, ymin, ymax])
    b = (min(xs) - 120, min(ys) - 120, max(xs) + 120, max(ys) + 120)
    url = "https://www.mrlc.gov/geoserver/mrlc_download/wcs"
    params = [("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"), ("coverageId", coverage),
              ("subset", f"X({b[0]},{b[2]})"), ("subset", f"Y({b[1]},{b[3]})"), ("format", "image/geotiff")]
    raw = http_get(url, params, timeout=240)
    if raw[:2] not in (b"II", b"MM"):
        raise IOError("WCS did not return a GeoTIFF")
    with MemoryFile(raw) as mf, mf.open() as ds:
        a = ds.read(1)
        return warp_to_grid(a, ds.transform, ds.crs, grid, nearest=True, nodata=ds.nodata if ds.nodata is not None else 0)


def _fetch_mrlc_wms(grid, layer):
    from PIL import Image
    epsg = grid.crs.split(":")[1]
    xmin, ymin, xmax, ymax = grid.bounds
    w, h = min(grid.nx, 4000), min(grid.ny, 4000)
    url = "https://www.mrlc.gov/geoserver/mrlc_display/wms"
    params = dict(service="WMS", version="1.1.1", request="GetMap", layers=layer, styles="",
                  srs=f"EPSG:{epsg}", bbox=f"{xmin},{ymin},{xmax},{ymax}", width=w, height=h, format="image/png")
    raw = http_get(url, params, timeout=240)
    im = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB")).astype(int)
    keys = np.array(list(NLCD_COLORS.keys()))
    cols = np.array(list(NLCD_COLORS.values()))
    d = ((im[:, :, None, :] - cols[None, None, :, :]) ** 2).sum(-1)
    cls = keys[np.argmin(d, axis=-1)]
    cls[np.min(d, axis=-1) > 900] = 0
    if (w, h) != (grid.nx, grid.ny):
        ri = (np.arange(grid.ny) * h / grid.ny).astype(int)
        ci = (np.arange(grid.nx) * w / grid.nx).astype(int)
        cls = cls[ri][:, ci]
    return cls


def get_landcover(grid, bcfg, ctx):
    """NLCD class + impervious fraction on grid. Order: local tif > MRLC WCS > MRLC WMS > default."""
    lc = ctx["cfg"]["common"]["landcover"]
    cls, imp, src = None, None, ""
    p = find_file(ctx["base"], bcfg.get("nlcd_tif"))
    if p:
        cls, src = read_tif_to_grid(p, grid).astype(int), f"local {os.path.basename(p)}"
    if cls is None:
        try:
            cls, src = _fetch_mrlc_wcs(grid, lc["wcs_landcover"]).astype(int), "MRLC WCS " + lc["wcs_landcover"]
        except Exception as e:
            log(f"  NLCD WCS failed ({e}); trying WMS")
    if cls is None:
        try:
            cls, src = _fetch_mrlc_wms(grid, lc["wms_landcover"]), "MRLC WMS (colour-decoded) " + lc["wms_landcover"]
        except Exception as e:
            log(f"  NLCD WMS failed ({e}); using default class {lc['default_class']}")
    if cls is None:
        cls, src = np.full((grid.ny, grid.nx), lc["default_class"]), f"DEFAULT class {lc['default_class']} (NLCD unavailable)"
    p = find_file(ctx["base"], bcfg.get("impervious_tif"))
    if p:
        imp = read_tif_to_grid(p, grid).astype(float)
    if imp is None:
        try:
            imp = _fetch_mrlc_wcs(grid, lc["wcs_impervious"]).astype(float)
        except Exception as e:
            log(f"  NLCD impervious WCS failed ({e}); using class-based impervious")
    cnt = ctx["cfg"]["_cn"]
    if imp is None:
        imp = np.zeros(cls.shape)
        for k, v in cnt["class_impervious_fraction"].items():
            imp[cls == int(k)] = v * 100
        src += "; impervious from class means"
    imp = np.where((imp >= 0) & (imp <= 100), imp, 0) / 100.0
    return cls, imp, src


def _sda(query):
    import requests
    r = requests.post("https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest",
                      json={"query": query, "format": "JSON+COLUMNNAME"}, timeout=300)
    r.raise_for_status()
    t = r.json().get("Table", [])
    return t[0], t[1:] if t else ([], [])


def get_hsg(grid, bcfg, ctx):
    """SSURGO dominant-component hydrologic soil group (1=A..4=D, 0 = unmapped)."""
    cache = os.path.join(ctx["cache"], f"hsg_{grid.x0:.0f}_{grid.y0:.0f}_{grid.res:g}_{grid.nx}x{grid.ny}.npy")
    if os.path.exists(cache):
        return np.load(cache), "SSURGO (cache)"
    from shapely import wkt as swkt
    from shapely.ops import transform as stransform
    from shapely.geometry import box
    from rasterio.features import rasterize
    tr = transformer(grid.crs, "EPSG:4326")
    xmin, ymin, xmax, ymax = grid.bounds
    xs, ys = tr.transform([xmin, xmin, xmax, xmax], [ymin, ymax, ymin, ymax])
    aoi = box(min(xs), min(ys), max(xs), max(ys)).wkt
    q = ("SELECT mukey, mupolygongeo.STIntersection(geometry::STGeomFromText('%s',4326)).STAsText() AS wkt "
         "FROM mupolygon WHERE mupolygongeo.STIntersects(geometry::STGeomFromText('%s',4326)) = 1") % (aoi, aoi)
    cols, rows = _sda(q)
    if not rows:
        raise IOError("SDA returned no SSURGO polygons")
    mukeys = sorted({r[0] for r in rows})
    q2 = ("SELECT mukey, hydgrp, comppct_r FROM component WHERE mukey IN (%s) AND hydgrp IS NOT NULL"
          % ",".join("'%s'" % m for m in mukeys))
    _, crow = _sda(q2)
    dom = {}
    for mk, hg, pct in crow:
        pct = float(pct or 0)
        if mk not in dom or pct > dom[mk][1]:
            dom[mk] = (hg, pct)
    dual = ctx["cfg"]["_cn"]["dual_hsg_use"]
    code = {"A": 1, "B": 2, "C": 3, "D": 4}

    def hcode(h):
        h = (h or "").strip().upper()
        if "/" in h:
            h = h.split("/")[1] if dual == "undrained" else h.split("/")[0]
        return code.get(h[:1], 0)

    tr2 = transformer("EPSG:4326", grid.crs)
    shapes = []
    for mk, w in rows:
        hc = hcode(dom.get(mk, ("", 0))[0])
        if hc and w:
            try:
                g = stransform(lambda x, y, z=None: tr2.transform(x, y), swkt.loads(w))
                parts = [gg for gg in getattr(g, "geoms", [g]) if gg.geom_type in ("Polygon", "MultiPolygon")]
                shapes += [(gg, hc) for gg in parts]
            except Exception:
                pass
    hsg = rasterize(shapes, out_shape=(grid.ny, grid.nx), transform=grid.transform, fill=0).astype(np.int8)
    np.save(cache, hsg)
    return hsg, "SSURGO via Soil Data Access"


def amc_convert(cn2, amc, bounds=(30, 99)):
    if amc == "I":
        cn = 4.2 * cn2 / (10 - 0.058 * cn2)
    elif amc == "III":
        cn = 23 * cn2 / (10 + 0.13 * cn2)
    else:
        cn = cn2
    return np.clip(cn, *bounds)


def build_cn(cls, hsg, imp, slope_frac, cnt, amc):
    """Composite CN (AMC II), optional slope adjustment of pervious part, AMC conversion."""
    table = cnt["nlcd_cn"]
    cnp = np.full(cls.shape, np.nan)
    h = np.clip(hsg, 1, 4) - 1
    for k, v in table.items():
        m = cls == int(k)
        if m.any():
            cnp[m] = np.asarray(v["cn"], float)[h[m]]
    dflt = cnt["unknown_class_cn"]
    cnp = np.where(np.isfinite(cnp), cnp, np.asarray(dflt, float)[h])
    sa = cnt.get("slope_adjustment", {})
    cnp_flat = cnp.copy()
    if sa.get("enabled", False):
        cn3 = 23 * cnp / (10 + 0.13 * cnp)
        s = np.clip(slope_frac, 0, sa.get("max_slope", 1.0))
        adj = (cn3 - cnp) / 3.0 * (1 - 2 * np.exp(-13.86 * s)) + cnp
        cnp = np.maximum(adj, cnp) if sa.get("increase_only", True) else adj
    cn2 = imp * cnt["impervious_cn"] + (1 - imp) * cnp
    cn2_flat = imp * cnt["impervious_cn"] + (1 - imp) * cnp_flat
    if amc == "I":
        cn = 4.2 * cn2 / (10 - 0.058 * cn2)
    elif amc == "III":
        cn = 23 * cn2 / (10 + 0.13 * cn2)
    else:
        cn = cn2
    lo, hi = cnt.get("cn_bounds", [30, 99])
    return np.clip(cn, lo, hi), np.clip(cn2, lo, hi), np.clip(cn2_flat, lo, hi)


def slope_grid(z, res):
    gy, gx = np.gradient(z, res)
    return np.hypot(gx, gy)


# =============================================================================
# 3. RADAR (MRMS)
# =============================================================================
MRMS_LAT1, MRMS_LON1, MRMS_D = 54.995, -129.995, 0.01
MRMS_AWS = "https://noaa-mrms-pds.s3.amazonaws.com/CONUS/{p}/{d}/MRMS_{p}_{d}-{hms}.grib2.gz"
MRMS_IEM = "https://mtarchive.geol.iastate.edu/{Y}/{m}/{dd}/mrms/ncep/{pn}/{p}_{d}-{hms}.grib2.gz"


class Window:
    """Sub-window of the MRMS CONUS 0.01-deg grid."""

    def __init__(self, lat_s, lat_n, lon_w, lon_e):
        self.i0 = int(math.floor((MRMS_LAT1 - lat_n) / MRMS_D))
        self.i1 = int(math.ceil((MRMS_LAT1 - lat_s) / MRMS_D)) + 1
        self.j0 = int(math.floor((lon_w - MRMS_LON1) / MRMS_D))
        self.j1 = int(math.ceil((lon_e - MRMS_LON1) / MRMS_D)) + 1
        self.ny, self.nx = self.i1 - self.i0, self.j1 - self.j0

    @property
    def key(self):
        return f"{self.i0}_{self.i1}_{self.j0}_{self.j1}"

    def lat(self, i):
        return MRMS_LAT1 - (self.i0 + np.asarray(i)) * MRMS_D

    def lon(self, j):
        return MRMS_LON1 + (self.j0 + np.asarray(j)) * MRMS_D

    def ij(self, lat, lon):
        i = np.rint((MRMS_LAT1 - np.asarray(lat)) / MRMS_D).astype(int) - self.i0
        j = np.rint((np.asarray(lon) - MRMS_LON1) / MRMS_D).astype(int) - self.j0
        return i, j

    @property
    def bounds_ll(self):  # (south, west, north, east) of pixel edges
        return (float(self.lat(self.ny - 1)) - MRMS_D / 2, float(self.lon(0)) - MRMS_D / 2,
                float(self.lat(0)) + MRMS_D / 2, float(self.lon(self.nx - 1)) + MRMS_D / 2)


def _mrms_urls(product, t):
    d = t.strftime("%Y%m%d")
    hms = t.strftime("%H%M%S")
    pn = product.replace("_00.00", "")
    return [MRMS_AWS.format(p=product, d=d, hms=hms),
            MRMS_IEM.format(Y=t.strftime("%Y"), m=t.strftime("%m"), dd=t.strftime("%d"), pn=pn, p=product, d=d, hms=hms)]


def decode_grib_window(raw, win):
    """Decode one MRMS GRIB2 (gz) message and cut the window. Values in native units."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    vals = meta = None
    try:
        import pygrib
        g = pygrib.fromstring(raw)
        vals = np.asarray(np.ma.filled(g.values.astype(float), np.nan))
        meta = (g["latitudeOfFirstGridPointInDegrees"], g["longitudeOfFirstGridPointInDegrees"],
                g["jDirectionIncrementInDegrees"], g["iDirectionIncrementInDegrees"], g["jScansPositively"])
    except ImportError:
        import xarray as xr
        with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
            f.write(raw)
            fn = f.name
        ds = xr.open_dataset(fn, engine="cfgrib", backend_kwargs={"indexpath": ""})
        v = ds[list(ds.data_vars)[0]]
        vals = v.values.astype(float)
        la, lo = ds.latitude.values, ds.longitude.values
        meta = (float(la[0]), float(lo[0]), abs(float(la[1] - la[0])), float(lo[1] - lo[0]), int(la[1] > la[0]))
        ds.close()
        os.remove(fn)
    lat1, lon1, dj, di, jpos = meta
    if lon1 > 180:
        lon1 -= 360
    if jpos:
        vals = vals[::-1]
        lat1 = lat1 + (vals.shape[0] - 1) * dj
    i_off = int(round((MRMS_LAT1 - lat1) / MRMS_D))
    j_off = int(round((MRMS_LON1 - lon1) / MRMS_D))
    out = np.full((win.ny, win.nx), np.nan)
    a0 = win.i0 - i_off
    b0 = win.j0 + j_off
    sub = vals[max(a0, 0):a0 + win.ny, max(b0, 0):b0 + win.nx]
    out[max(0, -a0):max(0, -a0) + sub.shape[0], max(0, -b0):max(0, -b0) + sub.shape[1]] = sub
    return out


def get_mrms(product, times_utc, win, ctx, scale=1 / 25.4, negative_is_missing=True):
    """Window time series [nt, ny, nx] (inches by default). Cached per file+window.
    Missing files -> NaN slices (reported)."""
    cdir = os.path.join(ctx["cache"], "mrms", product)
    os.makedirs(cdir, exist_ok=True)
    out = np.full((len(times_utc), win.ny, win.nx), np.nan)
    todo = []
    for k, t in enumerate(times_utc):
        fn = os.path.join(cdir, f"{t.strftime('%Y%m%d-%H%M%S')}_{win.key}.npy")
        if os.path.exists(fn):
            out[k] = np.load(fn)
        else:
            todo.append((k, t, fn))
    if todo:
        log(f"  MRMS {product}: downloading {len(todo)} of {len(times_utc)} files")

        def work(item):
            k, t, fn = item
            for u in _mrms_urls(product, t):
                try:
                    raw = http_get(u, timeout=90, tries=2)
                    a = decode_grib_window(raw, win)
                    np.save(fn, a)
                    return k, a, None
                except Exception as e:
                    err = str(e)
            return k, None, err

        with ThreadPoolExecutor(max_workers=ctx["cfg"]["common"]["mrms"].get("threads", 8)) as ex:
            for k, a, err in ex.map(work, todo):
                if a is not None:
                    out[k] = a
    raw_missing = np.all(np.isnan(out), axis=(1, 2))
    if negative_is_missing:
        out[out < 0] = np.nan
    out = out * scale
    if raw_missing.any():
        log(f"  MRMS {product}: {int(raw_missing.sum())} time steps missing")
    return out, raw_missing


def hour_range(t0, t1, step="1h"):
    """End-of-interval times strictly after t0 up to t1 (UTC)."""
    return pd.date_range(t0 + pd.Timedelta(step), t1, freq=step)


# =============================================================================
# 4. RUNOFF + ROUTING (ModClark)
# =============================================================================
def scs_excess_increments(rain_steps, cn, ia_ratio=0.2):
    """rain_steps [nt, k] (in per step), cn [k] -> incremental excess [nt, k]."""
    P = np.cumsum(rain_steps, axis=0)
    S = 1000.0 / cn - 10.0
    Ia = ia_ratio * S
    Pe = np.maximum(P - Ia, 0)
    Q = Pe ** 2 / (Pe + S)
    return np.diff(np.vstack([np.zeros((1, Q.shape[1])), Q]), axis=0)


def build_combos(pix, cn, tbin, area_ac):
    """Group cells by (rain pixel, integer CN) -> W[combo, lag-bin] area (ac)."""
    cni = np.rint(cn).astype(int)
    key = pix.astype(np.int64) * 1000 + cni
    uk, inv = np.unique(key, return_inverse=True)
    nb = int(tbin.max()) + 1
    W = np.zeros((len(uk), nb))
    np.add.at(W, (inv, tbin), area_ac)
    return (uk // 1000).astype(int), (uk % 1000).astype(float), W


def modclark(rain_px, combo_pix, combo_cn, W, dt_hr, R_hr, ia_ratio, attenuate=True):
    """rain_px [nt, npix] in/step. Returns translated inflow I and outflow O (cfs),
    plus basin excess volume (ac-in)."""
    E = scs_excess_increments(rain_px[:, combo_pix], combo_cn, ia_ratio)  # [nt, ncombo]
    nt = E.shape[0]
    X = E @ W  # [nt, nbins] ac-in per step at lag b
    I = np.zeros(nt + W.shape[1])
    for b in range(W.shape[1]):
        I[b:b + nt] += X[:, b]
    I = I * 1.008 / dt_hr  # ac-in per step -> cfs
    I = I[:nt]
    O = I.copy()
    if attenuate and R_hr > 0:
        cA = dt_hr / (R_hr + 0.5 * dt_hr)
        O = np.zeros(nt)
        for t in range(1, nt):
            O[t] = O[t - 1] + cA * (0.5 * (I[t] + I[t - 1]) - O[t - 1])
    vol_acin = float(X.sum())
    return I, O, vol_acin, E


SCS_DUH = np.array([[0, 0], [.1, .03], [.2, .1], [.3, .19], [.4, .31], [.5, .47], [.6, .66], [.7, .82], [.8, .93],
                    [.9, .99], [1, 1], [1.1, .99], [1.2, .93], [1.3, .86], [1.4, .78], [1.5, .68], [1.6, .56],
                    [1.7, .46], [1.8, .39], [1.9, .33], [2, .28], [2.2, .207], [2.4, .147], [2.6, .107],
                    [2.8, .077], [3, .055], [3.2, .04], [3.4, .029], [3.6, .021], [3.8, .015], [4, .011],
                    [4.5, .005], [5, 0]])


def scs_uh_peak(basin_rain, cn_comp, A_mi, Tc_hr, dt_hr, ia_ratio):
    """Lumped cross-check: basin-average rain, composite CN, SCS dimensionless UH."""
    ex = scs_excess_increments(basin_rain[:, None], np.array([cn_comp]), ia_ratio)[:, 0]
    Tp = dt_hr / 2 + 0.6 * Tc_hr
    qp = 484.0 * A_mi / Tp
    tu = np.arange(0, 5 * Tp + dt_hr, dt_hr)
    uh = qp * np.interp(tu / Tp, SCS_DUH[:, 0], SCS_DUH[:, 1])
    uh *= 1.0 / (uh.sum() * dt_hr * 3600 / (A_mi * 640 * 43560 / 12)) if uh.sum() > 0 else 1
    q = np.convolve(ex, uh)[:len(ex)]
    return q, Tp


def r_coefficient(Tc, A_mi, L_mi, rcfg):
    if rcfg["R_equation"] == "fcdmc":
        c = rcfg["fcdmc"]
        return c["a"] * Tc ** c["b_tc"] * A_mi ** c["c_area"] * L_mi ** c["d_length"]
    return rcfg["R_k"] * Tc


def alternating_block(depth_fn, D_hr, dt_hr):
    n = int(round(D_hr / dt_hr))
    cum = np.array([depth_fn(dt_hr * (k + 1)) for k in range(n)])
    inc = np.diff(np.concatenate([[0], cum]))
    inc = np.sort(inc)[::-1]
    out = np.zeros(n)
    mid = (n - 1) // 2
    pos = [mid]
    for k in range(1, n):
        pos.append(mid + (k + 1) // 2 if k % 2 else mid - k // 2)
    for v, p in zip(inc, pos):
        out[min(max(p, 0), n - 1)] += v
    return out


# =============================================================================
# 5. FREQUENCY
# =============================================================================
PFDS_DUR_HR = np.array([5 / 60, 10 / 60, 15 / 60, 30 / 60, 1, 2, 3, 6, 12, 24, 48, 72, 96, 168, 240, 480, 720, 1080, 1440])
PFDS_T = np.array([1, 2, 5, 10, 25, 50, 100, 200, 500, 1000])


def get_atlas14(lat, lon, ctx):
    """NOAA Atlas 14 PDS depths (in), 19 durations x 10 ARIs."""
    fn = os.path.join(ctx["cache"], f"atlas14_{lat:.4f}_{lon:.4f}.json")
    if os.path.exists(fn):
        return np.array(json.load(open(fn)))
    txt = http_get("https://hdsc.nws.noaa.gov/cgi-bin/hdsc/new/cgi_readH5.py",
                   dict(lat=f"{lat:.4f}", lon=f"{lon:.4f}", type="pf", data="depth", units="english", series="pds"),
                   binary=False)
    m = re.search(r"quantiles\s*=\s*(\[\[.*?\]\])", txt, re.S)
    if not m:
        raise IOError("PFDS response had no quantiles")
    q = np.array(ast.literal_eval(m.group(1)), float)
    json.dump(q.tolist(), open(fn, "w"))
    return q


def arf_tp29(D_hr, A_mi):
    """TP-29 areal reduction factor (Leclerc-Schaake fit)."""
    a = 1.1 * D_hr ** 0.25
    return float(min(1.0, 1 - math.exp(-a) + math.exp(-a - 0.01 * A_mi)))


def atlas_depth(q, D_hr, T=None):
    """Depth at any duration (log-log interpolation), for all T or one T."""
    ld = np.log(PFDS_DUR_HR)
    out = np.array([math.exp(np.interp(math.log(D_hr), ld, np.log(q[:, j]))) for j in range(q.shape[1])])
    if T is None:
        return out
    z = _z_of_T(np.maximum(PFDS_T, 1.0001))
    return float(math.exp(np.interp(_z_of_T(max(T, 1.0001)), z, np.log(out))))


def max_running(series, n):
    if n < 1 or n > len(series):
        return float("nan"), -1
    cs = np.concatenate([[0], np.cumsum(np.nan_to_num(series))])
    s = cs[n:] - cs[:-n]
    k = int(np.argmax(s))
    return float(s[k]), k + n - 1


def parse_streamstats_csv(path):
    """Flexible parser for a StreamStats report CSV -> (table, area_sqmi).
    Handles the current "N-percent AEP flood" report and the older "N-year peak" style."""
    rows = list(csv.reader(open(path, encoding="utf-8-sig", errors="ignore")))
    aep_rows, area = [], None
    for r in rows:
        if not r:
            continue
        if area is None and r[0].strip().upper() in ("DRNAREA", "CONTDA") and len(r) > 2:
            try:
                area = float(r[2])
            except ValueError:
                pass
        m = re.match(r"\s*([\d.]+)-percent AEP flood", r[0])
        if m and len(r) >= 5:
            aep = float(m.group(1))
            f = lambda x: float(x) if x not in ("", None) else None
            aep_rows.append(dict(T=100.0 / aep, Q=f(r[1]), lo=f(r[3]), hi=f(r[4])))
    if len(aep_rows) >= 3:
        return sorted(aep_rows, key=lambda d: d["T"]), area
    area = None
    hdr = None
    table = []
    for r in rows:
        line = ",".join(r)
        low = line.lower()
        if area is None and ("drainage area" in low or "drnarea" in low):
            nums = [float(x) for x in re.findall(r"(?<![A-Za-z])(\d+\.?\d*)", ",".join(r[1:]))]
            if nums:
                area = nums[0]
        if "value" in low and ("statistic" in low or "name" in low):
            hdr = [c.strip().lower() for c in r]
            continue
        m = re.search(r"(?:^|[^\d])(\d+(?:\.\d+)?)[\s-]*(?:year|yr)[^,]*(?:peak|flood)|pk(\d+(?:_\d+)?)", low)
        if m:
            T = float((m.group(1) or m.group(2)).replace("_", "."))
            nums = []
            for c in r[1:]:
                try:
                    nums.append(float(c.replace(",", "")))
                except ValueError:
                    nums.append(None)
            if hdr:
                def col(*names):
                    for i, h in enumerate(hdr):
                        if any(n in h for n in names) and 0 < i <= len(nums):
                            return nums[i - 1]
                    return None
                q, lo, hi = col("value"), col("pil", "lower"), col("piu", "upper")
            else:
                vals = [x for x in nums if x is not None]
                q = vals[0] if vals else None
                lo = hi = None
            if q:
                table.append(dict(T=T, Q=q, lo=lo, hi=hi))
    table.sort(key=lambda d: d["T"])
    return table, area


def peak_stats(bcfg, ctx, A_basin):
    """Peak-flow frequency table at the basin area (area-transfer if needed)."""
    fs = bcfg.get("peak_flow_stats", {})
    table, area, src = None, None, ""
    p = find_file(ctx["base"], fs.get("csv"))
    if p:
        try:
            table, area = parse_streamstats_csv(p)
            src = os.path.basename(p)
        except Exception as e:
            log(f"  StreamStats CSV parse failed: {e}")
        if table is not None and len(table) < 3:
            table = None
    if not table and fs.get("table"):
        table = [dict(T=float(k), Q=v[0], lo=(v[1] if len(v) > 1 else None), hi=(v[2] if len(v) > 2 else None))
                 for k, v in fs["table"].items()]
        table.sort(key=lambda d: d["T"])
        src = fs.get("source", "basins.json")
    if not table:
        return None
    area = fs.get("area_sqmi") or area or fs.get("area_sqmi_if_missing")
    note = ""
    ratio = 1.0
    if area and abs(A_basin / area - 1) > 0.05:
        ratio = (A_basin / area) ** fs.get("transfer_exponent", 0.6)
        note = (f"Published statistics are for {area:.2f} sq mi; transferred to the delineated {A_basin:.2f} sq mi "
                f"by Q*(At/As)^{fs.get('transfer_exponent', 0.6)} (x{ratio:.2f}).")
    for d in table:
        for k in ("Q", "lo", "hi"):
            if d.get(k):
                d[k] = d[k] * ratio
    return dict(table=table, source=src, area_sqmi=area, transfer_note=note, region=fs.get("region", ""))


def flood_T(Q, stats):
    if not stats:
        return dict(T=float("nan"), q="", T_lo=None, T_hi=None)
    t = stats["table"]
    T, q = return_period(Q, [d["T"] for d in t], [d["Q"] for d in t])
    out = dict(T=T, q=q, T_lo=None, T_hi=None)
    if all(d.get("hi") for d in t) and all(d.get("lo") for d in t):
        out["T_lo"] = return_period(Q, [d["T"] for d in t], [d["hi"] for d in t])
        out["T_hi"] = return_period(Q, [d["T"] for d in t], [d["lo"] for d in t])
    return out


# =============================================================================
# 6. GAUGE CHECKS
# =============================================================================
def read_alert(path, tz="UTC"):
    """Auto-detecting ALERT export reader -> pandas Series (UTC index, float values).
    Handles header junk, separate date/time columns, any delimiter."""
    lines = open(path, encoding="utf-8-sig", errors="ignore").read().splitlines()
    datepat = re.compile(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}")
    start = next((i for i, l in enumerate(lines) if datepat.search(l)), 0)
    hdr_line = lines[start - 1] if start > 0 else None
    sep = max([",", "\t", ";", "|"], key=lambda s: lines[start].count(s))
    if lines[start].count(sep) == 0:
        sep = r"\s+"
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), sep=sep, header=None, engine="python")
    names = None
    if hdr_line and (hdr_line.count(sep) == df.shape[1] - 1 if sep != r"\s+" else True):
        names = [c.strip().strip('"').lower() for c in re.split(sep if sep != r"\s+" else r"\s+", hdr_line)]
    dtcol = None
    for c in range(df.shape[1]):
        s = df[c].astype(str)
        if s.str.contains(datepat).mean() > 0.9:
            dtcol = c
            break
    if dtcol is None:
        raise ValueError(f"no date column in {path}")
    stamp = df[dtcol].astype(str)
    nxt = dtcol + 1
    if nxt < df.shape[1] and df[nxt].astype(str).str.match(r"^\d{1,2}:\d{2}").mean() > 0.9:
        stamp = stamp + " " + df[nxt].astype(str)
        nxt += 1
    t = pd.to_datetime(stamp, errors="coerce", format="mixed")
    numcols = []
    for c in range(nxt, df.shape[1]):
        v = pd.to_numeric(df[c], errors="coerce")
        if v.notna().mean() > 0.8:
            numcols.append(c)
    if not numcols:
        raise ValueError(f"no numeric column in {path}")
    vc = numcols[0]
    if names and len(names) == df.shape[1]:
        pref = [c for c in numcols if any(k in names[c] for k in ("value", "rain", "precip", "stage", "level", "reading", "accum"))]
        if pref:
            vc = pref[0]
    v = pd.to_numeric(df[vc], errors="coerce")
    s = pd.Series(v.values, index=t).dropna()
    s = s[s.index.notna()].sort_index()
    if s.index.tz is None:
        s.index = s.index.tz_localize(tz if tz not in ("MST", "local") else "America/Phoenix")
    s.index = s.index.tz_convert("UTC")
    hdr_text = (hdr_line or "") + " " + (names[vc] if names and len(names) > vc else "")
    s.attrs["header"] = hdr_text
    return s


def rain_increments(s, units="auto"):
    """Raw rain series -> incremental depth events (in). Detects cumulative vs
    incremental and tip-by-tip vs interval totals."""
    d = s.diff()
    frac_nondec = float((d.dropna() >= -1e-9).mean()) if len(d) > 2 else 0
    cumulative = frac_nondec > 0.97 and (s.iloc[-1] - s.iloc[0]) > 2 * max(float(d.max()), 1e-9)
    if cumulative:
        inc = d.fillna(0).clip(lower=0)
        inc[d < -1e-9] = 0  # counter reset
        kind = "cumulative"
    else:
        inc = s.clip(lower=0)
        nz = inc[inc > 0]
        kind = "tip-by-tip" if len(nz) > 5 and nz.round(4).nunique() <= 2 else "incremental"
    h = (s.attrs.get("header", "") or "").lower()
    u = units
    if units == "auto":
        u = "mm" if "mm" in h else ("in" if re.search(r"\bin\b|inch", h) else ("mm" if inc.sum() > 15 else "in"))
    if u == "mm":
        inc = inc / 25.4
    inc.attrs.update(kind=kind, units=u)
    return inc


def stage_series(s, status_hr=12.0, gap_factor=1.5):
    """Report-by-exception stage -> 15-min held series with outage mask."""
    s = s[~s.index.duplicated(keep="last")]
    idx = pd.date_range(s.index[0].floor("15min"), s.index[-1].ceil("15min"), freq="15min")
    held = s.reindex(s.index.union(idx)).ffill().reindex(idx)
    gaps = s.index.to_series().diff().dt.total_seconds() / 3600.0
    out = pd.Series(False, index=idx)
    outages = []
    for t_end, g in gaps.items():
        if g and g > gap_factor * status_hr:
            t0 = t_end - pd.Timedelta(hours=g)
            out[(idx > t0) & (idx < t_end)] = True
            outages.append((t0, t_end, float(g), float(s[:t0].iloc[-1]) if len(s[:t0]) else float("nan")))
    return held, out, outages


def rain_gauge_check(g, inc, ev, win, Pw, times_h, basin_pix_totals, chk):
    """Compare a rain gauge to the MRMS Pass 2 window (hourly, in)."""
    gi, gj = win.ij(g["lat"], g["lon"])
    gi, gj = int(gi), int(gj)
    gh = inc[(inc.index > ev["t0"]) & (inc.index <= ev["t1"])]
    gh = gh.resample("1h", label="right", closed="right").sum().reindex(times_h, fill_value=0.0)
    G = float(gh.sum())
    cell = np.nan_to_num(Pw[:, gi, gj])
    nb = np.nan_to_num(Pw[:, max(gi - 1, 0):gi + 2, max(gj - 1, 0):gj + 2]).reshape(len(times_h), -1)
    R = float(cell.sum())
    Rn = nb.sum(0)
    band = max(chk["rain_total_pct"] / 100 * R, chk["rain_total_abs_in"])
    tot_pass_cell = abs(G - R) <= band
    tot_pass_nb = any(abs(G - r) <= max(chk["rain_total_pct"] / 100 * r, chk["rain_total_abs_in"]) for r in Rn)
    # hourly peak within 3x3 and +/- time tolerance
    kpk = int(np.argmax(gh.values))
    tol = int(chk["time_tol_hr"])
    Rpk = float(nb[max(0, kpk - tol):kpk + tol + 1].max()) if nb.size else 0
    Gpk = float(gh.values[kpk])
    pk_ratio = Gpk / Rpk if Rpk > 0 else float("nan")
    pk_pass = (1 - chk["rain_hourly_pct"] / 100) <= pk_ratio <= (1 + chk["rain_hourly_pct"] / 100) if np.isfinite(pk_ratio) else False
    # correlation + timing offset (+/- 12 h scan for clock/time-zone errors)
    best = (0, -2.0)
    for lag in range(-12, 13):
        a = gh.values
        b = np.roll(cell, lag)
        if lag > 0:
            a, b = a[lag:], b[lag:]
        elif lag < 0:
            a, b = a[:lag], b[:lag]
        if a.std() > 0 and b.std() > 0:
            r = float(np.corrcoef(a, b)[0, 1])
            if r > best[1]:
                best = (lag, r)
    r0 = float(np.corrcoef(gh.values, cell)[0, 1]) if gh.values.std() > 0 and cell.std() > 0 else float("nan")
    core = bool(R >= np.nanpercentile(basin_pix_totals, 75)) if len(basin_pix_totals) else False
    flags = []
    if not tot_pass_cell:
        flags.append("event total outside band at radar cell" + (" (inside band for a 3x3 neighbour)" if tot_pass_nb else ""))
    if not pk_pass:
        flags.append(f"hourly peak ratio {pk_ratio:.2f} outside +/-{chk['rain_hourly_pct']}%")
    if abs(best[0]) > chk["time_tol_hr"]:
        flags.append(f"best timing offset {best[0]:+d} h (check gauge clock / time zone)")
    status = "pass" if tot_pass_cell and pk_pass and not flags else ("pass (3x3)" if tot_pass_nb and pk_pass and abs(best[0]) <= chk["time_tol_hr"] else "flag")
    return dict(id=g["id"], type="rain", lat=g["lat"], lon=g["lon"], gauge_total_in=G, radar_cell_in=R,
                radar_3x3_min_in=float(Rn.min()), radar_3x3_max_in=float(Rn.max()),
                G_over_R=G / R if R > 0 else float("nan"), band_in=band, gauge_peak_hr_in=Gpk,
                radar_peak_hr_3x3_in=Rpk, peak_ratio=pk_ratio, r=r0, best_lag_hr=best[0], best_lag_r=best[1],
                in_storm_core=core, status=status, flags=flags,
                series=dict(t=[str(t) for t in times_h], gauge=gh.values.tolist(), radar=cell.tolist()),
                note="Check only - radar is not adjusted.", file_kind=inc.attrs.get("kind"), units=inc.attrs.get("units"))


def stage_gauge_check(g, s, ev, model, chk):
    held, outage, outages = stage_series(s, chk["status_interval_hr"])
    w = held[(held.index >= ev["t0"]) & (held.index <= ev["t1"] + pd.Timedelta(hours=48))]
    ow = outage.reindex(w.index, fill_value=False)
    tm, Om = model["t_model"], model["O"]
    kp = int(np.argmax(Om))
    aft = np.flatnonzero(Om[kp:] < 0.25 * Om[kp])
    t_rec = tm[kp + aft[0]] if len(aft) else tm[-1]
    win_end = max(t_rec, model["rain_end_utc"] + pd.Timedelta(hours=1))
    rd = w[(w.index <= win_end) & ~ow]
    flags, res = [], dict(id=g["id"], type=g["type"], lat=g["lat"], lon=g["lon"], regulated=g.get("regulated", False))
    if rd.empty:
        res.update(status="no data", flags=["no stage reports in event window"])
        return res
    base = float(w.iloc[:4].min())
    t_obs = rd.idxmax()
    pk = float(rd.max())
    dt_h = (t_obs - model["peak_time_utc"]).total_seconds() / 3600.0
    timing_pass = abs(dt_h) <= max(model["Tc_hr"], 1.0)
    later = w[(w.index > win_end) & ~ow]
    if len(later) and later.max() > pk + 0.05:
        flags.append(f"later rise to {later.max():.2f} ft at {later.idxmax().tz_convert(ev['tz']):%b %d %H:%M} "
                     f"not driven by rain (release, debris or sensor shift?)")
    for t0, t1, gh_, v in outages:
        if t1 >= ev["t0"] and t0 <= ev["t1"] + pd.Timedelta(hours=48):
            flags.append(f"report gap {gh_:.0f} h from {t0.tz_convert(ev['tz']):%b %d %H:%M} at {v:.2f} ft"
                         + (" (possible sensor ceiling)" if v >= 0.97 * float(s.max()) else "") +
                         " - outage, not a delayed rise")
    mag = None
    # onset check: basin rain before the observed rise vs before the modelled rise (a CN / Ia check)
    onset = None
    pre = w[(w.index >= ev["t0"]) & ~ow]
    thr = base + max(0.05, 0.05 * (pk - base))
    up = pre[pre > thr]
    k_m = np.flatnonzero(Om > 0.02 * Om[kp])
    if len(up) and len(k_m):
        t_on = up.index[0]
        cum = model["cum_rain"]
        r_obs = float(np.interp((t_on - tm[0]).total_seconds(), [(x - tm[0]).total_seconds() for x in tm], cum))
        r_mod = float(cum[k_m[0]])
        onset = dict(obs_time_utc=str(t_on), rain_before_obs_in=r_obs, model_time_utc=str(tm[k_m[0]]), rain_before_model_in=r_mod)
        if abs(r_obs - r_mod) > max(0.3, 0.3 * r_obs):
            flags.append(f"runoff onset: observed rise after {r_obs:.2f} in of basin rain, model after {r_mod:.2f} in "
                         f"-> model losses look {'too small (CN high)' if r_mod < r_obs else 'too large (CN low)'}"
                         + (" [regulated gauge: indicative only]" if g.get("regulated") else ""))
    if g.get("rating"):
        rt = g["rating"]
        qobs = float(np.interp(pk, rt["stage_ft"], rt["q_cfs"]))
        fac = max(qobs, 1) / max(model["peak_cfs"], 1)
        mag = dict(q_obs_cfs=qobs, factor=fac, pass_=1 / chk["flow_factor"] <= fac <= chk["flow_factor"])
    if g.get("regulated"):
        status = "regulated"
        flags.insert(0, "regulated - check not valid for natural flow (timing only)")
    else:
        status = "pass" if timing_pass and (mag is None or mag["pass_"]) else "flag"
        if not timing_pass:
            flags.append(f"observed peak {dt_h:+.1f} h from modelled peak (tolerance +/-Tc)")
        if mag is None:
            flags.append("no rating - timing and shape only")
    res.update(status=status, flags=flags, base_ft=base, peak_ft=pk, peak_time_utc=str(t_obs), onset=onset,
               offset_hr=dt_h, timing_pass=timing_pass, magnitude=mag,
               series=dict(t=[str(t) for t in w.index], stage=w.values.tolist(), outage=ow.values.tolist()))
    return res


# =============================================================================
# 7. OUTPUTS
# =============================================================================
def _png_data_url(rgba):
    from PIL import Image
    b = io.BytesIO()
    Image.fromarray(rgba).save(b, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


def _colorize(a, cmap, vmin, vmax, alpha=0.75):
    import matplotlib
    cm = matplotlib.colormaps[cmap] if isinstance(cmap, str) else cmap
    x = (a - vmin) / max(vmax - vmin, 1e-12)
    rgba = (cm(np.clip(np.nan_to_num(x), 0, 1)) * 255).astype(np.uint8)
    rgba[..., 3] = np.where(np.isfinite(a), int(alpha * 255), 0)
    return rgba


def overlay_wgs84(arr, grid, cmap, vmin, vmax, alpha=0.75, max_px=1400):
    """Reproject a grid array to WGS84 and return (data_url, [[s,w],[n,e]])."""
    from rasterio.warp import reproject, Resampling, calculate_default_transform
    f = max(1, int(math.ceil(max(grid.nx, grid.ny) / max_px)))
    a = arr.astype(float)
    if f > 1:
        ny, nx = (grid.ny // f) * f, (grid.nx // f) * f
        a = np.nanmean(a[:ny, :nx].reshape(ny // f, f, nx // f, f), axis=(1, 3))
        from rasterio.transform import Affine
        tr = Affine(grid.res * f, 0, grid.x0, 0, -grid.res * f, grid.y0)
    else:
        tr = grid.transform
        ny, nx = grid.ny, grid.nx
    h, w = a.shape
    b = (tr.c, tr.f + tr.e * h, tr.c + tr.a * w, tr.f)
    dtr, dw, dh = calculate_default_transform(grid.crs, "EPSG:4326", w, h, *b)
    dst = np.full((dh, dw), np.nan)
    reproject(a, dst, src_transform=tr, src_crs=grid.crs, dst_transform=dtr, dst_crs="EPSG:4326",
              resampling=Resampling.nearest, src_nodata=np.nan, dst_nodata=np.nan)
    bounds = [[dtr.f + dtr.e * dh, dtr.c], [dtr.f, dtr.c + dtr.a * dw]]
    return _png_data_url(_colorize(dst, cmap, vmin, vmax, alpha)), bounds


def basin_outline_ll(mask2d, grid, simplify_m=None):
    from rasterio.features import shapes
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union, transform as stransform
    polys = [shape(g) for g, v in shapes(mask2d.astype(np.uint8), mask=mask2d, transform=grid.transform) if v == 1]
    u = unary_union(polys).simplify(simplify_m or grid.res, preserve_topology=True)
    tr = transformer(grid.crs, "EPSG:4326")
    return stransform(lambda x, y, z=None: tr.transform(x, y), u), u


def write_figures(R, outdir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figs = {}
    tz = R["event"]["tz"]
    # --- hydrograph
    h = R["_hydro"]
    t = pd.to_datetime(h["t_utc"]).tz_convert(tz)
    fig, ax = plt.subplots(figsize=(11, 5.2))
    ax.plot(t, h["Q_cfs"], color="#1f4e79", lw=2.2, label="ModClark outflow (natural, unregulated)")
    ax.plot(t, h["I_cfs"], color="#7fa7cc", lw=1, ls="--", label="Translation only (no attenuation)")
    if R["channel"]:
        ax.axhline(R["channel"]["benchmark_cfs"], color="#b03a2e", ls=":", lw=1.2,
                   label=f"Channel design flow {R['channel']['benchmark_cfs']:,.0f} cfs ({R['channel'].get('design_storm', '')})")
    k = int(np.argmax(h["Q_cfs"]))
    ax.annotate(f"{h['Q_cfs'][k]:,.0f} cfs", (t[k], h["Q_cfs"][k]), textcoords="offset points", xytext=(8, 6), fontsize=10,
                color="#1f4e79", weight="bold")
    ax.set_ylabel("Discharge (cfs)")
    ax.set_ylim(0, max(max(h["Q_cfs"]), max(h["I_cfs"])) * 1.6 + 1)
    ax2 = ax.twinx()
    tr_ = pd.to_datetime(R["_rain"]["t_utc"]).tz_convert(tz)
    width = (tr_[1] - tr_[0]) if len(tr_) > 1 else pd.Timedelta("1h")
    ax2.bar(tr_ - width, R["_rain"]["basin_in"], width=width, align="edge", color="#5dade2", alpha=0.6, label="Basin rain (radar)")
    ax2.set_ylim(max(R["_rain"]["basin_in"]) * 3.2 + 1e-3, 0)
    ax2.set_ylabel(f"Basin rain per {R['_rain']['res_label']} (in)")
    for gk in R["checks"]:
        if gk.get("type") in ("stage", "flow") and "series" in gk:
            ax3 = ax.twinx()
            ax3.spines["right"].set_position(("axes", 1.09))
            ts = pd.to_datetime(gk["series"]["t"]).tz_convert(tz)
            ax3.plot(ts, gk["series"]["stage"], color="#e67e22", lw=1.1, label=f"Gauge {gk['id']} stage (check)")
            ax3.set_ylabel(f"Stage {gk['id']} (ft)", color="#e67e22")
            ax3.legend(loc="center right", fontsize=8)
            break
    ax.legend(loc="upper left", fontsize=8.5)
    ax2.legend(loc="upper right", fontsize=8.5)
    ax.set_xlim(t[0], t[-1])
    ax.set_title(f"{R['name']} - {R['event']['label']}: hydrograph at outlet (v{VERSION})")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    figs["hydrograph"] = os.path.join(outdir, "hydrograph.png")
    fig.savefig(figs["hydrograph"], dpi=130)
    plt.close(fig)

    # --- DDF
    s = R["storm"]
    q = np.array(R["_atlas14"])
    A = R["basin"]["area_sqmi"]
    durs = np.array([d["duration_hr"] for d in s["durations"]])
    fig, ax = plt.subplots(figsize=(8.5, 5.5))
    dd = np.geomspace(0.25, 72, 60)
    for j, T in enumerate(PFDS_T[:7]):
        ax.plot(dd, [atlas_depth(q, x)[j] * arf_tp29(x, A) for x in dd], lw=1, color=plt.cm.Blues(0.35 + j * 0.09),
                label=f"{T}-yr (Atlas 14 x ARF)")
    ev_d = [d["depth_in"] for d in s["durations"]]
    ax.plot(durs, ev_d, "o-", color="#c0392b", lw=2, label="This storm (basin max)")
    if s.get("tc_duration"):
        ax.plot([s["tc_duration"]["duration_hr"]], [s["tc_duration"]["depth_in"]], "D", color="k", label="At Tc")
    ax.set_xscale("log")
    ax.set_xticks([0.25, 0.5, 1, 2, 3, 6, 12, 24, 48, 72])
    ax.set_xticklabels(["15m", "30m", "1h", "2h", "3h", "6h", "12h", "24h", "48h", "72h"])
    ax.set_ylabel("Basin-average depth (in)")
    ax.set_title(f"{R['name']}: depth-duration-frequency (controlling: {s['controlling']['label']})")
    ax.grid(alpha=0.3, which="both")
    ax.legend(fontsize=8)
    fig.tight_layout()
    figs["ddf"] = os.path.join(outdir, "ddf.png")
    fig.savefig(figs["ddf"], dpi=130)
    plt.close(fig)

    # --- isochrones
    tg = R["_tgrid"]
    fig, ax = plt.subplots(figsize=(7.5, 7))
    ext = R["_grid_extent"]
    im = ax.imshow(tg, extent=ext, cmap="viridis_r")
    lv = np.arange(0, np.nanmax(tg) + 0.5, max(0.25, round(np.nanmax(tg) / 8 * 4) / 4))
    ax.contour(np.where(np.isfinite(tg), tg, np.nan), levels=lv, extent=ext, origin="upper", colors="w", linewidths=0.6)
    plt.colorbar(im, ax=ax, label="Travel time to outlet (hr)", shrink=0.8)
    ax.set_title(f"{R['name']}: travel-time isochrones (Tc = {R['timing']['Tc_grid_hr']:.2f} h)")
    ax.set_xlabel("UTM 12N easting (m)")
    ax.set_ylabel("northing (m)")
    fig.tight_layout()
    figs["isochrones"] = os.path.join(outdir, "isochrones.png")
    fig.savefig(figs["isochrones"], dpi=120)
    plt.close(fig)

    # --- storm track
    st = R["_storm_field"]
    fig, ax = plt.subplots(figsize=(8, 7))
    sb = st["bounds"]
    im = ax.imshow(st["total"], extent=[sb[1], sb[3], sb[0], sb[2]], cmap="YlGnBu", origin="upper")
    plt.colorbar(im, ax=ax, label="Storm total, MRMS Pass 2 (in)", shrink=0.8)
    bx, by = R["_outline_ll"]
    ax.plot(bx, by, color="k", lw=1.5)
    tk = st["track"]
    if tk:
        sc = ax.scatter([p[2] for p in tk], [p[1] for p in tk], c=np.arange(len(tk)), cmap="autumn", s=[10 + 120 * p[3] for p in tk],
                        edgecolor="k", lw=0.4, zorder=3)
        ax.plot([p[2] for p in tk], [p[1] for p in tk], color="0.3", lw=0.7, zorder=2)
        cb = plt.colorbar(sc, ax=ax, shrink=0.6, label="hour sequence (rain-weighted centroid)")
    for gk in R["checks"]:
        ax.plot(gk["lon"], gk["lat"], "^", color="#e67e22" if gk.get("status") != "pass" else "#27ae60", ms=8, mec="k")
    ax.set_aspect(1 / math.cos(math.radians(R["basin"]["centroid_lat"])))
    import textwrap
    ax.set_title(f"{R['name']}: storm total and hourly storm-core track\n" + "\n".join(textwrap.wrap(st['motion_note'], 70)), fontsize=9.5)
    fig.tight_layout()
    figs["storm_track"] = os.path.join(outdir, "storm_track.png")
    fig.savefig(figs["storm_track"], dpi=120)
    plt.close(fig)

    # --- Tc flowpath profile
    pr = R["_profile"]
    fig, ax = plt.subplots(figsize=(10, 4.6))
    d_ = np.array(pr["dist_mi"]); e_ = np.array(pr["elev_ft"]); rg = np.array(pr["regime"]); dp = np.array(pr["dep"], bool)
    for code, col, lab in ((1, "#f4d03f", "sheet"), (2, "#e67e22", "shallow concentrated"), (3, "#2e86c1", "channel")):
        m_ = rg == code
        ax.scatter(d_[m_], e_[m_], s=4, color=col, label=lab)
    ax.scatter(d_[dp], e_[dp], s=10, facecolor="none", edgecolor="k", lw=0.5, label="filled DEM depression")
    ax.invert_xaxis()
    ax.set_xlabel("Distance to outlet (mi)")
    ax.set_ylabel("Elevation (ft)")
    ax2 = ax.twinx()
    ax2.plot(d_, pr["t_hr"], color="0.3", lw=1.2, label="travel time to outlet")
    ax2.set_ylabel("Travel time to outlet (hr)")
    ax.legend(loc="upper right", fontsize=8, markerscale=3)
    ax.set_title(f"{R['name']}: Tc flowpath profile (Tc {R['timing']['Tc_grid_hr']:.2f} h, mean {R['timing']['tc_path']['mean_v_fps']:.1f} ft/s)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    figs["tc_profile"] = os.path.join(outdir, "tc_profile.png")
    fig.savefig(figs["tc_profile"], dpi=130)
    plt.close(fig)

    # --- rain check table
    rows = [c for c in R["checks"] if c["type"] == "rain" and "gauge_total_in" in c]
    if rows:
        df = pd.DataFrame([{"Gauge": c["id"], "Gauge (in)": round(c["gauge_total_in"], 2), "Radar cell (in)": round(c["radar_cell_in"], 2),
                            "3x3 range (in)": f"{c['radar_3x3_min_in']:.2f}-{c['radar_3x3_max_in']:.2f}",
                            "G/R": round(c["G_over_R"], 2), "Peak-hr ratio": round(c["peak_ratio"], 2),
                            "r": round(c["r"], 2), "Lag (h)": c["best_lag_hr"], "Storm core": c["in_storm_core"],
                            "Status": c["status"]} for c in rows])
        df.to_csv(os.path.join(outdir, "rain_check.csv"), index=False)
        fig, ax = plt.subplots(figsize=(11, 0.6 + 0.4 * len(df)))
        ax.axis("off")
        tb = ax.table(cellText=df.values, colLabels=df.columns, loc="center", cellLoc="center")
        tb.auto_set_font_size(False)
        tb.set_fontsize(8.5)
        tb.scale(1, 1.4)
        fig.tight_layout()
        figs["rain_check"] = os.path.join(outdir, "rain_check.png")
        fig.savefig(figs["rain_check"], dpi=130)
        plt.close(fig)
    return figs


def write_map(R, outdir, layers):
    import folium
    lat, lon = R["basin"]["outlet_lat"], R["basin"]["outlet_lon"]
    m = folium.Map(location=[R["basin"]["centroid_lat"], R["basin"]["centroid_lon"]], zoom_start=12, tiles=None)
    folium.TileLayer("OpenStreetMap", name="Streets").add_to(m)
    folium.TileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
                     attr="Esri", name="Topo").add_to(m)
    folium.TileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                     attr="Esri", name="Imagery").add_to(m)
    for name, (url, bounds, show) in layers.items():
        folium.raster_layers.ImageOverlay(url, bounds=bounds, name=name, show=show, interactive=False).add_to(m)
    folium.GeoJson(R["_outline_geojson"], name="Basin (delineated)",
                   style_function=lambda f: dict(color="#000", weight=2.5, fill=False)).add_to(m)
    if R.get("_ref_geojson"):
        folium.GeoJson(R["_ref_geojson"], name="Reference polygon",
                       style_function=lambda f: dict(color="#8e44ad", weight=1.5, dashArray="6 4", fill=False)).add_to(m)
    folium.PolyLine(R["_longest_path"], color="#c0392b", weight=2.5, tooltip="Longest flowpath",
                    name="Longest flowpath").add_to(m)
    folium.Marker([lat, lon], tooltip="Outlet", icon=folium.Icon(color="red", icon="tint")).add_to(m)
    colors = {"pass": "green", "pass (3x3)": "green", "flag": "orange", "regulated": "gray", "no data": "lightgray"}
    for c in R["checks"]:
        pop = f"<b>{c['type'].title()} gauge {c['id']}</b><br>Status: {c.get('status')}<br>"
        if c["type"] == "rain":
            pop += (f"Gauge {c['gauge_total_in']:.2f} in vs radar cell {c['radar_cell_in']:.2f} in "
                    f"(3x3 {c['radar_3x3_min_in']:.2f}-{c['radar_3x3_max_in']:.2f}); G/R {c['G_over_R']:.2f}; r {c['r']:.2f}<br>")
        elif c.get("peak_ft") is not None:
            pop += f"Peak {c['peak_ft']:.2f} ft; offset {c['offset_hr']:+.1f} h vs model<br>"
        pop += "<br>".join(c.get("flags", []))
        folium.CircleMarker([c["lat"], c["lon"]], radius=8, color="#222", weight=1, fill=True,
                            fill_color=colors.get(c.get("status"), "lightgray"), fill_opacity=0.95,
                            popup=folium.Popup(pop, max_width=360), tooltip=f"{c['id']}: {c.get('status')}").add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.get_root().html.add_child(folium.Element(summary_panel_html(R)))
    path = os.path.join(outdir, "map.html")
    m.save(path)
    return path


def summary_panel_html(R):
    f, s, b, a = R["flood"], R["storm"], R["basin"], R["antecedent"]
    rng = f.get("range_text") or ""
    flags = "".join(f"<li>{x}</li>" for x in R["flags"])
    det = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in R["details_table"])
    return f"""
<div style="position:fixed;top:10px;right:10px;z-index:9999;width:340px;max-height:92vh;overflow:auto;
 background:rgba(255,255,255,.96);border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.3);
 font:13px/1.35 -apple-system,Segoe UI,Arial;padding:12px 14px">
 <div style="font-size:15px;font-weight:700">{R['name']}</div>
 <div style="color:#666;font-size:11.5px">{R['event']['label']} &middot; coconino_storm v{VERSION} (ModClark)</div>
 <div style="margin-top:8px;padding:8px;background:#eaf2fb;border-left:4px solid #1f4e79">
  <div style="font-weight:700">Flood intensity</div>
  <div style="font-size:17px;font-weight:700;color:#1f4e79">{f['headline']}</div>
  <div style="font-size:11.5px">{rng}</div><div style="font-size:11.5px">{f['band_text']}</div></div>
 <div style="margin-top:6px;padding:8px;background:#eef8f1;border-left:4px solid #1e8449">
  <div style="font-weight:700">Storm intensity (radar basin average)</div>
  <div>{s['headline']}</div>
  <div style="font-size:11.5px">Total {s['total_in']:.2f} in &middot; wettest hour {s['max_1hr_in']:.2f} in &middot; API {a['api_in']:.2f} in ({a['class']}); headline ARC {a['amc']}</div></div>
 <div style="margin-top:6px;padding:8px;background:#f6f6f6;border-left:4px solid #555">
  <div style="font-weight:700">Basin</div>
  <div>{b['area_sqmi']:.2f} sq mi &middot; Tc {R['timing']['Tc_grid_hr']:.2f} h &middot; R {R['timing']['R_hr']:.2f} h &middot; CN {R['runoff']['cn_mean']:.0f} (AMC {a['amc']})</div></div>
 {"<div style='margin-top:6px;color:#9a5b00'><b>Flags</b><ul style=margin:2px 0 0 16px;padding:0>" + flags + "</ul></div>" if flags else ""}
 <details style="margin-top:6px"><summary style="cursor:pointer;font-weight:600">More details</summary>
 <table style="font-size:11.5px;border-collapse:collapse;width:100%">{det}</table></details>
</div>"""


def summary_text(R):
    f, s, b, a, t, rr = R["flood"], R["storm"], R["basin"], R["antecedent"], R["timing"], R["runoff"]
    L = [f"{R['name']} - {R['event']['label']}   (coconino_storm v{VERSION}, ModClark + distributed CN)",
         "=" * 78,
         f"FLOOD INTENSITY : {f['headline']}",
         f"                  {f.get('range_text', '')}",
         f"STORM INTENSITY : {s['headline']}",
         f"                  total {s['total_in']:.2f} in, wettest hour {s['max_1hr_in']:.2f} in, flood vs rain rarity {f.get('rarity_text', 'n/a')}",
         f"                  {f['band_text']}",
         f"ANTECEDENT      : ARC {a['amc']} by {a['method_text']}",
         f"                  API {a['api_in']:.2f} in ({a['class']}), prior 5/7/20/60 d {a['prior_5d_in']:.2f}/{a['prior_7d_in']:.2f}/{a['prior_20d_in']:.2f}/{a['prior_60d_in']:.2f} in; NEH-4 5-day rule -> ARC {a['neh4_5day_amc']}",
         f"BASIN           : {b['area_sqmi']:.2f} sq mi delineated ({b['source']}); {b['area_note']}",
         f"                  mean elev {b['mean_elev_ft']:,.0f} ft, mean slope {b['mean_slope_pct']:.1f}%, impervious {b['impervious_pct']:.1f}%",
         f"TIMING          : Tc grid {t['Tc_grid_hr']:.2f} h | Kirpich {t['Tc_kirpich_hr']:.2f} h | FCDMC {t['Tc_fcdmc_hr']:.2f} h ; R {t['R_hr']:.2f} h ({t['R_eq']})",
         f"                  longest flowpath {t['L_longest_mi']:.2f} mi at {t['S_longest_ftmi']:.0f} ft/mi",
         f"                  Tc path {t['tc_path']['path_mi']:.2f} mi, mean {t['tc_path']['mean_v_fps']:.1f} ft/s: sheet {t['tc_path']['sheet_hr']:.2f} h | shallow {t['tc_path']['shallow_hr']:.2f} h ({t['tc_path']['shallow_mi']:.2f} mi) | channel {t['tc_path']['channel_hr']:.2f} h ({t['tc_path']['channel_mi']:.2f} mi) | in DEM depressions {t['tc_path']['in_depressions_hr']:.2f} h",
         f"RUNOFF          : CN mean {rr['cn_mean']:.1f} (AMC II {rr['cn2_mean']:.1f}); excess {rr['excess_in']:.2f} in of {s['total_in']:.2f} in (ratio {rr['runoff_ratio']:.2f}); volume {rr['volume_acft']:,.0f} ac-ft",
         f"PEAK            : {rr['peak_cfs']:,.0f} cfs at {rr['peak_time_local']} (translation only {rr['peak_translation_cfs']:,.0f} cfs)",
         f"CROSS-CHECKS    : lumped SCS UH {rr['scs_uh_peak_cfs']:,.0f} cfs | static rational (C=runoff ratio, i at Tc) {rr['static_rational_cfs']:,.0f} cfs | rain ceiling i(Tc)*A {rr['rain_ceiling_cfs']:,.0f} cfs",
         f"                  Tc/R by ARC: " + " | ".join(f"{a_} {v['Tc_hr']:.2f}/{v['R_hr']:.2f} h ({v['iterations']} it)" for a_, v in t['by_arc'].items()) + f"; bankfull pass {t['Tc_bankfull_hr']:.2f}/{t['R_bankfull_hr']:.2f} h",
         ]
    if R.get("design_check"):
        d = R["design_check"]
        L.append(f"DESIGN CHECK    : {d['label']}: {d['peak_cfs']:,.0f} cfs (Tc {d['Tc_hr']:.2f} h)")
        for b in d["benchmarks"]:
            L.append(f"                  vs {b['label']}: {b['cfs']:,.0f} cfs ({b['pct']:.0f}%)")
    if R.get("channel"):
        L.append(f"CHANNEL         : event peak = {R['channel']['pct_design']:.0f}% of the {R['channel']['benchmark_cfs']:,.0f} cfs design flow")
    L.append("")
    L.append("Storm depths (basin average, max running window):")
    for d in s["durations"]:
        L.append(f"   {d['label']:>5}: {d['depth_in']:5.2f} in  ARF {d['arf']:.2f}  ~{fmt_T(d['T'], d['q'])}")
    L.append("")
    L.append("Gauge checks (never used to adjust results):")
    for c in R["checks"]:
        if c["type"] == "rain" and "gauge_total_in" not in c:
            L.append(f"   rain {c['id']}: {c.get('status')}")
        elif c["type"] == "rain":
            L.append(f"   rain {c['id']}: {c['status']}  G {c['gauge_total_in']:.2f} / R {c['radar_cell_in']:.2f} in (G/R {c['G_over_R']:.2f}), r {c['r']:.2f}, lag {c['best_lag_hr']:+d} h")
        else:
            L.append(f"   {c['type']} {c['id']}: {c.get('status')}" + (f"  peak {c['peak_ft']:.2f} ft, {c['offset_hr']:+.1f} h vs model" if c.get('peak_ft') is not None else ""))
            if c.get("onset"):
                o_ = c["onset"]
                L.append(f"        onset: observed rise after {o_['rain_before_obs_in']:.2f} in basin rain; model after {o_['rain_before_model_in']:.2f} in")
        for fl in c.get("flags", []):
            L.append(f"        - {fl}")
    if not R["checks"]:
        L.append("   (no gauge files found)")
    L.append("")
    L.append("Flags:")
    for fl in R["flags"]:
        L.append(f"   - {fl}")
    L.append("")
    L.append("Data sources: " + "; ".join(f"{k}: {v}" for k, v in R["sources"].items()))
    return "\n".join(L)


# =============================================================================
# 8. DRIVER
# =============================================================================
def event_times(evc):
    tz = evc.get("tz", "America/Phoenix")
    t0 = pd.Timestamp(evc["start_local"]).tz_localize(tz).tz_convert("UTC")
    t1 = pd.Timestamp(evc["end_local"]).tz_localize(tz).tz_convert("UTC")
    if t0.minute or t1.minute:
        raise ValueError("event start/end must be on the hour")
    return dict(t0=t0, t1=t1, tz=tz, label=evc.get("label", f"{evc['start_local']} to {evc['end_local']}"))


def basin_extent_ll(bcfg, base, crs):
    """Approximate (south, west, north, east) for the radar window."""
    p = find_file(base, bcfg.get("polygon"))
    if p:
        g = load_polygon(p, "EPSG:4326")
        w, s, e, n = g.bounds
        return s, w, n, e
    h = bcfg["search_box_km"] / 2 / 111.0
    lat, lon = bcfg["outlet"]
    return lat - h, lon - h / math.cos(math.radians(lat)), lat + h, lon + h / math.cos(math.radians(lat))


def _tc_breakdown(T, TT, t, bidx, Tc):
    """Tc flowpath (cell with the largest travel time) and its regime breakdown."""
    c_tc = bidx[np.argmax(t[bidx])]
    tpath = [c_tc]
    while T["rcv"][tpath[-1]] >= 0:
        tpath.append(int(T["rcv"][tpath[-1]]))
    tpath = np.array(tpath)
    ts_, rg_, dp_ = TT["t_seg"][tpath], TT["regime"][tpath], TT["dep"][tpath]
    br = dict(sheet_hr=float(ts_[rg_ == 1].sum()), shallow_hr=float(ts_[rg_ == 2].sum()),
              channel_hr=float(ts_[rg_ == 3].sum()), in_depressions_hr=float(ts_[dp_].sum()),
              path_mi=float(T["L"][c_tc] / 1609.34),
              mean_v_fps=float(T["L"][c_tc] * 3.28084 / max(Tc * 3600, 1)),
              shallow_mi=float(T["seg"][tpath][rg_ == 2].sum() / 1609.34),
              channel_mi=float(T["seg"][tpath][rg_ == 3].sum() / 1609.34))
    return tpath, ts_, rg_, dp_, br


def write_site_layers(layers, outdir):
    """Write overlay layers (data URLs) as PNG files; return [{name, file, bounds, show}]."""
    out = []
    os.makedirs(os.path.join(outdir, "layers"), exist_ok=True)
    for k, (name, (url, bounds, show)) in enumerate(layers.items()):
        fn = f"layers/layer_{k}.png"
        with open(os.path.join(outdir, fn), "wb") as f:
            f.write(base64.b64decode(url.split(",", 1)[1]))
        out.append(dict(name=name, file=fn, bounds=bounds, show=bool(show)))
    return out


def run_basin(key, cfg, event_key, outroot="outputs", ctx=None):
    t_start = time.time()
    common = cfg["common"]
    bc = deep_merge(common, cfg["basins"][key])
    name = bc.get("name", key)
    ev = event_times(cfg["events"][event_key])
    base = cfg["_base"]
    ctx = ctx or {}
    ctx.setdefault("cache", os.path.join(base, "cache"))
    os.makedirs(ctx["cache"], exist_ok=True)
    ctx.update(cfg=cfg, base=base)
    outdir = os.path.join(outroot, key)
    os.makedirs(outdir, exist_ok=True)
    flags, sources = [], {}
    log(f"\n=== {name}: {ev['label']} ===")
    crs = bc["crs"]
    to_utm = transformer("EPSG:4326", crs)
    to_ll = transformer(crs, "EPSG:4326")
    olat, olon = bc["outlet"]
    ox, oy = to_utm.transform(olon, olat)

    # ---- 1. grid, DEM, basin -------------------------------------------------
    poly_path = find_file(base, bc.get("polygon"))
    ref_poly = load_polygon(poly_path, crs) if poly_path else None
    res = bc["dem_res_m"]
    if ref_poly is not None:
        xmin, ymin, xmax, ymax = ref_poly.bounds
        buf = bc["grid_buffer_m"]
        grid = Grid.from_bounds(min(xmin, ox) - buf, min(ymin, oy) - buf, max(xmax, ox) + buf, max(ymax, oy) + buf, res, crs)
    else:
        h = bc["search_box_km"] * 500
        grid = Grid.from_bounds(ox - h, oy - h, ox + h, oy + h, res, crs)
    log(f"  grid {grid.nx} x {grid.ny} @ {res} m")
    z = get_dem(grid, bc, ctx)
    sources["DEM"] = f"USGS 3DEP {res:g} m"
    mode = bc.get("basin_source", "auto")
    bc["_mode"] = "polygon" if mode == "polygon" else "delineate"
    T = terrain_model(grid, z, bc, ctx, ref_poly, (ox, oy))
    A_cell_ac = res * res / 4046.856
    area_d = T["inb"].sum() * A_cell_ac / 640
    area_ref = ref_poly.area / 2589988.11 if ref_poly is not None else None
    area_note = ""
    if bc["_mode"] == "delineate":
        bad = T["touches_edge"]
        diff = abs(area_d / area_ref - 1) if area_ref else 0
        if mode == "auto" and ref_poly is not None and (bad or diff > bc["area_tolerance"]):
            flags.append(f"DEM delineation gave {area_d:.2f} sq mi vs reference polygon {area_ref:.2f} sq mi "
                         f"({diff * 100:.0f}% diff{', touches grid edge' if bad else ''}); routing constrained to the reference polygon.")
            bc["_mode"] = "polygon"
            T = terrain_model(grid, z, bc, ctx, ref_poly, (ox, oy))
            area_d = T["inb"].sum() * A_cell_ac / 640
        elif bad:
            flags.append("Delineated basin touches the grid edge - increase search_box_km.")
        if area_ref and diff > bc["area_tolerance"] and bc["_mode"] == "delineate":
            flags.append(f"Delineated area {area_d:.2f} sq mi differs {diff * 100:.0f}% from the reference polygon ({area_ref:.2f} sq mi).")
    src_txt = "DEM delineation (priority-flood, D8)" if bc["_mode"] == "delineate" else "reference polygon, D8 routing inside it"
    pub = bc.get("published_area_sqmi") or {}
    parts = [f"reference polygon {area_ref:.2f}" if area_ref else None] + [f"{k} {v:.2f}" for k, v in pub.items()]
    area_note = "compare: " + ", ".join(p for p in parts if p) + " sq mi" if any(parts) else ""
    inb = T["inb"]
    bidx = np.flatnonzero(inb)
    oxy = grid.centers([T["outlet"]])
    olon_s, olat_s = to_ll.transform(oxy[0][0], oxy[1][0])
    bx, by = grid.centers(bidx)
    blon, blat = to_ll.transform(bx, by)
    clat, clon = float(np.mean(blat)), float(np.mean(blon))

    # ---- 2. ground conditions ---------------------------------------------
    cls, imp, lc_src = get_landcover(grid, bc, ctx)
    sources["Land cover"] = lc_src
    if lc_src.startswith("DEFAULT"):
        flags.append("NLCD land cover could not be fetched; a single default class was used. Supply nlcd_tif to fix.")
    try:
        hsg, hsg_src = get_hsg(grid, bc, ctx)
    except Exception as e:
        log(f"  SSURGO failed ({e}); using default HSG")
        hsg, hsg_src = np.zeros((grid.ny, grid.nx), np.int8), f"unavailable ({e})"
    unmapped = float((hsg.ravel()[bidx] == 0).mean())
    dflt = {"A": 1, "B": 2, "C": 3, "D": 4}[bc["default_hsg"]]
    if unmapped > 0.01:
        flags.append(f"{unmapped * 100:.0f}% of basin has no SSURGO hydrologic soil group; default HSG {bc['default_hsg']} used there.")
    hsg = np.where(hsg == 0, dflt, hsg)
    sources["Soils"] = hsg_src
    slope = slope_grid(T["z"], res)

    # ---- 3. Atlas 14 + travel time ----------------------------------------
    try:
        q14 = get_atlas14(clat, clon, ctx)
        sources["Precip frequency"] = "NOAA Atlas 14 PFDS (centroid)"
    except Exception as e:
        raise RuntimeError(f"NOAA Atlas 14 fetch failed: {e}")
    p2_24 = float(q14[PFDS_DUR_HR.tolist().index(24), 1])
    TT = travel_time(grid, T, imp, p2_24, bc["travel_time"])
    t = TT["t"]
    Tc = float(t[bidx].max())
    L_ft = T["L"] * 3.28084
    c_long = bidx[np.argmax(T["L"][bidx])]
    L_long_mi = L_ft[c_long] / 5280
    zf = T["z"].ravel()
    S_long_ftmi = (zf[c_long] - zf[T["outlet"]]) * 3.28084 / max(L_long_mi, 1e-6)
    S_long = S_long_ftmi / 5280
    tc_kirpich = 0.0078 * L_ft[c_long] ** 0.77 * max(S_long, 1e-4) ** -0.385 / 60
    A_mi = area_d
    R_hr = r_coefficient(Tc, A_mi, L_long_mi, bc["routing"])
    path = [c_long]
    while T["rcv"][path[-1]] >= 0:
        path.append(int(T["rcv"][path[-1]]))
    tpath, ts_, rg_, dp_, tc_break = _tc_breakdown(T, TT, t, bidx, Tc)
    dep_pct = float(TT["dep"][bidx].mean() * 100)
    px, py = grid.centers(path)
    plon, plat = to_ll.transform(px, py)
    log(f"  basin {A_mi:.2f} sq mi, Tc {Tc:.2f} h, R {R_hr:.2f} h, L {L_long_mi:.2f} mi; Tc path: "
        f"sheet {tc_break['sheet_hr']:.2f} h, shallow {tc_break['shallow_hr']:.2f} h ({tc_break['shallow_mi']:.2f} mi), "
        f"channel {tc_break['channel_hr']:.2f} h ({tc_break['channel_mi']:.2f} mi), in DEM depressions {tc_break['in_depressions_hr']:.2f} h")

    # ---- 4. radar ----------------------------------------------------------
    win = ctx["win"]
    mr = bc["mrms"]
    times_h = hour_range(ev["t0"], ev["t1"])
    Ph, miss_h = get_mrms(mr["pass2_01h"], times_h, win, ctx)
    sources["Radar"] = "MRMS MultiSensor QPE Pass 2 01H (1 km)"
    if miss_h.any():
        flags.append(f"{int(miss_h.sum())} of {len(times_h)} MRMS Pass 2 hours missing (treated as 0).")
    Ph = np.nan_to_num(Ph)
    pi, pj = win.ij(blat, blon)
    if pi.min() < 0 or pj.min() < 0 or pi.max() >= win.ny or pj.max() >= win.nx:
        raise RuntimeError("basin outside radar window")
    pid = pi * win.nx + pj
    upix, pinv = np.unique(pid, return_inverse=True)
    pw = np.bincount(pinv).astype(float)
    pw /= pw.sum()
    Ph_px = Ph.reshape(len(times_h), -1)[:, upix]  # [nh, npix]
    dt_min = bc["dt_min"]
    dt_hr = dt_min / 60
    spm = int(60 // dt_min)
    use15 = mr.get("subhourly", "auto")
    use15 = (min(Tc, tc_kirpich) < mr.get("subhourly_tc_hr", 3.0)) if use15 == "auto" else bool(use15)
    rain_res = "hour"
    if use15:
        times_q = hour_range(ev["t0"], ev["t1"], "15min")
        Pq, miss_q = get_mrms(mr["radaronly_15m"], times_q, win, ctx)
        if miss_q.mean() > 0.25:
            flags.append("MRMS 15-min RadarOnly >25% missing; hourly Pass 2 spread uniformly instead.")
            use15 = False
        else:
            Pq = np.nan_to_num(Pq).reshape(len(times_q), -1)[:, upix].reshape(len(times_h), 4, -1)
            sq = Pq.sum(1)
            shape_ = np.where(sq[:, None, :] > 0.001, Pq / np.maximum(sq[:, None, :], 1e-9), 0.25)
            P15 = (shape_ * Ph_px[:, None, :]).reshape(len(times_h) * 4, -1)
            rain_res = "15 min"
            sources["Radar"] += "; RadarOnly 15M rescaled to Pass 2 hourly"
    if use15:
        rain_px = np.repeat(P15 / (spm // 4), spm // 4, axis=0)
        native = P15
    else:
        rain_px = np.repeat(Ph_px / spm, spm, axis=0)
        native = Ph_px
    n_rain = rain_px.shape[0]
    tail = int(math.ceil(max(bc["tail_hr"], 3 * (Tc + R_hr)) / dt_hr))
    rain_px = np.vstack([rain_px, np.zeros((tail, rain_px.shape[1]))])
    basin_native = native @ pw
    basin_hourly = Ph_px @ pw
    total = float(basin_hourly.sum())
    pix_tot = Ph_px.sum(0)
    # radar-only comparison
    ro_total = None
    if mr.get("compare_radaronly", True):
        try:
            Pro, mro = get_mrms(mr["radaronly_01h"], times_h, win, ctx)
            ro_total = float((np.nan_to_num(Pro).reshape(len(times_h), -1)[:, upix] @ pw).sum())
        except Exception as e:
            log(f"  RadarOnly comparison skipped: {e}")

    # ---- 5. antecedent wetness (radar API) --------------------------------
    an = bc["antecedent"]
    d_end = (ev["t0"] - pd.Timedelta(hours=12)).floor("D") + pd.Timedelta(hours=12)
    if d_end > ev["t0"]:
        d_end -= pd.Timedelta(days=1)
    days = pd.date_range(d_end - pd.Timedelta(days=an["days"] - 1), d_end, freq="1D")
    Pd, miss_d = get_mrms(mr["pass2_24h"], days, win, ctx)
    Pd_b = np.nan_to_num(Pd).reshape(len(days), -1)[:, upix] @ pw
    if miss_d.any():
        flags.append(f"{int(miss_d.sum())} of {len(days)} antecedent days missing in MRMS 24H (treated as 0).")
    api = 0.0
    for p in Pd_b:
        api = an["k"] * api + p
    acl = "dry" if api < an["dry_in"] else ("wet" if api > an["wet_in"] else "normal")
    p5 = float(Pd_b[-5:].sum())
    growing = ev["t0"].tz_convert(ev["tz"]).month in an.get("growing_months", [5, 6, 7, 8, 9])
    lo5, hi5 = an["neh4_growing_in"] if growing else an["neh4_dormant_in"]
    neh4 = "I" if p5 < lo5 else ("III" if p5 > hi5 else "II")
    method = an.get("method", "arc_band")
    if method == "api":
        amc = {"dry": "I", "normal": "II", "wet": "III"}[acl]
        mtxt = f"API {api:.2f} in vs {an['dry_in']}/{an['wet_in']} in"
    elif method == "neh4_5day":
        amc = neh4
        mtxt = f"NEH-4 5-day rule ({'growing' if growing else 'dormant'}): {p5:.2f} in vs {lo5}/{hi5} in"
    else:
        amc = "II"
        mtxt = "NEH-630 ARC II (median) with ARC I-III band"
    antecedent = dict(api_in=float(api), k=an["k"], amc=amc, **{"class": acl}, method=method, method_text=mtxt,
                      prior_5d_in=p5, neh4_5day_amc=neh4,
                      prior_7d_in=float(Pd_b[-7:].sum()), prior_20d_in=float(Pd_b[-20:].sum()),
                      prior_60d_in=float(Pd_b.sum()), days=[str(d) for d in days], daily_in=Pd_b.tolist())

    # ---- 6. CN grid -------------------------------------------------------------
    _cn_unused, cn2, cn2_flat = build_cn(cls, hsg, imp, slope, cfg["_cn"], "II")
    kin = bc["travel_time"].get("channel_velocity", "bankfull") == "kinematic"
    area_cells = np.full(bidx.size, A_cell_ac)
    Tc_bf, R_bf, TT_bf, t_bf = Tc, R_hr, TT, t
    ia_r, att = cfg["_cn"]["ia_ratio"], bc["routing"].get("attenuation", True)

    def route(rain, pix, cnb_a):
        """ModClark on the bankfull grid, then (kinematic) repeat Q -> velocity -> Tc, R -> Q
        until the peak changes by < 2% (damped)."""
        cp_, cc_, W_ = build_combos(pix, cnb_a, np.floor(t_bf[bidx] / dt_hr).astype(int), area_cells)
        res = modclark(rain, cp_, cc_, W_, dt_hr, R_bf, ia_r, att)
        info = dict(t=t_bf, TT=TT_bf, Tc=Tc_bf, R=R_bf, iters=0, converged=True,
                    bankfull_peak_cfs=float(res[1].max()))
        if not kin or float(res[1].max()) < 1.0:
            return res, info  # no runoff: nothing to route kinematically
        Q = float(res[1].max())
        for it in range(1, 16):
            TTk = travel_time(grid, T, imp, p2_24, bc["travel_time"], q_peak=Q, A_total_mi=A_mi)
            tk = TTk["t"]
            Tck = float(tk[bidx].max())
            Rk = r_coefficient(Tck, A_mi, L_long_mi, bc["routing"])
            cp_, cc_, W_ = build_combos(pix, cnb_a, np.floor(tk[bidx] / dt_hr).astype(int), area_cells)
            res = modclark(rain, cp_, cc_, W_, dt_hr, Rk, ia_r, att)
            Qn = max(float(res[1].max()), 1.0)
            done = abs(Qn - Q) / Q < 0.02
            info.update(t=tk, TT=TTk, Tc=Tck, R=Rk, iters=it, converged=done)
            if done:
                break
            Q = 0.5 * (Q + Qn)
        return res, info

    runs, rinfo = {}, {}
    for a_ in ("I", "II", "III"):
        cn_a = amc_convert(cn2, a_, cfg["_cn"].get("cn_bounds", [30, 99]))
        cnb_a = cn_a.ravel()[bidx]
        res_, info_ = route(rain_px, pinv, cnb_a)
        runs[a_] = (*res_, cn_a, cnb_a)
        rinfo[a_] = info_
        log(f"  ARC {a_}: peak {float(res_[1].max()):,.0f} cfs (bankfull pass {info_['bankfull_peak_cfs']:,.0f}); "
            f"Tc {info_['Tc']:.2f} h, R {info_['R']:.2f} h, {info_['iters']} kinematic iterations")
    I, O, vol_acin, E, cn, cnb = runs[amc]
    t, TT, Tc, R_hr = rinfo[amc]["t"], rinfo[amc]["TT"], rinfo[amc]["Tc"], rinfo[amc]["R"]
    tpath, ts_, rg_, dp_, tc_break = _tc_breakdown(T, TT, t, bidx, Tc)
    dep_pct = float(TT["dep"][bidx].mean() * 100)
    A_ac = bidx.size * A_cell_ac
    excess_in = vol_acin / A_ac
    t_model = ev["t0"] + pd.to_timedelta(np.arange(1, len(O) + 1) * dt_min, unit="min")
    kpk = int(np.argmax(O))
    Qp = float(O[kpk])
    t_pk = t_model[kpk]
    rain_end_idx = np.flatnonzero(basin_native > 0.01)
    step_native = 60 if rain_res == "hour" else 15
    rain_end = ev["t0"] + pd.Timedelta(minutes=step_native * (int(rain_end_idx[-1]) + 1 if len(rain_end_idx) else 0))

    # ---- 7. storm intensity ----------------------------------------------------
    st = []
    nat_hr = step_native / 60
    for D in list(bc["durations_hr"]) + ["Tc"]:
        Dh = max(Tc, nat_hr) if D == "Tc" else float(D)
        n = int(round(Dh / nat_hr))
        if abs(n * nat_hr - Dh) > 1e-6 and D != "Tc":
            continue
        dep, kend = max_running(basin_native, n)
        if not np.isfinite(dep):
            continue
        Dn = n * nat_hr
        arf = arf_tp29(Dn, A_mi)
        Tq = return_period(dep, PFDS_T, atlas_depth(q14, Dn) * arf)
        lab = (f"{int(Dn * 60)}m" if Dn < 1 else f"{Dn:g}h") if D != "Tc" else f"Tc({Dn:.2f}h)"
        rec = dict(label=lab, duration_hr=Dn, depth_in=dep, arf=arf, T=Tq[0], q=Tq[1], intensity_inhr=dep / Dn,
                   end_local=str((ev["t0"] + pd.Timedelta(minutes=step_native * (kend + 1))).tz_convert(ev["tz"])))
        if D == "Tc":
            tcrec = rec
        else:
            st.append(rec)
    def tkey(d):
        return (d["T"] if np.isfinite(d["T"]) else 0) + (0.001 if d["q"] == ">" else 0) - (0.5 if d["q"] == "<" else 0)
    ctrl = max(st, key=tkey)
    storm = dict(durations=st, tc_duration=tcrec, controlling=ctrl, total_in=total,
                 max_1hr_in=float(basin_hourly.max()), resolution=rain_res,
                 headline=f"~{fmt_T(ctrl['T'], ctrl['q'])} storm at {ctrl['label']} ({ctrl['depth_in']:.2f} in)",
                 radaronly_total_in=ro_total)
    if ro_total:
        storm["pass2_vs_radaronly"] = total / ro_total if ro_total > 0 else None

    # ---- 8. cross-checks ---------------------------------------------------------
    i_tc = tcrec["intensity_inhr"]
    kb = bc["fcdmc_tc"]["Kb"]
    tcf = Tc
    for _ in range(30):
        n = max(1, int(round(tcf / nat_hr)))
        dep, _k = max_running(basin_native, n)
        i_ = max(dep / (n * nat_hr), 0.01)
        tcf_new = 11.4 * L_long_mi ** 0.5 * kb ** 0.52 * max(S_long_ftmi, 1) ** -0.31 * i_ ** -0.38
        if abs(tcf_new - tcf) < 0.01:
            break
        tcf = 0.5 * (tcf + tcf_new)
    basin_rain_steps = rain_px @ pw
    cn_comp = float(np.mean(cnb))
    q_uh, Tp = scs_uh_peak(basin_rain_steps, cn_comp, A_mi, Tc, dt_hr, cfg["_cn"]["ia_ratio"])
    C_eq = excess_in / total if total > 0 else 0
    Q_rat = C_eq * i_tc * A_ac * 1.008
    rain_ceiling = i_tc * A_ac * 1.008
    design = None
    if bc.get("design_check"):
        dc = bc["design_check"]
        dfn = lambda d: atlas_depth(q14, d, dc["T"]) * arf_tp29(dc["D_hr"], A_mi)
        hy = alternating_block(dfn, dc["D_hr"], dt_hr)
        rain_d = np.concatenate([hy, np.zeros(int(math.ceil(3 * (Tc_bf + R_bf) / dt_hr)) + 12)])[:, None]
        cnd = amc_convert(cn2, dc.get("amc", "II"), cfg["_cn"].get("cn_bounds", [30, 99])).ravel()[bidx]
        resd, infod = route(rain_d, np.zeros(bidx.size, int), cnd)
        pk_d = float(resd[1].max())
        bms = [dict(b, pct=pk_d / b["cfs"] * 100) for b in dc.get("benchmarks", [])]
        design = dict(label=f"NOAA Atlas 14 {dc['T']}-yr {dc['D_hr']:g}-hr, alternating block, ARF, AMC {dc.get('amc', 'II')}",
                      depth_in=float(hy.sum()), peak_cfs=pk_d, bankfull_peak_cfs=infod["bankfull_peak_cfs"],
                      Tc_hr=infod["Tc"], R_hr=infod["R"], benchmarks=bms, note=dc.get("note", ""),
                      hydrograph=np.round(resd[1][:int(math.ceil((dc["D_hr"] + 3 * (infod["Tc"] + infod["R"])) / dt_hr))], 1).tolist())

    # ---- 9. flood frequency -----------------------------------------------------
    stats = peak_stats(bc, ctx, A_mi)
    A_ac_tmp = bidx.size * A_cell_ac
    fT = flood_T(Qp, stats)
    if stats:
        head = f"Peak ~ {Qp:,.0f} cfs, about a {fmt_T(fT['T'], fT['q'])} flood"
        rng = ""
        if fT.get("T_lo"):
            rng = f"90% prediction range: {fmt_T(*fT['T_lo'])} to {fmt_T(*fT['T_hi'])}"
        if stats["transfer_note"]:
            flags.append(stats["transfer_note"])
            rng += (" | " if rng else "") + "area-transferred statistics"
        rar = ""
        if np.isfinite(fT["T"]) and np.isfinite(ctrl["T"]) and ctrl["T"] > 0:
            rar = f"flood {fT['T'] / ctrl['T']:.1f}x the storm return period"
    else:
        head = f"Peak ~ {Qp:,.0f} cfs (no peak-flow statistics loaded)"
        rng, rar = "Upload StreamStats peak-flow CSV to get a return period.", ""
        flags.append("No peak-flow frequency statistics - flood return period not computed.")
    band = {}
    for a_, rr_ in runs.items():
        q_ = float(rr_[1].max())
        band[a_] = dict(peak_cfs=q_, cn_mean=float(rr_[5].mean()), excess_in=rr_[2] / A_ac_tmp,
                        T=flood_T(q_, stats)["T"] if stats else float("nan"), q=flood_T(q_, stats)["q"] if stats else "")
    band_txt = "Antecedent band: " + " | ".join(
        f"ARC {a_} {b_['peak_cfs']:,.0f} cfs" + (f" (~{fmt_T(b_['T'], b_['q'])})" if stats else "") for a_, b_ in band.items())
    flood = dict(peak_cfs=Qp, T=fT["T"], q=fT["q"], T_lo=fT.get("T_lo"), T_hi=fT.get("T_hi"), headline=head,
                 range_text=rng, rarity_text=rar or "n/a", stats=stats, arc_band=band, band_text=band_txt)

    channel = None
    if bc.get("channel"):
        ch = dict(bc["channel"])
        ch["pct_design"] = Qp / ch["benchmark_cfs"] * 100
        channel = ch
    for a_, inf_ in rinfo.items():
        if not inf_.get("converged", True):
            flags.append(f"ARC {a_}: kinematic velocity loop did not converge in {inf_['iters']} iterations.")

    # ---- 10. flags (snow, size, structure) -------------------------------------
    elev_ft = T["z"].ravel()[bidx] * 3.28084
    month = ev["t0"].tz_convert(ev["tz"]).month
    if month in (11, 12, 1, 2, 3, 4) and elev_ft.mean() > 7000:
        flags.append(f"Cold season and mean elevation {elev_ft.mean():,.0f} ft: snow or frozen ground possible - CN losses may be wrong.")
    if mr.get("precipflag_check", True):
        try:
            hrs = [times_h[k] for k in np.argsort(-basin_hourly)[:12] if basin_hourly[k] >= 0.05]
            if hrs:
                Fl, _m = get_mrms(mr["precipflag"], pd.DatetimeIndex(hrs), win, ctx, scale=1.0, negative_is_missing=False)
                Flb = Fl.reshape(len(hrs), -1)[:, upix]
                prec = (Flb > 0)
                sf = float((Flb == 3).sum() / max(prec.sum(), 1))
                antecedent["snow_fraction_wet_hours"] = sf
                if sf > 0.2:
                    flags.append(f"MRMS PrecipFlag shows snow in {sf * 100:.0f}% of precipitating basin pixels during the wettest hours.")
        except Exception as e:
            log(f"  PrecipFlag check skipped: {e}")
    if tc_break["in_depressions_hr"] > 0.25 * Tc:
        flags.append(f"{tc_break['in_depressions_hr']:.1f} h of Tc is spent in filled DEM depressions (embankments/culverts?) - check Tc.")
    if not (0.6 <= Tc / max(tcf, 1e-6) <= 1.6):
        flags.append(f"Grid Tc {Tc:.2f} h disagrees with FCDMC Tc {tcf:.2f} h (ratio {Tc / tcf:.2f}).")
    if A_mi > bc.get("large_basin_flag_sqmi", 10):
        flags.append(f"Basin is {A_mi:.1f} sq mi: single linear reservoir, no channel storage routing; treat peak as screening-level.")
    for nt_ in bc.get("structure_notes", []):
        flags.append("Structure note (not modelled): " + nt_)

    # ---- 11. gauge checks -------------------------------------------------------
    checks = []
    model_info = dict(peak_time_utc=t_pk, peak_cfs=Qp, Tc_hr=Tc, R_hr=R_hr, rain_end_utc=rain_end,
                      t_model=t_model, O=O, cum_rain=np.cumsum(rain_px @ pw))
    for g in bc.get("gauges", []):
        p = find_file(base, g.get("file"), g.get("file_exclude"))
        if not p:
            checks.append(dict(id=g["id"], type=g["type"], lat=g["lat"], lon=g["lon"], status="no data",
                               flags=[f"no file matching {g.get('file')}"]))
            continue
        try:
            s = read_alert(p, g.get("tz", "UTC"))
            if g["type"] == "rain":
                inc = rain_increments(s, g.get("units", "auto"))
                checks.append(rain_gauge_check(g, inc, ev, win, Ph, times_h, pix_tot, bc["checks"]))
            else:
                checks.append(stage_gauge_check(g, s, ev, model_info, bc["checks"]))
        except Exception as e:
            checks.append(dict(id=g["id"], type=g["type"], lat=g["lat"], lon=g["lon"], status="no data",
                               flags=[f"read error: {e}"]))
    for c in checks:
        if c.get("status") == "flag":
            flags.append(f"Gauge {c['id']} check flagged: " + "; ".join(c.get("flags", [])[:2]))

    # ---- 12. assemble -----------------------------------------------------------
    imp_b = imp.ravel()[bidx]
    sl_b = slope.ravel()[bidx]
    R = dict(version=VERSION, key=key, name=name, event=dict(label=ev["label"], tz=ev["tz"], start_utc=str(ev["t0"]),
                                                              end_utc=str(ev["t1"])),
             basin=dict(area_sqmi=A_mi, area_ref_sqmi=area_ref, source=src_txt, area_note=area_note,
                        outlet_lat=olat_s, outlet_lon=olon_s, centroid_lat=clat, centroid_lon=clon,
                        mean_elev_ft=float(elev_ft.mean()), min_elev_ft=float(elev_ft.min()), max_elev_ft=float(elev_ft.max()),
                        mean_slope_pct=float(sl_b.mean() * 100), impervious_pct=float(imp_b.mean() * 100),
                        hsg_pct={h: float((hsg.ravel()[bidx] == i).mean() * 100) for h, i in zip("ABCD", range(1, 5))},
                        unmapped_soils_pct=unmapped * 100),
             timing=dict(Tc_grid_hr=Tc, Tc_kirpich_hr=tc_kirpich, Tc_fcdmc_hr=tcf, R_hr=R_hr,
                         R_eq=bc["routing"]["R_equation"], L_longest_mi=L_long_mi, S_longest_ftmi=S_long_ftmi,
                         regime_pct={k: float((TT["regime"][bidx] == v).mean() * 100) for k, v in
                                     (("sheet", 1), ("shallow", 2), ("channel", 3))},
                         tc_path=tc_break, depression_cells_pct=dep_pct,
                         channel_velocity="kinematic" if kin else "bankfull",
                         Tc_bankfull_hr=Tc_bf, R_bankfull_hr=R_bf,
                         by_arc={a_: dict(Tc_hr=i_["Tc"], R_hr=i_["R"], iterations=i_["iters"],
                                          converged=i_.get("converged", True), bankfull_peak_cfs=i_["bankfull_peak_cfs"])
                                 for a_, i_ in rinfo.items()}),
             runoff=dict(cn_mean=float(cnb.mean()), cn2_mean=float(cn2.ravel()[bidx].mean()),
                         cn2_noslope_mean=float(cn2_flat.ravel()[bidx].mean()), excess_in=excess_in,
                         runoff_ratio=C_eq, volume_acft=vol_acin / 12, peak_cfs=Qp,
                         peak_time_local=str(t_pk.tz_convert(ev["tz"]).strftime("%b %d %H:%M %Z")),
                         peak_translation_cfs=float(I.max()), scs_uh_peak_cfs=float(q_uh.max()), scs_uh_Tp_hr=Tp,
                         static_rational_cfs=Q_rat, C_equivalent=C_eq, i_tc_inhr=i_tc, dt_min=dt_min,
                         rain_ceiling_cfs=rain_ceiling),
             storm=storm, antecedent=antecedent, flood=flood, design_check=design, channel=channel,
             checks=checks, flags=flags, sources=sources)
    R["details_table"] = [
        ("Peak time", R["runoff"]["peak_time_local"]),
        ("Translation-only peak", f"{I.max():,.0f} cfs"),
        ("Lumped SCS UH peak", f"{q_uh.max():,.0f} cfs"),
        ("Static rational (C=runoff ratio)", f"{Q_rat:,.0f} cfs"),
        ("Rain ceiling i(Tc)*A (100% runoff)", f"{rain_ceiling:,.0f} cfs"),
        ("Tc / R, bankfull pass", f"{Tc_bf:.2f} / {R_bf:.2f} h"),
        ("Runoff", f"{excess_in:.2f} in, {vol_acin / 12:,.0f} ac-ft"),
        ("CN (ARC I / II / III)", " / ".join(f"{band[a_]['cn_mean']:.1f}" for a_ in ("I", "II", "III"))),
        ("Tc path breakdown", f"sheet {tc_break['sheet_hr']:.2f} / shallow {tc_break['shallow_hr']:.2f} / channel {tc_break['channel_hr']:.2f} / depressions {tc_break['in_depressions_hr']:.2f} h"),
        ("Tc grid / Kirpich / FCDMC", f"{Tc:.2f} / {tc_kirpich:.2f} / {tcf:.2f} h"),
        ("Clark R", f"{R_hr:.2f} h"),
        ("Longest flowpath", f"{L_long_mi:.2f} mi @ {S_long_ftmi:.0f} ft/mi"),
        ("Flow regimes (sheet/shallow/channel)", "/".join(f"{v:.0f}%" for v in R["timing"]["regime_pct"].values())),
        ("Elevation", f"{elev_ft.min():,.0f}-{elev_ft.max():,.0f} ft (mean {elev_ft.mean():,.0f})"),
        ("Mean slope / impervious", f"{sl_b.mean() * 100:.1f}% / {imp_b.mean() * 100:.1f}%"),
        ("HSG A/B/C/D", "/".join(f"{v:.0f}" for v in R["basin"]["hsg_pct"].values()) + " %"),
        ("Prior 7/20/60-day rain", f"{antecedent['prior_7d_in']:.2f} / {antecedent['prior_20d_in']:.2f} / {antecedent['prior_60d_in']:.2f} in"),
        ("Pass 2 / RadarOnly total", f"{total:.2f} / {ro_total:.2f} in" if ro_total else f"{total:.2f} in / n/a"),
        ("Rain resolution", rain_res),
        ("Area", f"{A_mi:.2f} sq mi; {area_note}"),
    ] + ([("Design check", f"{design['peak_cfs']:,.0f} cfs; " + "; ".join(f"{b['label']} {b['cfs']:,.0f} ({b['pct']:.0f}%)" for b in design['benchmarks']))] if design else []) \
      + ([("Channel", f"{channel['pct_design']:.0f}% of {channel['benchmark_cfs']:,.0f} cfs design flow")] if channel else [])

    # ---- internal arrays for outputs
    nshow = min(len(O), n_rain + int(math.ceil((3 * (Tc + R_hr) + 6) / dt_hr)))
    R["_hydro"] = dict(t_utc=[str(x) for x in t_model[:nshow]], Q_cfs=O[:nshow].tolist(), I_cfs=I[:nshow].tolist())
    tn = ev["t0"] + pd.to_timedelta(np.arange(1, len(basin_native) + 1) * step_native, unit="min")
    R["_rain"] = dict(t_utc=[str(x) for x in tn], basin_in=basin_native.tolist(), res_label=rain_res)
    R["_atlas14"] = q14.tolist()
    R["_profile"] = dict(dist_mi=(T["L"][tpath] / 1609.34).tolist(), elev_ft=(T["z"].ravel()[tpath] * 3.28084).tolist(),
                         t_hr=t[tpath].tolist(), regime=rg_.tolist(), dep=dp_.tolist(), v_fps=TT["v"][tpath].tolist())
    tg = np.full(grid.ny * grid.nx, np.nan)
    tg[bidx] = t[bidx]
    tg = tg.reshape(grid.ny, grid.nx)
    R["_tgrid"] = tg
    b_ = grid.bounds
    R["_grid_extent"] = [b_[0], b_[2], b_[1], b_[3]]
    outline_ll, outline_utm = basin_outline_ll(inb.reshape(grid.ny, grid.nx), grid)
    from shapely.geometry import mapping
    R["_outline_geojson"] = mapping(outline_ll)
    geom = outline_ll.geoms[np.argmax([g.area for g in outline_ll.geoms])] if hasattr(outline_ll, "geoms") else outline_ll
    R["_outline_ll"] = (list(geom.exterior.xy[0]), list(geom.exterior.xy[1]))
    if ref_poly is not None:
        from shapely.ops import transform as stransform
        R["_ref_geojson"] = mapping(stransform(lambda x, y, z=None: to_ll.transform(x, y), ref_poly))
    R["_longest_path"] = [[float(a), float(b)] for a, b in zip(plat, plon)][::max(1, len(path) // 400)] + [[olat_s, olon_s]]
    # storm field + track
    tot_w = Ph.sum(0)
    lat_w = win.lat(np.arange(win.ny))[:, None] * np.ones((1, win.nx))
    lon_w = np.ones((win.ny, 1)) * win.lon(np.arange(win.nx))[None, :]
    track = []
    for k in range(len(times_h)):
        f_ = np.clip(Ph[k] - np.percentile(Ph[k], 50), 0, None)  # storm core above the hour's median
        sm = f_.sum()
        if sm > 0 and basin_hourly[k] > 0.02:
            track.append((str(times_h[k]), float((f_ * lat_w).sum() / sm), float((f_ * lon_w).sum() / sm),
                          float(basin_hourly[k] / max(basin_hourly.max(), 1e-9))))
    motion = "Storm track: n/a"
    if len(track) >= 3:
        d_lat = track[-1][1] - track[0][1]
        d_lon = (track[-1][2] - track[0][2]) * math.cos(math.radians(clat))
        brg = (math.degrees(math.atan2(d_lon, d_lat)) + 360) % 360
        fl_brg = (math.degrees(math.atan2((olon_s - plon[0]) * math.cos(math.radians(clat)), olat_s - plat[0])) + 360) % 360
        diff = abs((brg - fl_brg + 180) % 360 - 180)
        rel = "with the flowpath (peaks stack)" if diff < 60 else ("against the flowpath (peaks spread)" if diff > 120 else "across the flowpath")
        motion = f"Rain-weighted centroid moved toward {brg:.0f} deg; flowpath runs toward {fl_brg:.0f} deg -> storm moved {rel}"
        R["storm"]["motion"] = dict(bearing_deg=brg, flowpath_bearing_deg=fl_brg, relation=rel)
    R["_storm_field"] = dict(total=tot_w, bounds=win.bounds_ll, track=track, motion_note=motion)

    # ---- 13. write ---------------------------------------------------------------
    figs = write_figures(R, outdir)
    layers = {}
    show = {"Storm total (MRMS Pass 2)": True}
    st_url = _png_data_url(_colorize(np.where(tot_w > 0.01, tot_w, np.nan), "YlGnBu", 0, max(tot_w.max(), 0.1), 0.55))
    sb = win.bounds_ll
    layers["Storm total (MRMS Pass 2)"] = (st_url, [[sb[0], sb[1]], [sb[2], sb[3]]], True)
    mask2 = inb.reshape(grid.ny, grid.nx)
    layers["Travel-time isochrones (hr)"] = (*overlay_wgs84(np.floor(tg / max(0.25, Tc / 10)) * max(0.25, Tc / 10), grid, "viridis_r", 0, Tc, 0.6), False)
    elev = np.where(mask2, T["z"] * 3.28084, np.nan)
    bw = max(250, math.ceil((np.nanmax(elev) - np.nanmin(elev)) / 12 / 250) * 250)
    layers[f"Elevation bands ({bw} ft)"] = (*overlay_wgs84(np.floor(elev / bw) * bw, grid, "terrain", np.nanmin(elev), np.nanmax(elev), 0.6), False)
    sln = np.clip(slope / max(np.nanpercentile(slope.ravel()[bidx], 95), 1e-6), 0, 1)
    prox = 1 - T["L"].reshape(grid.ny, grid.nx) / max(T["L"][bidx].max(), 1)
    risk = np.where(mask2, 0.45 * sln + 0.35 * imp + 0.20 * prox, np.nan)
    layers["Runoff risk (steepness + impervious)"] = (*overlay_wgs84(risk, grid, "OrRd", 0, 1, 0.6), False)
    layers[f"Curve number (AMC {amc})"] = (*overlay_wgs84(np.where(mask2, cn, np.nan), grid, "RdYlGn_r", 40, 98, 0.6), False)
    chn = np.where(TT["chan"].reshape(grid.ny, grid.nx), np.log10(TT["acc_ac"].reshape(grid.ny, grid.nx)), np.nan)
    layers["Flowpaths (channel network)"] = (*overlay_wgs84(chn, grid, "Blues", 0, np.nanmax(chn) if np.isfinite(chn).any() else 1, 0.95), True)
    site_layers = write_site_layers(layers, outdir)
    txt = summary_text(R)
    open(os.path.join(outdir, "summary.txt"), "w").write(txt)
    pub = {k: v for k, v in R.items() if not k.startswith("_")}
    pub["hydrograph"] = R["_hydro"]
    pub["basin_rain"] = R["_rain"]
    pub["runtime_s"] = round(time.time() - t_start, 1)
    pub["map"] = dict(layers=site_layers, outline=R["_outline_geojson"], reference=R.get("_ref_geojson"),
                      longest_path=R["_longest_path"], outlet=[olat_s, olon_s])
    pub["figures"] = {k: os.path.basename(v) for k, v in figs.items()}
    pub["summary_text"] = txt
    json.dump(pub, open(os.path.join(outdir, "summary.json"), "w"), indent=1, default=lambda o: o if not isinstance(o, (np.floating, np.integer, np.bool_)) else o.item())
    pd.DataFrame(dict(time_local=[pd.Timestamp(x).tz_convert(ev["tz"]) for x in R["_hydro"]["t_utc"]],
                      Q_cfs=R["_hydro"]["Q_cfs"], translation_cfs=R["_hydro"]["I_cfs"])).to_csv(os.path.join(outdir, "hydrograph.csv"), index=False)
    try:
        import rasterio
        prof = dict(driver="GTiff", height=grid.ny, width=grid.nx, count=1, dtype="float32", crs=grid.crs,
                    transform=grid.transform, nodata=np.nan, compress="deflate")
        for nm, a in (("travel_time_hr", tg), ("curve_number", np.where(mask2, cn, np.nan))):
            with rasterio.open(os.path.join(outdir, f"{nm}.tif"), "w", **prof) as ds:
                ds.write(a.astype("float32"), 1)
    except Exception as e:
        log(f"  GeoTIFF export skipped: {e}")
    log(txt)
    R["_files"] = dict(summary=os.path.join(outdir, "summary.txt"), **figs)
    return R


def preflight(config="basins.json"):
    """Quick connectivity test for every public data source (tiny requests)."""
    cfg = load_config(config)
    t = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)).floor("D") + pd.Timedelta(hours=12)
    tests = {
        "MRMS (AWS)": lambda: http_get(_mrms_urls(cfg["common"]["mrms"]["pass2_01h"], t)[0], timeout=60, tries=1)[:2],
        "MRMS (IEM fallback)": lambda: http_get(_mrms_urls(cfg["common"]["mrms"]["pass2_01h"], t)[1], timeout=60, tries=1)[:2],
        "GRIB decoder": lambda: decode_grib_window(http_get(_mrms_urls(cfg["common"]["mrms"]["pass2_01h"], t)[0], tries=1),
                                                   Window(35.1, 35.3, -111.8, -111.6)).shape,
        "USGS 3DEP ImageServer": lambda: _dem_exportimage(Grid(430000, 3900000, 10, 20, 20)).shape,
        "USGS 3DEP COG tiles (fallback)": lambda: float(np.nanmean(_dem_cog(Grid(430000, 3900000, 10, 20, 20)))),
        "NOAA Atlas 14": lambda: get_atlas14(35.25, -111.7, dict(cache=tempfile.mkdtemp()))[9, 1],
        "MRLC NLCD (WCS)": lambda: _fetch_mrlc_wcs(Grid(430000, 3900000, 30, 10, 10), cfg["common"]["landcover"]["wcs_landcover"]).shape,
        "MRLC NLCD (WMS)": lambda: _fetch_mrlc_wms(Grid(430000, 3900000, 30, 10, 10), cfg["common"]["landcover"]["wms_landcover"]).shape,
        "USDA Soil Data Access": lambda: len(_sda("SELECT TOP 1 mukey FROM mapunit")[1]),
    }
    ok = {}
    for k, f in tests.items():
        try:
            r = f()
            ok[k] = True
            print(f"  OK    {k}: {r}")
        except Exception as e:
            ok[k] = False
            print(f"  FAIL  {k}: {str(e)[:160]}")
    return ok


def run_event(basin_keys, event_key, config="basins.json", outroot="outputs", cache=None):
    cfg = load_config(config)
    if basin_keys in (None, "all"):
        basin_keys = list(cfg["basins"].keys())
    exts = []
    for k in basin_keys:
        bc = deep_merge(cfg["common"], cfg["basins"][k])
        exts.append(basin_extent_ll(bc, cfg["_base"], bc["crs"]))
    m = cfg["common"]["mrms"]["window_margin_deg"]
    win = Window(min(e[0] for e in exts) - m, max(e[2] for e in exts) + m,
                 min(e[1] for e in exts) - m, max(e[3] for e in exts) + m)
    ctx = dict(win=win, cache=cache or os.path.join(cfg["_base"], "cache"))
    out = {}
    for k in basin_keys:
        try:
            out[k] = run_basin(k, cfg, event_key, outroot, dict(ctx))
        except Exception as e:
            import traceback
            traceback.print_exc()
            log(f"!! {k} failed: {e}")
    rows = []
    for k, R in out.items():
        rows.append(dict(basin=R["name"], area_sqmi=round(R["basin"]["area_sqmi"], 2),
                         storm=R["storm"]["headline"], peak_cfs=round(R["flood"]["peak_cfs"]),
                         flood=R["flood"]["headline"], Tc_hr=round(R["timing"]["Tc_grid_hr"], 2),
                         R_hr=round(R["timing"]["R_hr"], 2), CN=round(R["runoff"]["cn_mean"], 1),
                         ARC=R["antecedent"]["amc"], band=R["flood"]["band_text"], flags=len(R["flags"])))
    if rows:
        os.makedirs(outroot, exist_ok=True)
        pd.DataFrame(rows).to_csv(os.path.join(outroot, f"event_summary_{event_key}.csv"), index=False)
    open(os.path.join(outroot, "run_log.txt"), "w").write("\n".join(_LOG))
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=f"coconino_storm v{VERSION}")
    ap.add_argument("--config", default="basins.json")
    ap.add_argument("--event", required=True)
    ap.add_argument("--basins", nargs="*", default=None)
    ap.add_argument("--out", default="outputs")
    a = ap.parse_args()
    run_event(a.basins or "all", a.event, a.config, a.out)
