"""On-demand analysis for a user-chosen window (called by .github/workflows/analyze.yml).

  python scripts/run_request.py --run-id r1a2b3c4d5e6 --start-mst 2026-10-30T00:00 \
      --end-mst 2026-11-03T00:00 --basins fort_valley mountain_dell --stage all

Stages (run one at a time from the workflow so progress shows step by step):
  validate  check the request against the site limits, write request.json
  radar     5-min MRMS rainfall + basin statistics      -> meta.json, frames.json, logs/
  model     ModClark flood model (3DEP, NLCD for the event year, SSURGO, Atlas 14, MRMS) -> model/
  soil      NASA SMAP L4 soil-moisture check (needs EARTHDATA_USER / EARTHDATA_PASS)    -> soil.json
  finalize  run.json (stage results, data sources) - the file the site waits for

Everything goes to out/runs/<run_id>/ (the workflow copies that folder to the `runs` branch).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MST = ZoneInfo("America/Phoenix")
LIMITS_PATH = os.path.join(ROOT, "config", "limits.json")


def limits():
    return json.load(open(LIMITS_PATH))


def min_start(now=None):
    """Earliest allowed start: Jan 1 of (current year - back_years), MST."""
    now = now or datetime.now(MST)
    return datetime(now.year - limits()["back_years"], 1, 1, tzinfo=MST)


def validate(start_mst: str, end_mst: str, basins, now=None):
    """Return (start, end) as aware MST datetimes or raise ValueError with a user-facing message."""
    L = limits()
    now = (now or datetime.now(timezone.utc)).astimezone(MST)
    try:
        s = datetime.fromisoformat(start_mst).replace(tzinfo=MST)
        e = datetime.fromisoformat(end_mst).replace(tzinfo=MST)
    except ValueError:
        raise ValueError("Start and end must be ISO date-times, e.g. 2026-10-30T00:00")
    if s.minute % 5 or e.minute % 5 or s.second or e.second:
        raise ValueError("Start and end must fall on 5-minute marks")
    if e <= s:
        raise ValueError("End must be after start")
    if e - s < timedelta(hours=L["min_hours"]):
        raise ValueError(f"The window must be at least {L['min_hours']} hour(s)")
    if e - s > timedelta(days=L["max_days"]):
        raise ValueError(f"The window can be at most {L['max_days']} days")
    if s < min_start(now):
        raise ValueError(f"Start must be on or after {min_start(now):%b %d, %Y} (radar archive limit)")
    if e > now - timedelta(minutes=L["latency_min"]):
        raise ValueError(f"End must be at least {L['latency_min']} minutes before now (radar latency)")
    known = {b["id"] for b in json.load(open(os.path.join(ROOT, "config", "basins.json")))["basins"]}
    bad = [b for b in basins if b not in known]
    if not basins or bad:
        raise ValueError(f"Unknown basin(s): {', '.join(bad) or '(none chosen)'}")
    return s, e


def write_json(path, obj):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, path)


def run(cmd, env=None):
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT, env={**os.environ, **(env or {})})


def stage_state(out, name, ok, t0, note=""):
    p = os.path.join(out, "stages.json")
    st = json.load(open(p)) if os.path.exists(p) else {}
    st[name] = {"ok": ok, "seconds": round(time.time() - t0), "note": note}
    write_json(p, st)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--start-mst", required=True)
    ap.add_argument("--end-mst", required=True)
    ap.add_argument("--basins", nargs="+", required=True)
    ap.add_argument("--stage", default="all", choices=["validate", "radar", "model", "soil", "finalize", "all"])
    ap.add_argument("--out-root", default=os.path.join(ROOT, "out", "runs"))
    a = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{5,47}", a.run_id):
        sys.exit("bad run id")
    basins = [b for part in a.basins for b in re.split(r"[,\s]+", part) if b]
    out = os.path.join(a.out_root, a.run_id)
    os.makedirs(out, exist_ok=True)
    stages = ["validate", "radar", "model", "soil", "finalize"] if a.stage == "all" else [a.stage]
    try:
        s, e = validate(a.start_mst, a.end_mst, basins)
    except ValueError as ex:
        write_json(os.path.join(out, "run.json"), {"run_id": a.run_id, "ok": False, "error": str(ex),
                                                   "finished_utc": datetime.now(timezone.utc).isoformat()})
        print(f"invalid request: {ex}")
        sys.exit(2)
    if s.date() == e.date():
        name = f"{s:%b %-d, %Y %H:%M}–{e:%H:%M} MST"
    elif s.year == e.year:
        name = f"{s:%b %-d %H:%M} – {e:%b %-d, %Y %H:%M} MST"
    else:
        name = f"{s:%b %-d, %Y %H:%M} – {e:%b %-d, %Y %H:%M} MST"
    rc = 0
    for st in stages:
        t0 = time.time()
        if st == "validate":
            write_json(os.path.join(out, "request.json"), {
                "run_id": a.run_id, "start_mst": s.strftime("%Y-%m-%dT%H:%M"), "end_mst": e.strftime("%Y-%m-%dT%H:%M"),
                "basins": basins, "requested_utc": datetime.now(timezone.utc).isoformat(),
                "pipeline_version": json.load(open(os.path.join(ROOT, "config", "model.json")))["version"]})
            stage_state(out, st, True, t0)
        elif st == "radar":
            r = run([sys.executable, "scripts/run_event.py", "--event", a.run_id, "--name", name,
                     "--start-mst", s.strftime("%Y-%m-%dT%H:%M"), "--end-mst", e.strftime("%Y-%m-%dT%H:%M"),
                     "--out-dir", out, "--no-index", "--basins", *basins])
            stage_state(out, st, r == 0, t0)
            if r:
                rc = r
                break  # nothing to show without radar
        elif st == "model":
            ms = s.replace(minute=0)
            me = e if e.minute == 0 else e.replace(minute=0) + timedelta(hours=1)
            r = run([sys.executable, "scripts/run_model.py", "--event", a.run_id, "--basins", *basins,
                     "--start-local", ms.strftime("%Y-%m-%dT%H:%M"), "--end-local", me.strftime("%Y-%m-%dT%H:%M"),
                     "--label", name, "--out", os.path.join(out, "model")])
            # drop the large rasters; the site uses the PNG layers and summary.json
            for dp, _, fs in os.walk(os.path.join(out, "model")):
                for f in fs:
                    if f.endswith(".tif"):
                        os.remove(os.path.join(dp, f))
            stage_state(out, st, r == 0, t0, "" if r == 0 else "flood model failed; radar results are still shown")
        elif st == "soil":
            if not os.environ.get("EARTHDATA_USER"):
                stage_state(out, st, False, t0, "skipped: no Earthdata login")
                continue
            r = run([sys.executable, "scripts/run_soil.py", "--start-mst", s.strftime("%Y-%m-%dT%H:%M"),
                     "--out-dir", out, "--basins", *basins])
            ok = r == 0 and os.path.exists(os.path.join(out, "soil.json"))
            stage_state(out, st, ok, t0, "" if ok else "SMAP check unavailable")
        elif st == "finalize":
            req = json.load(open(os.path.join(out, "request.json")))
            sts = json.load(open(os.path.join(out, "stages.json"))) if os.path.exists(os.path.join(out, "stages.json")) else {}
            meta_ok = os.path.exists(os.path.join(out, "meta.json"))
            sources = {}
            mi = os.path.join(out, "model", "index.json")
            if os.path.exists(mi):
                for k, v in json.load(open(mi)).get("basins", {}).items():
                    if v.get("ok"):
                        sm = json.load(open(os.path.join(out, "model", v["path"])))
                        sources[k] = sm.get("sources") or sm.get("data_sources")
            write_json(os.path.join(out, "run.json"), {
                **req, "name": name, "ok": meta_ok, "stages": sts, "sources": sources,
                "finished_utc": datetime.now(timezone.utc).isoformat(),
                "start_utc": s.astimezone(timezone.utc).isoformat(), "end_utc": e.astimezone(timezone.utc).isoformat()})
            if not meta_ok:
                rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
