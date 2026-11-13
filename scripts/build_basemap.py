"""Download USGS basemap tiles for the region into site/tiles/ so the site (and the
claude.ai preview, which blocks remote images) can show a map without outside requests.

USGS The National Map basemaps are public domain. Zoom 9-14 over config region_bbox.
  python scripts/build_basemap.py [--max-zoom 14]
"""
import argparse
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAYERS = {
    "topo": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
    "imagery": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}",
}


def tile_xy(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-zoom", type=int, default=9)
    ap.add_argument("--max-zoom", type=int, default=14)
    ap.add_argument("--layers", nargs="*", default=list(LAYERS))
    a = ap.parse_args()
    W, S, E, N = json.load(open(os.path.join(ROOT, "config", "basins.json")))["region_bbox"]
    s = requests.Session()
    s.headers["User-Agent"] = "azstormintel basemap builder (public-domain USGS tiles)"
    jobs = []
    for lyr in a.layers:
        for z in range(a.min_zoom, a.max_zoom + 1):
            x0, y0 = tile_xy(W, N, z)
            x1, y1 = tile_xy(E, S, z)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    jobs.append((lyr, z, x, y))

    def get(j):
        lyr, z, x, y = j
        p = os.path.join(ROOT, "site", "tiles", lyr, str(z), str(x), f"{y}.jpg")
        if os.path.exists(p):
            return 0
        os.makedirs(os.path.dirname(p), exist_ok=True)
        for k in range(4):
            try:
                r = s.get(LAYERS[lyr].format(z=z, x=x, y=y), timeout=60)
                if r.status_code == 200 and r.content:
                    open(p, "wb").write(r.content)
                    return 1
            except requests.RequestException:
                pass
            time.sleep(2 * (k + 1))
        print("failed", j)
        return 0

    with ThreadPoolExecutor(8) as ex:
        n = sum(ex.map(get, jobs))
    meta = {"layers": a.layers, "min_zoom": a.min_zoom, "max_zoom": a.max_zoom, "bbox": [W, S, E, N],
            "attribution": "USGS The National Map"}
    json.dump(meta, open(os.path.join(ROOT, "site", "tiles", "tiles.json"), "w"), indent=1)
    print(f"{len(jobs)} tiles in set, {n} downloaded")


if __name__ == "__main__":
    main()
