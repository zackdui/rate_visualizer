# Backend API (for the website's frontend)

The website is split into a **backend** (data and queries) and a **frontend** (the NiceGUI page, not built yet).
This doc is the frontend's contract: every call it can make, what goes in and what comes out.

## 1. Boundaries (don't mix front and back)
```
src/rate_visualizer/
  index, extract, profile, build_db, nppes, trace   pipeline (laptop, monthly)
  sitebuild/                                        laptop: build-site -> site.duckdb, publish-site -> R2
  backend/                                          runs in the website process
    schema.py     table names, provider types, POS labels, scope flags, code labels (shared with sitebuild)
    filters.py    Filters: every filter/toggle as one immutable object
    queries.py    Backend: one method per page section
    settings.py   environment variables / .env
    storage.py    R2 (boto3) or a local folder
    loader.py     download + verify site.duckdb at startup
  frontend/                                         later: NiceGUI page(s)
```
Rules:
1. The frontend imports **only** `rate_visualizer.backend` (`Filters`, `Backend`, `open_backend`, `schema`
   for labels). It never writes SQL and never opens DuckDB itself.
2. The backend never imports UI code, and returns **plain Python data**: dicts, lists, `str`, `int`, `float`,
   `bool`, `None`. Dates come back as ISO strings. Everything is JSON-serialisable, so the same calls could sit
   behind HTTP routes later without changes.
3. All filtering goes through one `Filters` object. The frontend's filter bar edits a `Filters`, then passes it to
   whichever sections are on screen.

## 2. Starting the backend
```python
from rate_visualizer.backend import Filters, open_backend
backend = open_backend()        # reads .env / env vars; downloads + verifies site.duckdb from R2 (or SITE_DB_PATH)
backend.health()                # {"ok": True, "rates": 16903856, "snapshot_date": "2026-08-20"}
```
- **Local development without R2:** set `SITE_DB_PATH=data/TX/2026-08-20/site.duckdb` in `.env`.
- **On Render:** `open_backend()` reads `latest.json` from R2, downloads `site.duckdb` into `.site_cache/<date>/`
  (skipped if already there and the SHA-256 matches), verifies size and SHA-256, and opens it read-only.
  `/healthz` should return 200 only once `backend.health()["ok"]` is true.
- Errors: `SiteDataError` (manifest missing, download doesn't match, R2 not configured; the message lists any missing
  variables). Invalid arguments raise `ValueError`.

## 3. `Filters`
Immutable and hashable (so results are cached per filter set). Build with keywords, `Filters.from_dict(d)` (unknown
keys are ignored), or change one field with `f.replace(codes=("90837",))`. Lists are accepted and stored as sorted
tuples.

| Field | Type / values | Default | Meaning |
|---|---|---|---|
| `codes` | tuple of codes | `()` all | billing codes |
| `provider_types` | Psychiatrist, Psych NP, Psychologist, LCSW, LPC, LMFT, Other, Organization | `()` | NPI's provider type |
| `networks` | e.g. "Blue Choice PPO" | `()` | rate applies in any of these networks |
| `states`, `cities`, `zips` | NPPES practice location (cities upper case; 5-digit ZIPs) | `()` | provider location |
| `places_of_service` | POS codes, e.g. "11" | `()` | rate lists any of these POS codes |
| `pos_groups` | non_facility, facility, all_settings, unspecified | `()` | BCBSTX's office vs facility rate lists |
| `billing_classes` | professional, institutional | `("professional",)` | fee type |
| `tag_types` | platform, health_system, none | `()` | entity tag type ("none" = untagged) |
| `tag_names` | e.g. "Headway" | `()` | specific tagged entities |
| `min_tag_confidence` | confirmed, high, medium | `"high"` | a TIN counts as tagged at this confidence or above |
| `search` | free text | `""` | contains-match on NPI, TIN, provider name, billing-entity names |
| `tins`, `npis` | exact values | `()` | drill-down (e.g. from a summary row) |
| `exclude_zero` | bool | `True` | hide $0 rates |
| `exclude_invalid_npi` | bool | `True` | hide NPI `0` and other invalid NPIs |
| `texas_files_only` | bool | `True` | only rates from Texas network files |
| `dollar_rates_only` | bool | `True` | `rate_unit = dollars` and type negotiated / fee schedule |
| `exclude_expired` | bool | `True` | hide rates expired before the snapshot date |
| `exclude_platforms_from_market` | bool | `False` | leave platform TINs out of market statistics |
| `bh_providers_only` | bool | `True` | only `scope_flag = expected` (decision log 2026-09-29) |
| `counting_unit` | tin, npi, row | `"tin"` | what one observation is in statistics |

**Counting unit:** with `tin` or `npi`, each unit is reduced to one value per code (the median of its rates under the
current filters) before percentiles are taken; with `row`, every rate row counts.

## 4. Methods
All return plain data. "Rows" are lists of dicts. Paginated methods return
`{"total", "page", "page_size", "rows"}`. `page_size` is capped at 5,000.

