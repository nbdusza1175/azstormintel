# azstormintel API Worker

The site is static, so on-demand analyses need a small service that holds a GitHub token and
starts `.github/workflows/analyze.yml`. This folder is that service: one Cloudflare Worker
(free plan is plenty).

```
browser ──POST /api/runs──▶ Worker ──workflow_dispatch──▶ GitHub Actions (analyze.yml)
   ▲                          │                                   │ radar, 3DEP, NLCD, SSURGO,
   └──GET /api/runs/:id ◀─────┘◀── runs branch: runs/<id>/ ◀──────┘ Atlas 14, SMAP, model
```

## One-time setup (about 15 minutes)

1. **GitHub token** – github.com → Settings → Developer settings → Fine-grained tokens → *Generate*.
   - Repository access: *Only select repositories* → `nbdusza1175/azstormintel`
   - Permissions: **Actions: Read and write**, **Contents: Read-only** (Metadata: read is automatic)
   - Expiration: up to a year; put a reminder in your calendar to renew it.
2. **Cloudflare account** – sign up at dash.cloudflare.com (free).
3. **Deploy** (needs Node 18+ on any computer, or the Cloudflare dashboard editor):
   ```
   cd worker
   npx wrangler login
   npx wrangler deploy
   npx wrangler secret put GITHUB_TOKEN      # paste the token from step 1
   ```
   Wrangler prints the URL, e.g. `https://azstormintel-api.<you>.workers.dev`.
   *No Node?* Dashboard → Workers & Pages → Create → "Hello World" worker → Edit code → paste
   `src/index.js` → Deploy. Then Settings → Variables: add `REPO`, `REF`, `ALLOWED_ORIGINS`,
   `RUNS_PER_IP_HOUR` (values in `wrangler.toml`) and the secret `GITHUB_TOKEN`.
4. **Point the site at it** – set `api` in `site/config.js` to that URL and push to main.
   The "New analysis…" button appears once `api` is set.
5. Check: open `https://<worker-url>/api/config` – it should list the limits and basins.

Optional per-IP limit: create a KV namespace, bind it as `RATE` (see `wrangler.toml`).

## Limits

`config/limits.json` (read by both the Worker and `scripts/run_request.py`):
start on or after Jan 1 of (this year − 10), up to 7 days per run, end at least 20 minutes ago,
at most 3 runs at once, results kept 45 days. The same window and basins always map to the
same run id, so a repeated request returns the saved result instantly.

## Endpoints

| | |
|---|---|
| `GET /api/config` | limits, pipeline version, preset basins |
| `POST /api/runs` `{start_mst, end_mst, basins[]}` | starts (or finds) a run → `{id, state}` |
| `GET /api/runs/:id` | `queued` / `running` (with workflow steps) / `publishing` / `done` / `failed` / `unknown` |
| `GET /api/runs/:id/files/<path>` | result files from the `runs` branch |

Tests: `node test.mjs` (mocked GitHub, no network).
