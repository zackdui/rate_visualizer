# BCBSTX Rates Tool: Hosting & Data Plan

## 1. Decisions
| Layer | Choice | Cost / month |
|---|---|---|
| Web app (frontend and backend together) | **NiceGUI** (Python), one app | $0 |
| Query engine | **DuckDB** inside the app, reading a local `site.duckdb` file | $0 |
| Site data storage | **Cloudflare R2** bucket (the app downloads the data at startup) | $0 (free tier) |
| Hosting | **Render** web service, **Standard** instance (2 GB RAM, 1 CPU) | $25 |
| Code and deploys | **GitHub** (`main` branch); Render redeploys on every push | $0 |
| Monthly pipeline | Runs on your laptop (the existing `rate-visualizer` commands) | $0 |
| **Total** | | **~$25** |

Not used in v1: **Vercel** (can't run a Python server, and browser-side DuckDB would download ~350 MB per visitor)
and **MotherDuck** (not needed to serve the site; add it later only if people want to run their own SQL).

*Prices are as of Sept 2026 and change often; check Render and Cloudflare pricing before signing up.*

## 1a. Your to-do list and open decisions
**To do (you):**
- [ ] Check current Render and Cloudflare R2 pricing and plan limits (section 8 assumes ~$25/month and the R2 free tier).
- [ ] Create the Cloudflare account, the R2 bucket and the **two** R2 API tokens (section 4.1).
- [ ] Put the **read & write** token in a `.env` file in the repo folder on your laptop (section 4.2). **Don't paste
      keys into chat.** The code reads them from `.env`, and `.env` will be git-ignored.
- [ ] Create the Render account and web service with the settings in section 4.3, and put the **read-only** token in
      Render's environment variables. The service won't start successfully until the site app exists (section 7).
- [ ] Copy Render's deploy hook URL into `.env` as `RENDER_DEPLOY_HOOK_URL`.
- [ ] Tell me when `.env` is filled in, so `publish-site` can be tested against the real bucket.

**To confirm when you set up** (these came from my knowledge, not checked against the providers' current sites):
- R2 needs a payment method on file even on the free tier.
- Render's region names (Ohio / Virginia) and that its Python runtime builds with `uv` and honours `PYTHON_VERSION`.

**Decisions (answered):**
- [x] Custom domain: **later**. For now use Render's automatic `*.onrender.com` address (HTTPS included).
- [x] Access: **public for now**; a password (`SITE_PASSWORD`) can be added later.
- [x] Small Thriveworks TIN (47-1744442): **tagged Thriveworks** as an affiliated location with its own TIN
      (`relationship = affiliated_location`, confidence medium). Reasoning in `entity_matching.md` §4.
- [x] Health-system tags: **done**. 238 TINs across all ten systems in `configs/entity_tags_tx.csv`; method and
      every decision in `entity_matching.md`.
- [x] Ghost-rate rule: **decided**. See `rates_tool_plan.md` §8.9. Market stats default to behavioral-health providers
      (new toggle in §6), because ghost rates turned out to be very common.
- [x] Backend: **built** (`build-site`, `publish-site`, the `backend` package, `check-backend`). API in
      `docs/backend_api.md`.
- [ ] Frontend (the NiceGUI page): **waiting for your go-ahead.**

## 2. Architecture
```
BCBSTX index + in-network files, NPPES, NUCC
        │  monthly: rate-visualizer index / extract / build-db / nppes  (your laptop)
        ▼
data/TX/<index_date>/rates.duckdb   full detail, 2.5 GB, stays on your laptop (source of truth + trace)
        │  rate-visualizer build-site                     (new step, section 7)
        ▼
data/TX/<index_date>/site.duckdb    slim site database, ~350 MB
        │  rate-visualizer publish-site                   (new step: upload + deploy hook)
        ▼
Cloudflare R2  bucket "rates-tool-data"
   sites/TX/<index_date>/site.duckdb
   sites/TX/latest.json             ← points to the current site.duckdb (+ SHA-256, size)
        │  on every start: read latest.json, download site.duckdb, check SHA-256
        ▼
Render web service "rates-tool"  (NiceGUI + DuckDB, read-only, in-process)
        ▼
Users (browser)
```

## 3. Where each piece of data lives
| Data | Where | Size | In Git? |
|---|---|---|---|
| Pipeline code, tests, configs, docs | GitHub repo | small | Yes |
| Downloaded index + NPPES zip | laptop: `mrf_data/`, `data/nppes/` | ~1.2 GB | No (git-ignored) |
| Map inputs: Census 2025 ZCTA gazetteer + HRSA ZIP-to-ZCTA crosswalk | laptop: `data/geo/` (sources in `rates_tool_plan.md` §8.5) | ~5 MB | No |
| Entity tags seed file (`configs/entity_tags_tx.csv`, 250 TINs; see `entity_matching.md`) | GitHub repo | tiny | Yes |
| Per-file extraction output (Parquet) | laptop: `data/TX/<date>/files/` | ~100 MB | No |
| Full database (`rates.duckdb`) | laptop: `data/TX/<date>/` | ~2.5 GB | No |
| Site database (`site.duckdb`) | laptop, then **R2** `sites/TX/<date>/site.duckdb` | ~350 MB | No |
| Pointer to the current site database | **R2** `sites/TX/latest.json` | tiny | No |
| Secrets (R2 keys, deploy hook URL) | laptop: `.env` (git-ignored); Render: environment variables | – | **Never** |
| Running app's copy of `site.duckdb` | Render instance disk (re-downloaded on each start) | ~350 MB | No |

Keep the last 3 months of `site.duckdb` in R2 (~1 GB) so a bad month can be rolled back by editing `latest.json`.
The raw BCBSTX files are not stored (the pipeline streams them); `trace` works while each month's files are online.

`latest.json` format:
```json
{"state": "TX", "index_date": "2026-08-20", "key": "sites/TX/2026-08-20/site.duckdb",
 "sha256": "…", "bytes": 364000000, "built_at": "2026-09-30T12:00:00Z", "parser_git_commit": "…"}
```

## 4. One-time setup (in this order)

### 4.1 Cloudflare R2 (~15 min)
1. Create a Cloudflare account at dash.cloudflare.com (free). Open **R2 Object Storage** and enable it. R2 asks
   for a payment method even on the free tier; usage here stays inside the free limits.
2. **Create bucket** → name `rates-tool-data`, location hint **Eastern North America (ENAM)**, default storage
   class. Leave public access **off** (the app reads it with a key).
3. **Manage R2 API tokens → Create API token**, twice:
   - `rates-tool-pipeline`: permission **Object Read & Write**, bucket `rates-tool-data` only. Used by your laptop.
   - `rates-tool-site`: permission **Object Read only**, bucket `rates-tool-data` only. Used by Render.
   For each, copy the **Access Key ID** and **Secret Access Key** (shown once) and note the **Account ID**
   (the S3 endpoint is `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`).
4. Optional: add a **lifecycle rule** to delete objects under `sites/` older than 100 days.

### 4.2 Laptop `.env` (git-ignored: copy `.env.example` to `.env` and fill it in)
```
R2_ACCOUNT_ID=…
R2_ACCESS_KEY_ID=…            # rates-tool-pipeline token (read & write)
R2_SECRET_ACCESS_KEY=…
R2_BUCKET=rates-tool-data
RENDER_DEPLOY_HOOK_URL=…      # from step 4.3.6
```

### 4.3 Render (~20 min)
1. Create an account at render.com and connect GitHub (grant access to `zackdui/rate_visualizer` only).
2. **New → Web Service** → repo `rate_visualizer`, branch `main`.
3. Settings:
   | Setting | Value |
   |---|---|
   | Name | `rates-tool` |
   | Region | **Ohio (us-east-2)** or Virginia: closest to Texas users and to R2's ENAM location |
   | Runtime | Python 3 |
   | Build command | `pip install uv && uv sync --frozen --no-dev` |
   | Start command | `.venv/bin/rate-visualizer site --host 0.0.0.0 --port $PORT` |
   | Instance type | **Standard** (2 GB RAM, 1 CPU, $25/month) |
   | Health check path | `/healthz` (answers only after the data is downloaded and opened) |
   | Auto-deploy | On commit to `main` |
4. **Environment variables**:
   ```
   PYTHON_VERSION=3.12
   R2_ACCOUNT_ID=…
   R2_ACCESS_KEY_ID=…            # rates-tool-site token (read only)
   R2_SECRET_ACCESS_KEY=…
   R2_BUCKET=rates-tool-data
   SITE_MANIFEST_KEY=sites/TX/latest.json
   ```
5. Don't add a persistent disk: the app downloads `site.duckdb` from R2 on each start (~350 MB, well under a minute
   in the same region). Render keeps the old instance serving until the new one passes `/healthz`, so a data
   refresh has no downtime.
6. **Settings → Deploy Hook**: copy the URL into your laptop `.env` as `RENDER_DEPLOY_HOOK_URL`. Calling it
   restarts the app, which then loads whatever `latest.json` points to.
7. Optional later: custom domain (HTTPS is automatic) and a password (`SITE_PASSWORD` env var + a login page).
   The site is public for now.

## 5. Monthly refresh (after section 7 is built)
```
uv run rate-visualizer index                       # new month's index
uv run rate-visualizer extract --in-state --workers 4
uv run rate-visualizer profile                     # review profile.txt
uv run rate-visualizer build-db
uv run rate-visualizer nppes
uv run rate-visualizer build-site                  # -> site.duckdb, with sanity checks
uv run rate-visualizer publish-site                # upload to R2, update latest.json, call the deploy hook
```
Roughly 35 min extract + 14 min build-db + 2–3 min NPPES + a few minutes for the rest. `build-site` stops if row
counts change drastically vs. the previous month (e.g. more than ±30% per code), so a broken month is never published.

## 6. Performance (measured on the real data)
A slim test copy of the site data (16.9 M deduplicated rates + provider info) was **347 MB**. On 1 thread and
1 GB of memory:
| Query | Time |
|---|---|
| One code, filtered by specialty + city (count TINs, quartiles) | 0.08 s |
| Name search, first 200 rows | 0.05 s |
| All 9 codes × percentiles, per TIN, nothing precomputed | 1.30 s |

So the rules are:
1. **Precompute** the default Code Summary (per code × provider type × network × place of service, per TIN and
   per NPI) and TIN/NPI summaries in `build-site`. Filtered views run live.
2. **Sort** the fact table by `billing_code`, then TIN, so filters skip most of the file.
3. **Cache** filter options (codes, networks, provider types, cities, ZIPs) at startup.
4. **Never send all rows to the browser**: the Rate Explorer loads 200–500 rows per page from the server; CSV
   export streams from the server.
5. **Map**: one point per provider for the current filters, clustered; above ~5,000 points, aggregate by ZIP.
6. Before launch, time the 5 slowest views; anything over ~0.5 s gets a precomputed table.

Standard's 2 GB leaves room for the ~350 MB file, DuckDB's working memory (capped at ~1 GB) and the app.

## 7. Code still to build (next work)
| Piece | What it does |
|---|---|
| `rate-visualizer build-site` | From `rates.duckdb`: slim fact table (dedup grain, sorted, with a `rate_id` linking back to full detail/sources), `providers` (NPPES + provider-type group + ZIP lat/lon), `tins`, `entity_tags`, `files`, `dq_stats`, precomputed summaries. Writes `site.duckdb` + checks |
| `rate-visualizer publish-site` | Uploads `site.duckdb` to R2 (S3 API via `boto3`), verifies SHA-256, writes `latest.json`, keeps 3 months, calls the deploy hook |
| `rate-visualizer site` | The NiceGUI app: downloads/opens the data, `/healthz`, one page with the sections in `rates_tool_plan.md` |
| ZIP coordinates | Census 2025 ZCTA gazetteer + HRSA ZIP-to-ZCTA crosswalk (already downloaded to `data/geo/`), loaded by `build-site`; 99.996% of Texas NPIs get a point |
| Entity tags | `configs/entity_tags_tx.csv` seeded from the platform research (`rates_tool_plan.md` §10.1), loaded by `build-site` |

**Libraries:** `nicegui` (app, sticky header, dark mode, Tailwind styling), `duckdb`, `ui.echart` (ECharts:
histograms, bars), `ui.aggrid` (AG Grid tables), `ui.leaflet` with marker clustering (map), `boto3` (R2). Design:
one accent color, light/dark toggle, a clean font (e.g. Inter), generous spacing.

## 8. Limits & risks
| Risk | Mitigation |
|---|---|
| R2 free tier: 10 GB storage, 1 M writes and 10 M reads per month, no download fees | ~1 GB stored, a handful of operations per month |
| Render Standard memory (2 GB) | Precomputed tables, DuckDB memory cap, paginated tables; next size up if ever needed |
| A bad month published | `build-site` checks; roll back by pointing `latest.json` at the previous month and calling the deploy hook |
| Secrets leaking | Separate read-only key for Render; `.env` git-ignored; no keys in code |
| Laptop disk (29 GB free) | Pipeline limits (`max_storage_gb = 13`); delete old `data/TX/<date>/` folders after publishing |

## 9. Alternatives considered
| Option | Why not chosen |
|---|---|
| Vercel (static + DuckDB-WASM, or functions) | No always-on Python server; browser download of ~350 MB per visitor, or metered per-query compute |
| Next.js on Vercel + FastAPI on Render | Best polish, but two codebases and more work; the site needs to look good, not perfect |
| Streamlit (any host) | Reruns the whole page on every filter change; sticky nav needs hacks |
| Dash + Mantine | Good fallback; more boilerplate than NiceGUI |
| Fly.io (~$10–15) | Cheaper, but needs a Dockerfile and CLI; Render is simpler at $25 |
| Railway | Usage-based bills; no advantage here |
| Render Starter ($7, 512 MB) | Too small for a ~350 MB data file plus the app |
| MotherDuck as the site's data source | Adds a second service and token; its next tier is $250/month; not needed to serve the site |

## 10. Backend configuration
The backend is built (see `docs/backend_api.md`); the frontend isn't. This section lists every service, key and
interface the backend uses.

### 10.1 External services and APIs
| Service | Used by | Interface | Auth | Operations |
|---|---|---|---|---|
| **Cloudflare R2** | `publish-site` (laptop) | S3-compatible API at `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`, region `auto`, via `boto3` | read & write token | multipart upload of `site.duckdb`; `HeadObject` to verify size; upload `latest.json` last; list + delete months older than the newest 3 |
| **Cloudflare R2** | site (Render) | same | read-only token | get `latest.json`, download `site.duckdb`, check SHA-256 |
| **Render deploy hook** | `publish-site` | HTTP POST to `RENDER_DEPLOY_HOOK_URL` | the secret URL itself | restart the site after a new month is uploaded |
| **Render health check** | Render | `GET /healthz` on the site | none | 200 only once `site.duckdb` is downloaded, verified and opened |
| OpenStreetMap tiles | site (browser) | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` (NiceGUI `ui.leaflet` default) | none; attribution shown | background map |
| BCBSTX index + MRFs | pipeline | HTTPS (index URL in `configs/tx.toml`) | none | monthly download/stream (already built) |
| NPPES + NUCC | pipeline | HTTPS, newest file auto-picked (already built) | none | monthly |
| Census ZCTA gazetteer | `build-site` | `https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_zcta_national.zip` | none | yearly |
| HRSA ZIP→ZCTA crosswalk | `build-site` | `https://data.hrsa.gov/DataDownload/GeoCareNavigator/ZIP%20Code%20to%20ZCTA%20Crosswalk.xlsx` | none | yearly |
| Headway help center | tag re-checks (optional) | Zendesk JSON: `https://help.headway.co/api/v2/help_center/en-us/articles/21556524888596.json` | none | re-check Headway's published entity list |

No other paid service or API key is needed. Everything else runs inside the app.

### 10.2 Environment variables (template: `.env.example`)
| Variable | Laptop `.env` | Render | Purpose |
|---|---|---|---|
| `R2_ACCOUNT_ID` | ✓ | ✓ | R2 endpoint |
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | ✓ read & write token | ✓ read-only token | R2 auth |
| `R2_BUCKET` | ✓ `rates-tool-data` | ✓ `rates-tool-data` | bucket |
| `SITE_MANIFEST_KEY` | optional | ✓ `sites/TX/latest.json` | which month the site loads |
| `RENDER_DEPLOY_HOOK_URL` | ✓ | – | restart after publishing |
| `PYTHON_VERSION` | – | ✓ `3.12` | Render build |
| `PORT` | – | set by Render | app port |
| `SITE_PASSWORD` | – | later | optional password |

### 10.3 `site.duckdb` contract (built by `build-site`; names in `backend/schema.py`)
| Table | Grain | Contents |
|---|---|---|
| `rates` | one deduplicated rate (NPI × TIN × code × price × conditions) | `rate_id`, snapshot_date, billing_code, negotiation_arrangement, negotiated_type, rate_unit, negotiated_rate (+ exact `negotiated_rate_raw`), is_zero_rate, billing_class, setting, `pos_set_id`, `pos_group`, modifier, expiration_date, is_expired, npi, is_valid_npi, tin_type, tin, networks, is_texas_file, n_sources, and copied filter columns: provider_type_group, practice_state/city/zip5, `scope_flag` (ghost rule), tag_type/tag_name/tag_confidence. Sorted by code, then TIN |
| `pos_sets` | place-of-service list | `pos_set_id` → `service_code` list (a handful of distinct lists; rates store the id) |
| `rate_sources` | rate × source | rate_id, `file_idx` (→ `files`), JSON positions i, j, k, r, m, n, p. Group IDs and original list order are one `trace` away in the raw file |
| `providers` | NPI | name, entity type 1/2, taxonomy + NUCC specialty, provider_type_group, is_prescriber_type, practice address/city/state/ZIP, lat/lon, geo_source, in_nppes |
| `tins` | TIN | display_name (+ name_source), name_variants, NPPES org_names, n_npis, tag columns, search_text |
| `entity_tags` | TIN | `configs/entity_tags_tx.csv` |
| `files` | source file | file_idx, file_id, URL, descriptions, network_names, schema version, last_updated_on, SHA-256, is_texas_file |
| `capitation` | payment × payee NPI | capitation rows with covered target codes and payee names |
| `dq_stats`, `dq_conflicts`, `dq_file_rows` | data quality | precomputed counts; keys with >1 price per network; rows per file before dedup |
| `site_meta` | 1 row | state, snapshot_date, schema_version, built_at, code version, row counts |

### 10.4 Site endpoints
| Path | What |
|---|---|
| `/` | the single page (NiceGUI, live over a websocket) |
| `/healthz` | Render health check |
| `/export/rates.csv?…` | CSV export of the current Rate Explorer filters (streamed from DuckDB) |

### 10.5 Backend query layer (Python functions the page calls)
All take one `Filters` object (the global filters and toggles in `rates_tool_plan.md` §6) and return small result sets:
`filter_options()`, `code_summary()`, `benchmarks()`, `rate_explorer(page, sort)`, `percentage_rates(page)`,
`map_points(code)`, `rate_type_composition()`, `provider_profile(npi)`, `billing_entity_profile(tin)`,
`data_quality()`, `source_rows(rate_id)`. One shared read-only DuckDB connection (cursor per request), memory capped
at ~1 GB, filter options cached at startup.

### 10.6 Config and dependencies (added)
- `configs/tx.toml`: `r2_prefix = "sites/TX"`, `entity_tags_path = "configs/entity_tags_tx.csv"`, `geo_zcta_url`,
  `geo_crosswalk_url`, `site_months_kept = 3`, `site_max_change = 0.30`.
- Python packages: `boto3` (R2) and `openpyxl` (HRSA crosswalk) added; `nicegui` comes with the frontend.
- Extra optional env vars for the site: `SITE_DB_PATH` (use a local site.duckdb, for development),
  `SITE_CACHE_DIR` (default `.site_cache`), `DUCKDB_MEMORY_LIMIT` (default 1GB), `DUCKDB_THREADS`.

### 10.7 Ready-to-build checklist
- [x] Hosting, storage and app stack decided (sections 1–4)
- [x] Map data sources found and downloaded; coverage checked (`rates_tool_plan.md` §8.5)
- [x] Entity tags seeded (`configs/entity_tags_tx.csv`) and documented (`entity_matching.md`)
- [x] Provider-type groups verified; ghost rule decided (`rates_tool_plan.md` §4, §8.9)
- [x] `.env.example` added and `.env` git-ignored
- [ ] Cloudflare R2 bucket + two tokens; `.env` filled in (you)
- [ ] Render service created with the env vars in 10.2 (you)
- [x] `build-site`, `publish-site`, backend package built and tested
- [ ] Go-ahead to build the frontend (you)

### 10.8 Measured backend speed (real data, `uv run rate-visualizer check-backend`)
`site.duckdb` for the 2026-08-20 snapshot: **766 MB**, built in ~3.5 minutes. `rates` 16.9 M rows; `rates_core`
(rows under the default toggles, used automatically when the filters allow) 1.47 M rows; `rate_sources` 35.4 M.

| Call (whole of Texas, default filters unless noted) | 1 thread, 1 GB (Render Standard) | 2 threads |
|---|---|---|
| code_summary | 0.38 s | 0.20 s |
| code_summary by network | 1.45 s | 0.75 s |
| benchmarks (all 19 tagged entities × 9 codes, 7,755 rows) | 1.17 s | 0.92 s |
| benchmarks, Headway only | 0.63 s | 0.43 s |
| rate_explorer page 1 / name search / summarise by TIN | 0.22 / 0.39 / 0.39 s | 0.16 / 0.40 / 0.24 s |
| map_points (90837, 5,000 ZIP points) | 0.41 s | 0.25 s |
| provider_list / entity_list | 0.76 / 0.55 s | 0.49 / 0.32 s |
| provider_profile / billing_entity_profile | 0.29 / 0.20 s | 0.18 / 0.15 s |
| Houston psychiatrists, 90837 (a typical filtered view) | 0.03 s | 0.02 s |
| source_rows (trace one number) | 0.11 s | 0.07 s |

- Narrower filters are faster (less data); repeated calls come from the in-memory cache.
- The heaviest default views (summaries by every breakdown, all benchmarks, rate-type composition, first explorer
  page, filter options) are computed at startup by `Backend.warm_up()`, so the first visitor gets them instantly.
- If the site ever feels slow, the next Render size up (2 CPUs) roughly halves the heavy calls.
