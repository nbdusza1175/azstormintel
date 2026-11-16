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


def validate(start_mst: str, end_mst: str, basins, now=None, basins_config=None):
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
    known = {b["id"] for b in json.load(open(basins_config or os.path.join(ROOT, "config", "basins.json")))["basins"]}
    bad = [b for b in basins if b not in known]
    if not basins or bad:
        raise ValueError(f"Unknown basin(s): {', '.join(bad) or '(none chosen)'}")
    return s, e


CUSTOM_ID = "upload"


def prepare_custom_basin(run_id: str, spec: dict):
    """Turn an uploaded basin {name, geometry, outlet [lon, lat] | null, streamstats_csv} into
    request-specific config files. Returns (basins_config_path, model_config_path, info)."""
    from pyproj import Geod
    from shapely.geometry import Point, mapping, shape
    L = limits()
    name = re.sub(r"[^\w .,()'&/-]", "", str(spec.get("name") or "Uploaded basin"))[:60].strip() or "Uploaded basin"
    try:
        geom = shape(spec["geometry"])
    except Exception:
        raise ValueError("The uploaded basin is not a valid polygon")
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError("The uploaded basin must be a polygon")
    if not geom.is_valid:
        geom = geom.buffer(0)
    W, S, E, N = L["region_bbox"]
    gx0, gy0, gx1, gy1 = geom.bounds
    if gx0 < W or gx1 > E or gy0 < S or gy1 > N:
        raise ValueError(f"The basin must lie within {L['region_name']}")
    area = abs(Geod(ellps="WGS84").geometry_area_perimeter(geom)[0]) / 2589988.11
    if area > L["max_area_sqmi"] or area < L["min_area_sqmi"]:
        raise ValueError(f"Basin area {area:.2f} sq mi is outside {L['min_area_sqmi']}-{L['max_area_sqmi']} sq mi")
    outlet = spec.get("outlet")
    if outlet:
        pt = Point(float(outlet[0]), float(outlet[1]))
        if geom.distance(pt) > 0.01:  # ~1 km: a stray point, not this basin's pour point
            outlet = None
    tag = f"_run_{run_id}"
    poly_path = os.path.join(ROOT, "data", "basins", f"{tag}.geojson")
    json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": name},
              "geometry": mapping(geom)}]}, open(poly_path, "w"))
    csv_txt = (spec.get("streamstats_csv") or "")[:L["max_csv_chars"]]
    csv_path = None
    if "AEP" in csv_txt or "year" in csv_txt.lower():
        csv_path = os.path.join(ROOT, "data", "streamstats", f"{tag}_streamstats.csv")
        open(csv_path, "w").write(csv_txt)

    bcfg = json.load(open(os.path.join(ROOT, "config", "basins.json")))
    rep = geom.representative_point()
    m = 0.12
    bcfg["region_bbox"] = [round(gx0 - m, 3), round(gy0 - m, 3), round(gx1 + m, 3), round(gy1 + m, 3)]
    bcfg["basins"] = [{
        "id": CUSTOM_ID, "name": name, "polygon": poly_path,
        "outlet": [float(outlet[0]), float(outlet[1])] if outlet else [rep.x, rep.y],
        "outlet_auto": not outlet, "published_area_sqmi": {},
        "rain_gauges": [], "stage_gauges": [], "channel": None,
        "frequency": ([{"basis": "streamstats", "file": csv_path, "label": "USGS StreamStats (uploaded report)",
                        "transfer_exponent": 0.6}] if csv_path else []),
        "notes": ["Uploaded basin." + ("" if csv_path else " No StreamStats flow report was uploaded, so the flood "
                                         "return period is not available; the peak discharge is still computed.")],
    }]
    b_path = os.path.join(ROOT, "config", f"{tag}_basins.json")
    json.dump(bcfg, open(b_path, "w"), indent=1)

    mcfg = json.load(open(os.path.join(ROOT, "config", "model.json")))
    mcfg["basins"] = {CUSTOM_ID: {
        "name": name, "role": "uploaded basin",
        "outlet": [float(outlet[1]), float(outlet[0])] if outlet else None,
        "polygon": f"{tag}.geojson", "basin_source": "auto" if outlet else "polygon",
        "published_area_sqmi": {}, "gauges": [], "structure_notes": [], "channel": None,
        "peak_flow_stats": ({"csv": [f"{tag}_streamstats.csv"], "transfer_exponent": 0.6,
                             "source": "uploaded StreamStats report"} if csv_path else {}),
    }}
    m_path = os.path.join(ROOT, "config", f"{tag}_model.json")
    json.dump(mcfg, open(m_path, "w"), indent=1)
    return b_path, m_path, {"name": name, "area_sqmi": round(area, 2), "outlet_from_file": bool(outlet),
                            "streamstats_report": bool(csv_path)}


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
    ap.add_argument("--basins", nargs="*", default=[])
    ap.add_argument("--custom-basin-env", default="CUSTOM_BASIN",
                    help="environment variable holding an uploaded basin as JSON (empty = preset basins)")
    ap.add_argument("--stage", default="all", choices=["validate", "radar", "model", "soil", "finalize", "all"])
    ap.add_argument("--out-root", default=os.path.join(ROOT, "out", "runs"))
    a = ap.parse_args()

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{5,47}", a.run_id):
        sys.exit("bad run id")
    basins = [b for part in a.basins for b in re.split(r"[,\s]+", part) if b]
    out = os.path.join(a.out_root, a.run_id)
    os.makedirs(out, exist_ok=True)
    b_cfg = m_cfg = None
    custom = None
    raw = os.environ.get(a.custom_basin_env, "").strip()
    try:
        if raw:
            b_cfg, m_cfg, custom = prepare_custom_basin(a.run_id, json.loads(raw))
            basins = [CUSTOM_ID]
    except (ValueError, KeyError, json.JSONDecodeError) as ex:
        write_json(os.path.join(out, "run.json"), {"run_id": a.run_id, "ok": False, "error": str(ex),
                                                   "finished_utc": datetime.now(timezone.utc).isoformat()})
        print(f"invalid basin: {ex}")
        sys.exit(2)
    cfg_args_b = ["--config", b_cfg] if b_cfg else []
    cfg_args_m = ["--config", m_cfg] if m_cfg else []
    stages = ["validate", "radar", "model", "soil", "finalize"] if a.stage == "all" else [a.stage]
    try:
        s, e = validate(a.start_mst, a.end_mst, basins, basins_config=b_cfg)
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
    if custom:
        name = f"{custom['name']} · {name}"
    rc = 0
    for st in stages:
        t0 = time.time()
        if st == "validate":
            write_json(os.path.join(out, "request.json"), {
                "run_id": a.run_id, "start_mst": s.strftime("%Y-%m-%dT%H:%M"), "end_mst": e.strftime("%Y-%m-%dT%H:%M"),
                "basins": basins, "custom_basin": custom, "requested_utc": datetime.now(timezone.utc).isoformat(),
                "pipeline_version": json.load(open(os.path.join(ROOT, "config", "model.json")))["version"]})
            stage_state(out, st, True, t0)
        elif st == "radar":
            r = run([sys.executable, "scripts/run_event.py", "--event", a.run_id, "--name", name,
                     "--start-mst", s.strftime("%Y-%m-%dT%H:%M"), "--end-mst", e.strftime("%Y-%m-%dT%H:%M"),
                     "--out-dir", out, "--no-index", "--basins", *basins, *cfg_args_b])
            stage_state(out, st, r == 0, t0)
            if r:
                rc = r
                break  # nothing to show without radar
        elif st == "model":
            ms = s.replace(minute=0)
            me = e if e.minute == 0 else e.replace(minute=0) + timedelta(hours=1)
            r = run([sys.executable, "scripts/run_model.py", "--event", a.run_id, "--basins", *basins,
                     "--start-local", ms.strftime("%Y-%m-%dT%H:%M"), "--end-local", me.strftime("%Y-%m-%dT%H:%M"),
                     "--label", name, "--out", os.path.join(out, "model"), *cfg_args_m])
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
                     "--out-dir", out, "--basins", *basins, *cfg_args_b])
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
            if custom and not custom["outlet_from_file"] and meta_ok and os.path.exists(mi):
                ms = os.path.join(out, "model", CUSTOM_ID, "summary.json")
                if os.path.exists(ms):
                    bs = json.load(open(ms))["basin"]
                    mp = os.path.join(out, "meta.json")
                    meta = json.load(open(mp))
                    for b in meta["basins"]:
                        if b["id"] == CUSTOM_ID:
                            b["outlet"] = [bs["outlet_lon"], bs["outlet_lat"]]
                            b["notes"] = b.get("notes", []) + ["Outlet placed by the model on the polygon edge (no pour point in the upload)."]
                    json.dump(meta, open(mp, "w"), separators=(",", ":"))
            write_json(os.path.join(out, "run.json"), {
                **req, "name": name, "ok": meta_ok, "stages": sts, "sources": sources,
                "finished_utc": datetime.now(timezone.utc).isoformat(),
                "start_utc": s.astimezone(timezone.utc).isoformat(), "end_utc": e.astimezone(timezone.utc).isoformat()})
            if not meta_ok:
                rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
