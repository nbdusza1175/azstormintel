# AZ Storm Intel — Coconino County storm & flood intensity

Radar-driven storm and flood intensity for Coconino County Flood Control District basins.
Same radar and ground data always give the same answer; gauges are checks only.

Live site: https://azstormintel.com (GitHub Pages)

## How it runs

| Workflow | When | What |
|---|---|---|
| **Build data** (`.github/workflows/build.yml`) | push to `config/`, `azstorm/`, `scripts/`, `data/`; or run manually | Basemap tiles → `site/tiles/`; archived radar events → `site/data/events/<id>/`; ModClark flood model → `site/data/events/<id>/model/`. Outputs are committed. |
| **Deploy site** (`.github/workflows/pages.yml`) | every 30 min, after Build data, on `site/` changes | Builds a live 24-hour radar window (not committed) and publishes `site/` to Pages. |
| **Analyze** (`.github/workflows/analyze.yml`) | started by the API Worker when a visitor clicks *New analysis* | Radar + ground conditions + flood model + SMAP for the visitor's window → `runs` branch, `runs/<id>/`. |
| **Runs cleanup** (`.github/workflows/runs-cleanup.yml`) | weekly | Drops runs older than 45 days and squashes the `runs` branch. |

### On-demand analyses (v3.4)
Visitors pick a window (MST, back to Jan 1 ten years ago, up to 7 days) and basins. The API Worker
(`worker/`, Cloudflare, free) validates the request and starts **Analyze**; the page shows the
workflow steps live and loads the results into the same viewer when they're published (about
5–20 min). Same window + basins → same run id, so repeats are instant. Setup: `worker/README.md`.

Data for older windows: MRMS on AWS starts 2020-10-14; earlier radar comes from the Iowa
Environmental Mesonet mtarchive (pre-v12 names, e.g. `GaugeCorr_QPE_01H` in place of Pass 2;
reflectivity may be missing). Land cover and impervious area use the **Annual NLCD** year of the
event (latest available on or before it); SSURGO, 3DEP and Atlas 14 are current editions;
SMAP L4 covers 2015 on.

## Layout

```
config/basins.json      site config: basins, polygons, gauges, frequency bases, channel, radar events
config/model.json       flood model config (v3.3: ModClark, kinematic channel velocity, design check)
config/cn_table.json    curve numbers by NLCD class x HSG
data/basins/            basin polygons (GeoJSON)
data/streamstats/       StreamStats peak-flow reports (CSV, "N-percent AEP" format)
data/gauges/            ALERT exports for gauge checks (optional; file names as in config/model.json)
azstorm/mrms.py         NOAA MRMS listing/reading (AWS noaa-mrms-pds)
azstorm/radar.py        5-min rain: PrecipRate timing scaled to Pass 2 hourly depth; basin stats
azstorm/model.py        ModClark flood model (3DEP, NLCD, SSURGO, Atlas 14, MRMS)
azstorm/frequency.py    StreamStats parsing, area transfer, return periods
scripts/run_event.py    radar event or live window -> site/data/...
scripts/run_request.py  on-demand run for a user window (stages: validate, radar, model, soil, finalize)
config/limits.json      on-demand limits (shared with the Worker)
worker/                 Cloudflare Worker API that starts Analyze runs and serves results
scripts/run_model.py    flood model -> site/data/events/<id>/model/
scripts/build_basemap.py  USGS (public-domain) basemap tiles for offline display
site/                   static viewer (Leaflet, no build step)
```

## Add an event
Add an entry to `events` in `config/basins.json` (MST start/end) and, for the flood model,
to `events` in `config/model.json` with `site_event` set to the same id. Push; Build data runs.

## One-time setup
1. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
2. **Settings → Pages → Custom domain:** `azstormintel.com`, then tick *Enforce HTTPS* once the certificate is issued.
3. At the domain registrar, add DNS records:
   - `A` records for `@`: 185.199.108.153, 185.199.109.153, 185.199.110.153, 185.199.111.153
   - `CNAME` for `www` → `nbdusza1175.github.io`
4. Optional: **Settings → Pages → verify the domain** (protects against takeover).

## Method notes
- Radar: MRMS PrecipRate (2-min) summed to 5-min, scaled per cell and hour to MultiSensor QPE Pass 2.
- Flood model: distributed SCS CN (NLCD × SSURGO), D8 time-area translation on the 3DEP 10 m DEM with
  channel velocity at the routed discharge, Clark reservoir (FCDMC R). Natural, unregulated flow.
- Mountain Dell: design basis is the 100-yr 24-hr storm (2017 IEA, FLO-2D: 1,088 cfs above Holmes Ave,
  1,391 cfs below; Civiltec design report 2019). FEMA FIS 100-yr is 470 cfs; StreamStats is shown alongside.
