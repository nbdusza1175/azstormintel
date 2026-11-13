"""5-minute radar rainfall for a window: MRMS PrecipRate timing scaled to Pass 2 hourly depth.

Method (unchanged from radar-viz 1.0.1):
- PrecipRate (2-min, mm/hr): each scan is held for 2 min; minutes are summed into 5-min depths.
  A scan older than 6 min is not used (gaps of up to 2 scans are forward-filled).
- Pass 2 01H (mm): for every cell and hour, the 5-min depths are scaled so they add up to Pass 2
  (factor clipped to 0.1-10 and counted). Where Pass 2 has rain but PrecipRate has none, the
  Pass 2 depth is spread evenly over the hour.
- Reflectivity: scan nearest the middle of each 5-min step (display only; < 5 dBZ hidden).
- PrecipFlag: snow-flagged share of basin area at each step.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
from shapely.geometry import box, shape
from shapely.ops import unary_union

from . import mrms

R_MI = 3958.8
MM_PER_IN = 25.4


# ---------------------------------------------------------------- basin cells
def basin_cells(geojson: dict, grid: mrms.Grid):
    """Clip grid cells to the basin polygon.

    Returns dict with flat cell indices, fraction inside, area in basin (sq mi, ac).
    """
    polys = [shape(f["geometry"]) for f in geojson["features"]
             if f["geometry"]["type"] in ("Polygon", "MultiPolygon")]
    poly = unary_union(polys)
    lats, lons, d = grid.lats(), grid.lons(), grid.d
    idx, frac, area = [], [], []
    for i, la in enumerate(lats):
        for j, lo in enumerate(lons):
            c = box(lo - d / 2, la - d / 2, lo + d / 2, la + d / 2)
            if not c.intersects(poly):
                continue
            f = c.intersection(poly).area / c.area
            if f <= 1e-6:
                continue
            cell_sqmi = (R_MI ** 2) * np.radians(d) * (np.sin(np.radians(la + d / 2)) - np.sin(np.radians(la - d / 2)))
            idx.append(i * grid.shape[1] + j)
            frac.append(f)
            area.append(f * cell_sqmi)
    area = np.array(area)
    return {
        "idx": np.array(idx), "frac": np.array(frac), "area_sqmi": area,
        "area_ac": area * 640.0, "total_sqmi": float(area.sum()), "polygon": poly,
    }


# ---------------------------------------------------------------- main build
def build(start_utc: datetime, end_utc: datetime, bbox, cache_dir=None, step_min=5, log=print):
    """Return dict with region 5-min depth (in), intensity (in/hr), dBZ, snow flag, QA."""
    assert step_min >= 5 and step_min % 1 == 0
    step = timedelta(minutes=step_min)
    ends = []
    t = start_utc + step
    while t <= end_utc:
        ends.append(t)
        t += step
    n = len(ends)

    # --- listings
    pr_keys = mrms.list_product(mrms.PRECIP_RATE, start_utc - timedelta(minutes=10), end_utc)
    p2_keys = mrms.list_product(mrms.PASS2_01H, start_utc + timedelta(hours=1) - timedelta(minutes=1),
                                end_utc + timedelta(minutes=1))
    dbz_keys = mrms.list_product(mrms.REFLECTIVITY, start_utc - timedelta(minutes=3), end_utc)
    flag_keys = mrms.list_product(mrms.PRECIP_FLAG, start_utc - timedelta(minutes=3), end_utc)
    log(f"listed PrecipRate {len(pr_keys)}, Pass2 {len(p2_keys)}, refl {len(dbz_keys)}, flag {len(flag_keys)}")

    # nearest-to-mid-step picks for reflectivity and flag
    def pick(keys):
        if not keys:
            return [None] * n
        ts = np.array([k[0].timestamp() for k in keys])
        out = []
        for e in ends:
            mid = (e - step / 2).timestamp()
            j = int(np.argmin(np.abs(ts - mid)))
            out.append(keys[j][1] if abs(ts[j] - mid) <= 300 else None)
        return out

    dbz_pick, flag_pick = pick(dbz_keys), pick(flag_keys)

    need = [k for _, k in pr_keys] + [k for _, k in p2_keys] + \
           [k for k in set(dbz_pick) if k] + [k for k in set(flag_pick) if k]
    log(f"reading {len(need)} files")
    data = mrms.read_many(need, bbox, cache_dir)
    grid = next(iter(data.values()))[0]
    shp = grid.shape

    # --- PrecipRate -> minute rates -> 5-min depth (mm)
    pr = [(ts, data[k][1]) for ts, k in pr_keys if k in data]
    pr_ts = np.array([ts.timestamp() for ts, _ in pr])
    missing_pr = len(pr_keys) - len(pr)
    depth_mm = np.zeros((n,) + shp, np.float32)
    minutes_missing = np.zeros(n, int)
    for s, e in enumerate(ends):
        for m in range(step_min):
            tm = (e - step + timedelta(minutes=m)).timestamp()
            j = np.searchsorted(pr_ts, tm, side="right") - 1
            if j < 0 or tm - pr_ts[j] > 360:
                minutes_missing[s] += 1
                continue
            r = pr[j][1]
            depth_mm[s] += np.nan_to_num(np.clip(r, 0, None)) / 60.0

    # --- Pass 2 hourly scaling
    p2 = {ts: data[k][1] for ts, k in p2_keys if k in data}
    hours = []
    n_clipped = n_spread = 0
    end_ts = np.array([e.timestamp() for e in ends])
    hour_end = sorted({(e - timedelta(seconds=1)).replace(minute=0, second=0) + timedelta(hours=1) for e in ends})
    for he in hour_end:
        sel = np.where((end_ts > (he - timedelta(hours=1)).timestamp()) & (end_ts <= he.timestamp()))[0]
        if len(sel) == 0:
            continue
        pr_sum = depth_mm[sel].sum(0)
        tgt = p2.get(he)
        rec = {"hour_end_utc": he.isoformat(), "steps": int(len(sel)), "pass2": tgt is not None}
        if tgt is None:
            hours.append(rec)
            continue
        tgt = np.nan_to_num(np.clip(tgt, 0, None))
        with np.errstate(divide="ignore", invalid="ignore"):
            fac = np.where(pr_sum > 0, tgt / pr_sum, np.nan)
        clip = np.isfinite(fac) & ((fac < 0.1) | (fac > 10))
        n_clipped += int(clip.sum())
        fac = np.clip(fac, 0.1, 10)
        spread = (pr_sum <= 0) & (tgt > 0)
        n_spread += int(spread.sum())
        for s in sel:
            v = depth_mm[s] * np.nan_to_num(fac, nan=0.0)
            v[spread] = tgt[spread] / len(sel)
            v[(tgt <= 0)] = 0.0
            depth_mm[s] = v
        rec["pr_sum_mm"] = pr_sum
        rec["pass2_mm"] = tgt
        hours.append(rec)

    depth_in = depth_mm / MM_PER_IN
    inten = depth_in * (60.0 / step_min)

    # --- reflectivity & snow flag frames
    dbz = np.full((n,) + shp, np.nan, np.float32)
    snow = np.zeros((n,) + shp, bool)
    for s in range(n):
        k = dbz_pick[s]
        if k and k in data:
            dbz[s] = data[k][1]
        k = flag_pick[s]
        if k and k in data:
            snow[s] = data[k][1] == mrms.SNOW_FLAG

    qa = {
        "precip_rate_scans_listed": len(pr_keys), "precip_rate_scans_missing": int(missing_pr),
        "pass2_hours_expected": len(hour_end), "pass2_hours_found": len(p2),
        "steps_with_missing_minutes": int((minutes_missing > 0).sum()),
        "reflectivity_frames_missing": int(sum(1 for k in dbz_pick if not k or k not in data)),
        "cells_factor_clipped": n_clipped, "cells_even_spread": n_spread,
    }
    return {"grid": grid, "ends": ends, "step_min": step_min, "depth_in": depth_in, "inten": inten,
            "dbz": dbz, "snow": snow, "hours": hours, "qa": qa, "minutes_missing": minutes_missing}


# ---------------------------------------------------------------- basin stats
DURS_MIN = [5, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 4320]
THRESH = [0.1, 0.25, 0.5, 1.0, 2.0]


def basin_stats(R: dict, cells: dict):
    n = len(R["ends"])
    sm = R["step_min"]
    flat_d = R["depth_in"].reshape(n, -1)[:, cells["idx"]]
    flat_i = R["inten"].reshape(n, -1)[:, cells["idx"]]
    a = cells["area_sqmi"]
    A = a.sum()
    mean_d = flat_d @ a / A
    mean_i = flat_i @ a / A
    max_i = flat_i.max(1)
    wet = (flat_i > 0.01) @ a
    above = {t: (flat_i >= t) @ a for t in THRESH}
    snow = R["snow"].reshape(n, -1)[:, cells["idx"]] @ a / A * 100
    dbz = R["dbz"].reshape(n, -1)[:, cells["idx"]]
    with np.errstate(all="ignore"):
        dbz_max = np.nanmax(np.where(dbz >= 5, dbz, np.nan), 1)
    cum = np.cumsum(mean_d)
    cell_tot = flat_d.sum(0)

    def best(series, k):
        if k > len(series):
            return None
        c = np.convolve(series, np.ones(k), "valid")
        j = int(np.argmax(c))
        return float(c[j]), j + k - 1

    durs = {}
    for dm in DURS_MIN:
        k = dm // sm
        b = best(mean_d, k)
        if b is None:
            continue
        depth, j = b
        worst = None
        if dm <= 60:
            cc = np.array([np.convolve(flat_d[:, c], np.ones(k), "valid").max() for c in range(flat_d.shape[1])])
            worst = float(cc.max())
        durs[str(dm)] = {"depth_in": round(depth, 3), "intensity_inhr": round(depth / (dm / 60), 3),
                         "end_utc": R["ends"][j].isoformat(),
                         "worst_cell_depth_in": None if worst is None else round(worst, 3),
                         "worst_cell_inhr": None if worst is None else round(worst / (dm / 60), 3)}

    centroid = _storm_track(R, cells)
    i_peak = int(np.argmax(mean_i))
    summary = {
        "area_sqmi": round(float(A), 2), "n_cells": int(len(a)),
        "total_in": round(float(cum[-1]), 2), "max_cell_total_in": round(float(cell_tot.max()), 2),
        "peak_5min_inhr": round(float(mean_i[i_peak]), 3), "peak_5min_utc": R["ends"][i_peak].isoformat(),
        "worst_cell_5min_inhr": round(float(max_i.max()), 3),
        "max_area_ge_0_5_sqmi": round(float(above[0.5].max()), 2),
        "steps_ge_0_5": int((above[0.5] > 0).sum()),
        "any_cell_ge_1": bool((max_i >= 1).any()),
        "snow_steps": int((snow > 0).sum()), "snow_max_pct": round(float(snow.max()), 1),
        "durations": durs,
    }
    if summary["snow_steps"]:
        s_idx = np.where(snow > 0)[0]
        summary["snow_first_utc"] = R["ends"][s_idx[0]].isoformat()
        summary["snow_last_utc"] = R["ends"][s_idx[-1]].isoformat()
    # hourly bias (Pass 2 / PrecipRate), depth weighted over basin
    hb = []
    for h in R["hours"]:
        if not h.get("pass2"):
            hb.append({"hour_end_utc": h["hour_end_utc"], "pass2_in": None, "factor": None})
            continue
        p2 = h["pass2_mm"].reshape(-1)[cells["idx"]] @ a / A / MM_PER_IN
        prs = h["pr_sum_mm"].reshape(-1)[cells["idx"]] @ a / A / MM_PER_IN
        hb.append({"hour_end_utc": h["hour_end_utc"], "pass2_in": round(float(p2), 3),
                   "precip_rate_in": round(float(prs), 3),
                   "factor": round(float(p2 / prs), 3) if prs > 0.001 else None})
    tot_p2 = sum(h["pass2_in"] or 0 for h in hb)
    tot_pr = sum(h.get("precip_rate_in") or 0 for h in hb if (h["pass2_in"] or 0) > 0.02)
    summary["pass2_over_precip_rate"] = round(tot_p2 / tot_pr, 2) if tot_pr > 0 else None
    fs = [h["factor"] for h in hb if h["factor"] and (h["pass2_in"] or 0) > 0.02]
    summary["factor_range"] = [round(min(fs), 2), round(max(fs), 2)] if fs else None

    ts = {
        "mean_inhr": np.round(mean_i, 4).tolist(), "max_inhr": np.round(max_i, 4).tolist(),
        "cum_in": np.round(cum, 4).tolist(), "wet_sqmi": np.round(wet, 3).tolist(),
        "above_sqmi": {str(t): np.round(v, 3).tolist() for t, v in above.items()},
        "snow_pct": np.round(snow, 1).tolist(),
        "dbz_max": [None if not np.isfinite(v) else round(float(v), 1) for v in dbz_max],
        "storm": centroid,
    }
    cells_out = {"idx": cells["idx"].tolist(), "frac": np.round(cells["frac"], 4).tolist(),
                 "area_ac": np.round(cells["area_ac"], 1).tolist(),
                 "total_in": np.round(cell_tot, 3).tolist()}
    return summary, ts, hb, cells_out, (flat_d, flat_i, dbz)


def _storm_track(R, cells, radius_km=30.0, thresh=0.1):
    """Intensity-weighted centroid of rain >= thresh within radius of the basin centroid,
    and its 15-min speed (mph) and heading (deg, direction of travel)."""
    g = R["grid"]
    lat, lon = np.meshgrid(g.lats(), g.lons(), indexing="ij")
    c = cells["polygon"].centroid
    dy = (lat - c.y) * 111.0
    dx = (lon - c.x) * 111.0 * np.cos(np.radians(c.y))
    near = (dx ** 2 + dy ** 2) <= radius_km ** 2
    out = []
    for s in range(len(R["ends"])):
        w = np.where(near & (R["inten"][s] >= thresh), R["inten"][s], 0)
        W = w.sum()
        out.append(None if W <= 0 else [float((w * lat).sum() / W), float((w * lon).sum() / W)])
    k = 15 // R["step_min"]
    track = []
    for s in range(len(out)):
        if out[s] is None or s < k or out[s - k] is None:
            track.append(None if out[s] is None else [round(out[s][0], 4), round(out[s][1], 4), None, None])
            continue
        (la1, lo1), (la0, lo0) = out[s], out[s - k]
        dyk = (la1 - la0) * 111.0
        dxk = (lo1 - lo0) * 111.0 * np.cos(np.radians(la1))
        dist = np.hypot(dxk, dyk)
        spd = dist / 0.25 * 0.621371
        hdg = (np.degrees(np.arctan2(dxk, dyk)) + 360) % 360
        track.append([round(la1, 4), round(lo1, 4), round(float(spd), 1), round(float(hdg))])
    return track
