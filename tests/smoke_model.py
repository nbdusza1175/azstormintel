"""Offline smoke test of azstorm.model.

Terrain, land cover, soils and Atlas 14 are replaced with synthetic stand-ins so the
code path can be exercised without those services; MRMS radar is real. Results are
NOT meaningful - this only checks that the pipeline runs end to end.

  python tests/smoke_model.py mountain_dell 2026-11-01T05:00 2026-11-01T11:00
"""
import json
import os
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from azstorm import model as M  # noqa: E402


def fake_dem(grid, bcfg, ctx):
    x, y = M.grid_xy = grid.centers()
    x = x.reshape(grid.ny, grid.nx)
    y = y.reshape(grid.ny, grid.nx)
    ox, oy = ctx["_outlet_xy"]
    d = np.hypot(x - ox, y - oy)
    return 2100 + 0.02 * d + 5 * np.sin(x / 400.0) * np.cos(y / 500.0)


def fake_lc(grid, bcfg, ctx):
    cls = np.full((grid.ny, grid.nx), 42)
    cls[: grid.ny // 5] = 22
    imp = np.where(cls == 22, 0.35, 0.0)
    return cls, imp, "synthetic"


def fake_hsg(grid, bcfg, ctx):
    return np.full((grid.ny, grid.nx), 3, np.int8), "synthetic"


def fake_atlas(lat, lon, ctx):
    T = M.PFDS_T
    D = M.PFDS_DUR_HR
    return np.outer(0.9 * D ** 0.3, 1 + 0.35 * np.log(T))


if __name__ == "__main__":
    key, t0, t1 = sys.argv[1], sys.argv[2], sys.argv[3]
    cfg_path = os.path.join(ROOT, "config", "model.json")
    cfg = json.load(open(cfg_path))
    cfg["events"]["smoke"] = {"label": "smoke", "start_local": t0, "end_local": t1, "tz": "America/Phoenix"}
    tmp = tempfile.mkdtemp()
    p = os.path.join(ROOT, "config", "_smoke.json")
    json.dump(cfg, open(p, "w"))
    M.get_dem = lambda grid, b, ctx: fake_dem(grid, b, ctx)
    M.get_landcover, M.get_hsg, M.get_atlas14 = fake_lc, fake_hsg, fake_atlas
    orig = M.run_basin

    def wrapped(k, cfg_, ev, outroot, ctx):
        bc = cfg_["basins"][k]
        tr = M.transformer("EPSG:4326", "EPSG:26912")
        ctx["_outlet_xy"] = tr.transform(bc["outlet"][1], bc["outlet"][0])
        return orig(k, cfg_, ev, outroot, ctx)

    M.run_basin = wrapped
    try:
        out = M.run_event([key], "smoke", p, os.path.join(tmp, "out"), cache=os.path.join(ROOT, ".cache", "model"))
    finally:
        os.remove(p)
    R = out[key]
    print("OK", R["flood"]["headline"], "| design", R.get("design_check", {}) and round(R["design_check"]["peak_cfs"]))
    print(os.listdir(os.path.join(tmp, "out", key)))
