"""NASA SMAP L4 soil moisture (SPL4SMGP, 9 km EASE-Grid 2.0, 3-hourly) for basin antecedent checks.

Used as a CHECK beside the model's ARC band, never to change it (regional calibration is offline).
Needs a NASA Earthdata login: env EARTHDATA_USER / EARTHDATA_PASS.

Granules are found with NASA CMR (no login), downloaded over HTTPS (Earthdata login redirect),
read with h5py, cropped to the basins, and deleted.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

CMR = "https://cmr.earthdata.nasa.gov/search"
SHORT_NAME = "SPL4SMGP"
URS = "urs.earthdata.nasa.gov"
VARS = {
    "surface_wetness": "sm_surface_wetness",      # 0-5 cm, fraction of saturation (0-1)
    "rootzone_wetness": "sm_rootzone_wetness",    # 0-100 cm, fraction of saturation (0-1)
    "profile_wetness": "sm_profile_wetness",      # surface to bedrock, fraction of saturation
    "rootzone_m3m3": "sm_rootzone",               # volumetric m3/m3
    "soil_temp_l1_K": "soil_temp_layer1",
    "snow_mass_kgm2": "snow_mass",
}


class EarthdataSession(requests.Session):
    """Keeps the Authorization header only for the Earthdata login host across redirects
    (NASA's documented pattern for DAAC HTTPS downloads)."""

    def __init__(self, user, password):
        super().__init__()
        self.auth = (user, password)
        self.headers["User-Agent"] = "azstormintel soil check"

    def rebuild_auth(self, prepared_request, response):
        headers = prepared_request.headers
        url = prepared_request.url
        if "Authorization" in headers:
            orig = requests.utils.urlparse(response.request.url).hostname
            redir = requests.utils.urlparse(url).hostname
            if orig != redir and redir != URS and orig != URS:
                del headers["Authorization"]


def _iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_version(log=print):
    r = requests.get(f"{CMR}/collections.json", params={"short_name": SHORT_NAME, "page_size": 50}, timeout=60)
    r.raise_for_status()
    vs = [e.get("version_id") for e in r.json()["feed"]["entry"]]
    vs = sorted({v for v in vs if v}, key=lambda v: int(re.sub(r"\D", "", v) or 0))
    log(f"SMAP {SHORT_NAME} versions in CMR: {vs}")
    return vs


def find_granule(t: datetime, versions):
    """Granule (url, t_start, t_end, version) whose 3-hour window contains t; newest version first."""
    for v in reversed(versions):
        r = requests.get(f"{CMR}/granules.json", params={
            "short_name": SHORT_NAME, "version": v, "temporal": f"{_iso(t)},{_iso(t)}", "page_size": 10}, timeout=60)
        r.raise_for_status()
        for e in r.json()["feed"]["entry"]:
            links = [l["href"] for l in e.get("links", []) if l.get("href", "").startswith("https") and l["href"].endswith(".h5")]
            if links:
                return links[0], e["time_start"], e["time_end"], v
    return None


class Reader:
    """Crops SMAP L4 granules to a lat/lon box and computes basin-weighted values."""

    def __init__(self, session, basins, pad_deg=0.15, log=print):
        self.s, self.basins, self.pad, self.log = session, basins, pad_deg, log
        self.idx = None   # (r0, r1, c0, c1)
        self.w = {}       # basin id -> weight array on the crop

    def _setup(self, f):
        lat, lon = f["cell_lat"][:], f["cell_lon"][:]
        W = min(b["bounds"][0] for b in self.basins) - self.pad
        S = min(b["bounds"][1] for b in self.basins) - self.pad
        E = max(b["bounds"][2] for b in self.basins) + self.pad
        N = max(b["bounds"][3] for b in self.basins) + self.pad
        m = (lat >= S) & (lat <= N) & (lon >= W) & (lon <= E)
        rows, cols = np.where(m)
        r0, r1, c0, c1 = rows.min(), rows.max() + 1, cols.min(), cols.max() + 1
        self.idx = (r0, r1, c0, c1)
        la, lo = lat[r0:r1, c0:c1], lon[r0:r1, c0:c1]
        from shapely.geometry import box
        hy = np.abs(np.gradient(la, axis=0)) / 2
        hx = np.abs(np.gradient(lo, axis=1)) / 2
        self.cells = []
        for b in self.basins:
            wt = np.zeros(la.shape)
            for i in range(la.shape[0]):
                for j in range(la.shape[1]):
                    c = box(lo[i, j] - hx[i, j], la[i, j] - hy[i, j], lo[i, j] + hx[i, j], la[i, j] + hy[i, j])
                    if c.intersects(b["poly"]):
                        wt[i, j] = c.intersection(b["poly"]).area
            self.w[b["id"]] = wt / wt.sum() if wt.sum() > 0 else wt
            n = int((wt > 0).sum())
            self.log(f"SMAP cells touching {b['id']}: {n} (largest share {wt.max() / max(wt.sum(), 1e-12):.0%})")
            self.cells.append({"basin": b["id"], "cells": [
                {"lat": round(float(la[i, j]), 4), "lon": round(float(lo[i, j]), 4), "weight": round(float(self.w[b['id']][i, j]), 3)}
                for i, j in zip(*np.where(wt > 0))]})

    def read(self, url):
        import h5py
        with tempfile.NamedTemporaryFile(suffix=".h5") as tf:
            with self.s.get(url, stream=True, timeout=600) as r:
                if r.status_code != 200:
                    raise IOError(f"HTTP {r.status_code} for {url} ({r.url[:120]})")
                for ch in r.iter_content(1 << 22):
                    tf.write(ch)
            tf.flush()
            with h5py.File(tf.name, "r") as f:
                if self.idx is None:
                    self._setup(f)
                r0, r1, c0, c1 = self.idx
                g = f["Geophysical_Data"]
                out = {}
                for k, v in VARS.items():
                    if v not in g:
                        continue
                    a = g[v][r0:r1, c0:c1].astype(float)
                    a[a <= -9990] = np.nan
                    out[k] = a
        res = {}
        for b in self.basins:
            w = self.w[b["id"]]
            res[b["id"]] = {k: (None if not np.isfinite(np.nansum(a * w)) or np.all(np.isnan(a[w > 0]))
                                else round(float(np.nansum(a * w) / np.sum(w[np.isfinite(a)])), 4))
                            for k, a in out.items()}
        return res


def basin_check(basins, t_event: datetime, user, password, prior_days=30, step_days=3, log=print):
    """SMAP L4 at event start, a prior series, and same-date values in earlier years."""
    s = EarthdataSession(user, password)
    versions = latest_version(log)
    rd = Reader(s, basins, log=log)
    out = {"product": f"NASA SMAP L4 {SHORT_NAME} (9 km, 3-hourly)", "event_start_utc": t_event.isoformat(),
           "basins": {b["id"]: {"series": [], "climatology": []} for b in basins}, "errors": []}

    def grab(t, kind):
        g = find_granule(t, versions)
        if not g:
            out["errors"].append(f"no granule for {t.isoformat()}")
            return None
        url, t0, t1, v = g
        try:
            vals = rd.read(url)
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{t.isoformat()}: {e}")
            log(f"  SMAP read failed {t.isoformat()}: {e}")
            return None
        log(f"  SMAP {kind} {t0} v{v}: " + ", ".join(f"{k} rz {d.get('rootzone_wetness')}" for k, d in vals.items()))
        out["version"] = v
        return t0, vals

    # just before the event (granule containing event start - 1 h)
    r = grab(t_event - timedelta(hours=1), "event start")
    if r:
        for k, d in r[1].items():
            out["basins"][k]["at_start"] = {"granule_start_utc": r[0], **d}
    # prior series
    for dd in range(prior_days, 0, -step_days):
        r = grab(t_event - timedelta(days=dd), "prior")
        if r:
            for k, d in r[1].items():
                out["basins"][k]["series"].append({"t_utc": r[0], **d})
    # same date and hour, earlier years (SMAP L4 starts 2015-03-31)
    for y in range(2015, t_event.year):
        try:
            ty = (t_event - timedelta(hours=1)).replace(year=y)
        except ValueError:
            continue
        r = grab(ty, f"climatology {y}")
        if r:
            for k, d in r[1].items():
                out["basins"][k]["climatology"].append({"year": y, "t_utc": r[0], **d})
    # percentile of event-start root-zone wetness among same-date values
    for k, b in out["basins"].items():
        x = (b.get("at_start") or {}).get("rootzone_wetness")
        clim = [c["rootzone_wetness"] for c in b["climatology"] if c.get("rootzone_wetness") is not None]
        if x is not None and len(clim) >= 3:
            pct = 100.0 * (sum(c < x for c in clim) + 0.5 * sum(c == x for c in clim)) / len(clim)
            b["rootzone_percentile"] = round(pct)
            b["clim_min_median_max"] = [round(min(clim), 3), round(float(np.median(clim)), 3), round(max(clim), 3)]
            b["relative"] = "drier than usual" if pct < 33 else ("wetter than usual" if pct > 67 else "near usual")
    out["cells"] = getattr(rd, "cells", [])
    return out