| Method | Page section | Returns |
|---|---|---|
| `meta()` | header | snapshot date, build time, code version, row counts |
| `health()` | `/healthz` | `{"ok", "rates", "snapshot_date"}`; never raises |
| `filter_options()` | filter bar | `codes` (value, label, group), `provider_types`, `networks`, `states`, `cities` (TX, with counts), `zips` (TX), `places_of_service` (value, label), `pos_groups`, `billing_classes`, `negotiated_types`, `tag_types`, `tag_names` (by type), `scope_flags`, `counting_units`, `defaults` |
| `suggest(field, text, limit=20)` | type-to-search | `field` = city, zip, provider (NPI/name) or entity (TIN/name) |
| `code_summary(f, breakdown=None)` | 8.1 | per code (and optional breakdown: provider_type, network, place_of_service, tag): `n_tins, n_npis, n_rows, n_units, min, p10, p25, p50, p75, p90, max, mean, pct_percentage_rows, label` |
| `histogram(f, code, bins=30)` | 8.1 | `edges`, `counts`, `underflow`/`overflow` (outside P1–P99), `markers` (tagged entities: median + distinct rates) |
| `benchmarks(f, codes=None)` | 8.2 | one row per entity × code × distinct rate × networks × POS group × provider type: `rate, n_npis, n_tins, n_market, market_median, pct_below, pct_at_or_below` |
| `rate_explorer(f, page, page_size, sort_by, sort_dir, summarize_by=None)` | 8.3 | rows with `rate_id, billing_code, negotiated_rate, negotiated_type, rate_unit, networks, service_code, pos_group, billing_code_modifier, billing_class, expiration_date, npi, provider_name, provider_type_group, specialty, tin, tin_name, tag_name, practice_city/state/zip5, scope_flag, n_sources, snapshot_date`. `summarize_by` = code, tin_code or npi_code (avg/median/min/max, counts). `sort_by` must be one of those column names |
| `percentage_rates(f, …)` | 8.4 | same columns, percentage rows only, plus `billed_charge` / `estimated_dollars` (empty for now) |
| `export_csv(f, path, summarize_by=None, percentage=False)` | CSV button | writes a CSV file, returns its row count (the frontend serves the file) |
| `map_points(f, code=None, max_points=5000)` | 8.5 | one point per ZIP centroid: `lat, lon, zip5, city, n_providers, n_tins, median_rate, market_percentile, has_platform, has_health_system, tag_names`, plus `unmapped_providers`, `capped` |
| `zip_providers(f, lat, lon, code=None)` | map popup | providers at one point with their median rate and TINs |
| `rate_type_composition(f, by_network=True)` | 8.6 | per code (× network) × negotiated_type: `n_tins, n_npis, n_rows` (ignores "dollar rates only") |
| `provider_profile(npi, f)` | 8.7 | `provider` (NPPES details), `tins`, `networks`, `per_code` (rate, distinct rates, market median, pct ranks, rate ratio), `rate_index` |
| `provider_list(f, page, page_size, sort_by, sort_dir)` | 8.7 list | unique NPIs: `name, provider_type_group, practice_city, n_codes, rate_index`; sort by rate_index, n_codes, name, n_npis |
| `billing_entity_profile(tin, f)` | 8.8 | `entity` (names, tag), `n_npis`, `provider_type_mix`, `per_code` (+ `uniform`, `max_distinct_rates`), `rate_index` |
| `entity_list(f, …)` | 8.8 list | unique TINs: `name, tag_name, n_codes, n_npis, rate_index` |
| `data_quality()` | 8.9 | `stats` (precomputed counts), `scope_by_code`, `top_taxonomies` behind ghost/non-BH flags, `file_rows` |
| `dq_rows(kind, page, …)` | 8.9 drill-down | rows behind a count; `kind` = zero_rate, invalid_npi, not_in_nppes, expired, conflicts, `scope:<flag>`, `file:<file_id>` |
| `dq_conflicts(limit)` | 8.9 | keys with more than one price in the same network |
| `source_rows(rate_id)` | source link (section 9) | every source: `file_name, url, schema_version, last_updated_on, sha256, snapshot_date, price_path, npi_path` |
| `capitation()` | extra | capitation payments with payee and covered codes |

Profiles ignore the location, provider-type, search, drill-down and tag filters, so a provider's own page never
filters the provider out. Codes, networks, POS, billing class and the toggles still apply.

## 5. Examples
```python
f = Filters(codes=("90837",), cities=("HOUSTON",))
backend.code_summary(f)["rows"][0]["p50"]                      # median 90837 rate for Houston BH providers (per TIN)
backend.benchmarks(f.replace(tag_names=("Headway", "Talkiatry")))["rows"]
page = backend.rate_explorer(f, page=1, page_size=200, sort_by="negotiated_rate", sort_dir="desc")
drill = backend.rate_explorer(f.replace(tins=("83-2675429",)))  # click a Headway summary row -> its rows
backend.source_rows(page["rows"][0]["rate_id"])                 # where that number came from
```

## 6. Performance
One read-only DuckDB connection, a cursor per call (safe from several threads), and an in-memory LRU cache of
recent results. `uv run rate-visualizer check-backend` opens the real `site.duckdb` with Render-like settings (1
thread, 1 GB) and times every call; timings are recorded in `rates_tool_hosting_plan.md` §10.8.

- **`rates_core`:** `build-site` also writes the rows allowed by the default toggles (except dollars-only) as a
  table ~10× smaller than `rates`. Each query uses it automatically when its filters are at least as strict as the
  defaults (`Filters.fits_core()`), otherwise the full table; the answers are identical (tested).
- **`warm_up()`:** `open_backend()` pre-computes the default views (all summaries, benchmarks, composition, first
  explorer page, filter options) before the site reports healthy.

## 7. Commands (laptop)
```
uv run rate-visualizer build-site                 # rates.duckdb (+ tags, map data) -> site.duckdb, with sanity checks
uv run rate-visualizer check-backend              # time every backend call on the new site.duckdb
uv run rate-visualizer publish-site --dry-run     # show what would be uploaded
uv run rate-visualizer publish-site               # upload to R2, write latest.json, keep 3 months, call deploy hook
```
`publish-site --local-store <folder>` publishes into a folder instead of R2 (testing).
