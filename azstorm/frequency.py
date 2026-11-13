"""Flood-frequency curves: StreamStats report parsing, area transfer, return-period lookup."""
from __future__ import annotations

import csv
import io
import math

from scipy.stats import norm


def parse_streamstats(path: str):
    """Read a StreamStats CSV report. Returns {'area_sqmi', 'region', 'rows': [{aep_pct, T, q, pil, piu}]}."""
    text = open(path, encoding="utf-8-sig").read()
    lines = text.splitlines()
    area = None
    region = None
    rows = []
    for i, l in enumerate(lines):
        if (l.startswith('"DRNAREA"') or l.startswith('"CONTDA"')) and area is None:
            area = float(next(csv.reader([l]))[2])
        if l.startswith("Peak-Flow Statistics Flow Report"):
            region = l.split(",", 1)[1].strip()
        m = next(csv.reader([l])) if l.startswith('"') else None
        if m and m[0].endswith("AEP flood") and len(m) >= 5:
            aep = float(m[0].split("-percent")[0])
            rows.append({"aep_pct": aep, "T": round(100.0 / aep, 3), "q": float(m[1]),
                         "pil": float(m[3]) if m[3] else None, "piu": float(m[4]) if m[4] else None})
    return {"area_sqmi": area, "region": region, "rows": rows}


def transfer(curve: dict, target_area: float, exponent: float = 0.6):
    """Scale a curve to another drainage area by Q * (At/As)^exponent."""
    f = (target_area / curve["area_sqmi"]) ** exponent
    rows = [{**r, "q": r["q"] * f, "pil": r["pil"] and r["pil"] * f, "piu": r["piu"] and r["piu"] * f}
            for r in curve["rows"]]
    return {**curve, "rows": rows, "factor": f, "target_area_sqmi": target_area,
            "source_area_sqmi": curve["area_sqmi"]}


def return_period(curve: dict, q: float):
    """Interpolate log Q against the normal deviate of non-exceedance. Returns T (yr) or a bound string."""
    rows = sorted(curve["rows"], key=lambda r: r["q"])
    qs = [math.log(r["q"]) for r in rows]
    zs = [norm.ppf(1 - r["aep_pct"] / 100) for r in rows]
    if q <= 0:
        return None
    lq = math.log(q)
    if lq < qs[0]:
        return f"<{rows[0]['T']:g}"
    if lq > qs[-1]:
        return f">{rows[-1]['T']:g}"
    for k in range(len(qs) - 1):
        if qs[k] <= lq <= qs[k + 1]:
            z = zs[k] + (zs[k + 1] - zs[k]) * (lq - qs[k]) / (qs[k + 1] - qs[k])
            return 1.0 / (1 - norm.cdf(z))
    return None
