"""Build one event (or the live window) for the site.

  python scripts/run_event.py --event 2026-10-30            # an event from config/basins.json
  python scripts/run_event.py --live --hours 72             # rolling window ending now

Writes site/data/<events/ID | live>/: meta.json, dbz.bin, int.bin, logs/*.csv.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from azstorm import frequency, radar  # noqa: E402

MST = ZoneInfo("America/Phoenix")


def enc_dbz(a):
    a = np.nan_to_num(a, nan=0.0)
    return np.where(a >= 5, np.clip(np.round(a * 3), 1, 255), 0).astype(np.uint8)


def enc_int(a):
    """Display code: i = 10*(q/255)^2 in/hr (exact values are in the logs)."""
    return np.clip(np.round(255 * np.sqrt(np.clip(a, 0, 10) / 10)), 0, 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--start-mst")
    ap.add_argument("--end-mst")
    ap.add_argument("--cache", default=os.path.join(ROOT, ".cache", "mrms"))
    args = ap.parse_args()

    cfg = json.load(open(os.path.join(ROOT, "config", "basins.json")))
    if args.live:
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        end -= timedelta(minutes=end.minute % 5 + 10)  # allow MRMS latency
        start = end - timedelta(hours=args.hours)
        ev = {"id": "live", "name": f"Last {args.hours} hours", "live": True}
        out_dir = os.path.join(ROOT, "site", "data", "live")
    else:
        ev = next((e for e in cfg["events"] if e["id"] == args.event), None)
        if ev is None:
            ev = {"id": args.event, "name": args.event, "start_mst": args.start_mst, "end_mst": args.end_mst}
        start = datetime.fromisoformat(ev["start_mst"]).replace(tzinfo=MST).astimezone(timezone.utc)
        end = datetime.fromisoformat(ev["end_mst"]).replace(tzinfo=MST).astimezone(timezone.utc)
        out_dir = os.path.join(ROOT, "site", "data", "events", ev["id"])
    os.makedirs(os.path.join(out_dir, "logs"), exist_ok=True)
    os.makedirs(args.cache, exist_ok=True)

    t0 = time.time()
    R = radar.build(start, end, cfg["region_bbox"], cache_dir=args.cache)
    print(f"radar built in {time.time() - t0:.0f}s; QA {R['qa']}")
    g, n = R["grid"], len(R["ends"])

    basins_out = []
    log_rows = []
    for b in cfg["basins"]:
        gj = json.load(open(os.path.join(ROOT, b["polygon"])))
        cells = radar.basin_cells(gj, g)
        summary, ts, hb, cells_out, (fd, fi, fdbz) = radar.basin_stats(R, cells)
        freq = []
        for f in b.get("frequency", []):
            if f["basis"] == "streamstats":
                p = os.path.join(ROOT, f["file"])
                if not os.path.exists(p):
                    freq.append({**f, "missing": True})
                    continue
                c = frequency.parse_streamstats(p)
                ct = frequency.transfer(c, cells["total_sqmi"], f.get("transfer_exponent", 0.6))
                freq.append({**f, "region": c["region"], "source_area_sqmi": c["area_sqmi"],
                             "target_area_sqmi": round(cells["total_sqmi"], 2), "factor": round(ct["factor"], 3),
                             "rows": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items()}
                                      for r in ct["rows"]],
                             "rows_published": c["rows"]})
            else:
                freq.append(f)
        basins_out.append({
            "id": b["id"], "name": b["name"], "outlet": b["outlet"], "polygon": gj,
            "rain_gauges": b.get("rain_gauges", []), "stage_gauges": b.get("stage_gauges", []),
            "channel": b.get("channel"), "notes": b.get("notes", []), "frequency": freq,
            "published_area_sqmi": b.get("published_area_sqmi"),
            "summary": summary, "ts": ts, "hourly": hb, "cells": cells_out, "model": None,
        })
        # cell log: rainy cells only
        lat, lon = g.lats(), g.lons()
        nx = g.shape[1]
        cum = np.cumsum(fd, 0)
        for s, e in enumerate(R["ends"]):
            for c in np.where(fd[s] > 0)[0]:
                ii, jj = divmod(int(cells["idx"][c]), nx)
                log_rows.append((b["id"], e.astimezone(MST).strftime("%Y-%m-%d %H:%M"), round(lat[ii], 3),
                                 round(lon[jj], 3), round(float(fi[s, c]), 4), round(float(fd[s, c]), 4),
                                 round(float(cum[s, c]), 4),
                                 "" if not np.isfinite(fdbz[s, c]) else round(float(fdbz[s, c]), 1),
                                 round(float(fd[s, c]) / 12 * cells["area_ac"][c], 3),
                                 round(float(cells["area_ac"][c]), 1), round(float(cells["frac"][c]) * 100, 1)))
        print(f"{b['name']}: {summary['area_sqmi']} sq mi, total {summary['total_in']} in, "
              f"peak 5-min {summary['peak_5min_inhr']} in/hr")

    with gzip.open(os.path.join(out_dir, "logs", "cell_log.csv.gz"), "wt") as f:
        f.write("basin,time_mst,lat,lon,intensity_inhr,depth_in,cum_depth_in,dbz,rain_volume_acft,"
                "cell_area_in_basin_ac,pct_cell_in_basin\n")
        for r in log_rows:
            f.write(",".join(str(x) for x in r) + "\n")
    with gzip.open(os.path.join(out_dir, "logs", "basin_timeseries.csv.gz"), "wt") as f:
        f.write("basin,time_mst,mean_inhr,max_cell_inhr,cum_in,wet_sqmi,"
                + ",".join(f"area_ge_{t}_sqmi" for t in radar.THRESH) + ",snow_pct,dbz_max\n")
        for b in basins_out:
            t = b["ts"]
            for s, e in enumerate(R["ends"]):
                f.write(",".join(str(x) for x in [b["id"], e.astimezone(MST).strftime("%Y-%m-%d %H:%M"),
                        t["mean_inhr"][s], t["max_inhr"][s], t["cum_in"][s], t["wet_sqmi"][s]]
                        + [t["above_sqmi"][str(th)][s] for th in radar.THRESH]
                        + [t["snow_pct"][s], "" if t["dbz_max"][s] is None else t["dbz_max"][s]]) + "\n")

    enc_dbz(R["dbz"]).tofile(os.path.join(out_dir, "dbz.bin"))
    enc_int(R["inten"]).tofile(os.path.join(out_dir, "int.bin"))
    meta = {
        "version": cfg["version"], "event": ev, "generated_utc": datetime.now(timezone.utc).isoformat(),
        "start_utc": start.isoformat(), "end_utc": end.isoformat(), "step_min": R["step_min"],
        "steps_utc": [e.isoformat() for e in R["ends"]],
        "grid": {"lat0": g.lat0, "lon0": g.lon0, "d": g.d, "ny": g.shape[0], "nx": g.shape[1]},
        "encoding": {"dbz": "dBZ = code/3 (0 = none, < 5 dBZ hidden)", "int": "in/hr = 10*(code/255)^2"},
        "qa": R["qa"], "basins": basins_out,
        "sources": ["NOAA MRMS PrecipRate (2-min), MultiSensor QPE Pass 2 01H, ReflectivityAtLowestAltitude, "
                    "PrecipFlag via AWS noaa-mrms-pds"],
    }
    json.dump(meta, open(os.path.join(out_dir, "meta.json"), "w"), separators=(",", ":"))
    # event index for the site
    idx_path = os.path.join(ROOT, "site", "data", "events.json")
    idx = json.load(open(idx_path)) if os.path.exists(idx_path) else {"events": []}
    entry = {"id": ev["id"], "name": ev["name"], "path": os.path.relpath(out_dir, os.path.join(ROOT, "site")),
             "start_utc": start.isoformat(), "end_utc": end.isoformat(), "live": bool(ev.get("live"))}
    idx["events"] = [e for e in idx["events"] if e["id"] != ev["id"]] + [entry]
    idx["events"].sort(key=lambda e: (not e["live"], e["start_utc"]), reverse=False)
    json.dump(idx, open(idx_path, "w"), indent=1)
    print(f"wrote {out_dir} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
