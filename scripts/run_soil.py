"""SMAP L4 soil-moisture check for an event (shown beside the model's ARC band; never changes it).

  EARTHDATA_USER=... EARTHDATA_PASS=... python scripts/run_soil.py --event 2026-10-30

Writes site/data/events/<id>/soil.json and soil_log.txt.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from shapely.geometry import shape
from shapely.ops import unary_union

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from azstorm import smap  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--event", required=True)
a = ap.parse_args()
cfg = json.load(open(os.path.join(ROOT, "config", "basins.json")))
mcfg = json.load(open(os.path.join(ROOT, "config", "model.json")))
ev = next(e for e in cfg["events"] if e["id"] == a.event)
# the model event that maps to this site event sets the antecedent time (rain start)
mev = next((m for m in mcfg["events"].values() if m.get("site_event") == a.event), None)
start_local = (mev or {}).get("start_local", ev["start_mst"])
t_event = datetime.fromisoformat(start_local).replace(tzinfo=ZoneInfo("America/Phoenix")).astimezone(timezone.utc)

basins = []
for b in cfg["basins"]:
    gj = json.load(open(os.path.join(ROOT, b["polygon"])))
    poly = unary_union([shape(f["geometry"]) for f in gj["features"] if f["geometry"]["type"] in ("Polygon", "MultiPolygon")])
    basins.append({"id": b["id"], "poly": poly, "bounds": poly.bounds})

out_dir = os.path.join(ROOT, "site", "data", "events", a.event)
lines = []


def log(m):
    print(m, flush=True)
    lines.append(str(m))


user, pw = os.environ.get("EARTHDATA_USER"), os.environ.get("EARTHDATA_PASS")
if not user or not pw:
    log("EARTHDATA_USER / EARTHDATA_PASS not set - skipped")
    open(os.path.join(out_dir, "soil_log.txt"), "w").write("\n".join(lines))
    sys.exit(0)
try:
    res = smap.basin_check(basins, t_event, user, pw, log=log)
    res["note"] = ("Check only: shown beside the model's ARC I-III band; it does not change the headline. "
                   "Percentile is event-start root-zone wetness against the same date and hour in earlier SMAP years.")
    json.dump(res, open(os.path.join(out_dir, "soil.json"), "w"), indent=1)
    log(f"wrote soil.json; errors: {len(res['errors'])}")
except Exception as e:  # noqa: BLE001
    import traceback
    log("FAILED: " + repr(e))
    log(traceback.format_exc())
open(os.path.join(out_dir, "soil_log.txt"), "w").write("\n".join(lines))
