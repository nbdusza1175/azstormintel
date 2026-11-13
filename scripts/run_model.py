"""Run the ModClark flood model (azstorm.model, v3.3) for an event and write site outputs.

  python scripts/run_model.py --event oct30_nov2_2026            # all basins
  python scripts/run_model.py --event oct30_nov2_2026 --basins mountain_dell

Needs internet access to USGS 3DEP, MRLC NLCD, USDA Soil Data Access, NOAA Atlas 14 and
NOAA MRMS (runs in GitHub Actions). Gauge exports go in data/gauges/ (optional checks).
Output: site/data/events/<site_event>/model/<basin>/summary.json (+ figures, layers)
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from azstorm import model as M  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--event", required=True)
ap.add_argument("--basins", nargs="*")
ap.add_argument("--preflight", action="store_true")
a = ap.parse_args()

cfg_path = os.path.join(ROOT, "config", "model.json")
if a.preflight:
    M.preflight(cfg_path)
cfg = json.load(open(cfg_path))
ev = cfg["events"][a.event]
site_ev = ev.get("site_event", a.event)
out = os.path.join(ROOT, "site", "data", "events", site_ev, "model")
os.makedirs(out, exist_ok=True)
t0 = time.time()
res = M.run_event(a.basins or "all", a.event, cfg_path, out, cache=os.path.join(ROOT, ".cache", "model"))
idx_p = os.path.join(out, "index.json")
idx = json.load(open(idx_p)) if os.path.exists(idx_p) else {"basins": {}}
for k in (a.basins or cfg["basins"].keys()):
    ok = k in res
    idx["basins"][k] = {"ok": ok, "path": f"{k}/summary.json" if ok else None,
                        "run_utc": datetime.now(timezone.utc).isoformat(), "version": M.VERSION}
idx.update(model_event=a.event, label=ev.get("label"), version=M.VERSION)
json.dump(idx, open(idx_p, "w"), indent=1)
print(f"model done in {time.time() - t0:.0f}s: {', '.join(res)}")
sys.exit(0 if res else 1)
