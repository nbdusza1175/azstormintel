"""Delete runs/<id>/ folders older than limits.json runs_keep_days (by run.json finished_utc).

  python scripts/prune_runs.py <runs-dir>
"""
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

root = sys.argv[1]
lim_path = os.environ.get("LIMITS") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "limits.json")
keep = timedelta(days=json.load(open(lim_path)).get("runs_keep_days", 45))
now = datetime.now(timezone.utc)
for rid in sorted(os.listdir(root)):
    p = os.path.join(root, rid, "run.json")
    try:
        t = datetime.fromisoformat(json.load(open(p))["finished_utc"])
    except Exception:  # noqa: BLE001 - unfinished or unreadable: keep for a day after the folder appeared
        t = datetime.fromtimestamp(os.path.getmtime(os.path.join(root, rid)), timezone.utc)
    if now - t > keep:
        shutil.rmtree(os.path.join(root, rid))
        print("pruned", rid)
