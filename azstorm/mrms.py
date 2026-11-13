"""NOAA MRMS access: list and read CONUS products from the public AWS bucket.

Grids are 0.01 deg. Every read is cropped to a bounding box, so callers get small
arrays plus the lat/lon of the first row/column (cell centres, north-up).
"""
from __future__ import annotations

import gzip
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

BUCKET = "https://noaa-mrms-pds.s3.amazonaws.com"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
TS_RE = re.compile(r"_(\d{8})-(\d{6})\.grib2\.gz$")

PRECIP_RATE = "PrecipRate_00.00"                    # mm/hr, every 2 min
PASS2_01H = "MultiSensor_QPE_01H_Pass2_00.00"       # mm, hour ending at stamp
REFLECTIVITY = "ReflectivityAtLowestAltitude_00.50"  # dBZ, every ~2 min
PRECIP_FLAG = "PrecipFlag_00.00"                    # category, every 2 min
SNOW_FLAG = 3

_session = requests.Session()


@dataclass
class Grid:
    """Cropped grid: values[i, j] is the cell centred at lat0 - i*d, lon0 + j*d."""
    lat0: float
    lon0: float
    d: float
    shape: tuple

    def lats(self):
        return self.lat0 - self.d * np.arange(self.shape[0])

    def lons(self):
        return self.lon0 + self.d * np.arange(self.shape[1])


def _get(url, tries=4, stream=False):
    for k in range(tries):
        try:
            r = _session.get(url, timeout=60)
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(1.5 * (k + 1))
    raise RuntimeError(f"MRMS request failed: {url}")


def list_product(product: str, start_utc: datetime, end_utc: datetime):
    """Return sorted [(timestamp_utc, key)] for files stamped within [start, end]."""
    out = []
    day = start_utc.date()
    while day <= end_utc.date():
        prefix = f"CONUS/{product}/{day:%Y%m%d}/"
        token = None
        while True:
            url = f"{BUCKET}/?list-type=2&prefix={prefix}&max-keys=1000"
            if token:
                url += "&continuation-token=" + requests.utils.quote(token, safe="")
            r = _get(url)
            root = ET.fromstring(r.content)
            for c in root.findall("s3:Contents", NS):
                key = c.find("s3:Key", NS).text
                m = TS_RE.search(key)
                if not m:
                    continue
                ts = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                if start_utc <= ts <= end_utc:
                    out.append((ts, key))
            if root.findtext("s3:IsTruncated", namespaces=NS) == "true":
                token = root.findtext("s3:NextContinuationToken", namespaces=NS)
            else:
                break
        day += timedelta(days=1)
    return sorted(out)


def read_cropped(key: str, bbox, cache_dir: str | None = None):
    """Download one MRMS GRIB2 file and crop it to bbox = (W, S, E, N).

    Returns (Grid, float32 array). Missing / no-coverage values (< -1) become NaN.
    """
    import pygrib  # imported lazily so the site build does not need it

    cache = None
    if cache_dir:
        cache = os.path.join(cache_dir, key.replace("/", "_") + ".npz")
        if os.path.exists(cache):
            z = np.load(cache)
            g = Grid(float(z["lat0"]), float(z["lon0"]), float(z["d"]), tuple(z["v"].shape))
            return g, z["v"]
    r = _get(f"{BUCKET}/{key}")
    if r is None:
        raise FileNotFoundError(key)
    raw = gzip.decompress(r.content)
    with tempfile.NamedTemporaryFile(suffix=".grib2") as tf:
        tf.write(raw)
        tf.flush()
        grbs = pygrib.open(tf.name)
        msg = grbs.message(1)
        la1 = msg["latitudeOfFirstGridPointInDegrees"]
        lo1 = msg["longitudeOfFirstGridPointInDegrees"]
        d = msg["iDirectionIncrementInDegrees"]
        vals = msg.values
        grbs.close()
    if lo1 > 180:
        lo1 -= 360
    vals = np.asarray(vals, dtype=np.float32)
    if hasattr(vals, "filled"):
        vals = vals.filled(np.nan)
    W, S, E, N = bbox
    i0 = int(round((la1 - N) / d)); i1 = int(round((la1 - S) / d)) + 1
    j0 = int(round((W - lo1) / d)); j1 = int(round((E - lo1) / d)) + 1
    sub = vals[i0:i1, j0:j1].copy()
    sub[sub < -1] = np.nan
    g = Grid(la1 - i0 * d, lo1 + j0 * d, d, sub.shape)
    if cache:
        np.savez_compressed(cache, v=sub, lat0=g.lat0, lon0=g.lon0, d=g.d)
    return g, sub


def read_many(keys, bbox, cache_dir=None, workers=8):
    """Read many keys in parallel. Returns {key: (Grid, array)}; failed keys are absent."""
    out = {}
    args = [(k, tuple(bbox), cache_dir) for k in keys]
    with ProcessPoolExecutor(workers) as ex:
        for k, res in zip(keys, ex.map(_job, args, chunksize=4)):
            if res is not None:
                out[k] = res
    return out


def _job(a):
    k, bbox, cache_dir = a
    try:
        return read_cropped(k, bbox, cache_dir)
    except Exception:  # noqa: BLE001 - a missing scan is logged by the caller
        return None
