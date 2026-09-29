# rate_visualizer

Extracts insurer in-network rates for selected billing codes from Transparency in Coverage machine-readable files
(starting with Blue Cross and Blue Shield of Texas). Every number can be traced back to its exact position in the
source file. The algorithm and all design decisions are in [docs/PLAN.md](docs/PLAN.md). Every output table and
column is described in [docs/OUTPUTS.md](docs/OUTPUTS.md).

## Quick start
```
uv sync                                                    # install dependencies
uv run pytest -q                                           # all tests (offline)
uv run rate-visualizer index   --config configs/tx.toml    # step 1: read the index
uv run rate-visualizer extract --config configs/tx.toml --file-id <file_id>   # steps 2-3: one file
uv run rate-visualizer extract --config configs/tx.toml --in-state --workers 4 # steps 2-3: all 36 TX files
uv run rate-visualizer profile                                                # step 3b: report -> profile.txt
uv run rate-visualizer build-db                                               # step 4: -> rates.duckdb
uv run rate-visualizer nppes                                                  # step 5: names, specialty, addresses
uv run rate-visualizer trace --npi <NPI> --code 90837                         # verify rows against the raw file
uv run rate-visualizer build-site                                             # site.duckdb for the website
uv run rate-visualizer check-backend                                          # time every backend call
uv run rate-visualizer publish-site                                           # upload to R2 + restart Render
uv run rate-visualizer site                                                   # run the website locally (port 8080)
```
To run the website locally without R2, put `SITE_DB_PATH=data/TX/2026-08-20/site.duckdb` in `.env`.
`extract` skips files that already finished (they have `_SUCCESS`); `--force` redoes them. `--local <path>` parses a
local copy instead of streaming (matched to the index by file name). `--workers N` processes N files in parallel
(a 475 MB file took about 4 minutes and 576 MB of RAM on one worker).

## Safety limits
Set in the config (`configs/tx.toml`), both default to 13 GB:
- `max_memory_gb`: RAM for all extract workers together. With `--workers N`, each worker may use `max_memory_gb / N`.
  Every file runs in a fresh process, which checks its own peak memory after every `in_network` item, every 1,000
  provider references and after the final filter step. DuckDB inside a worker is capped at half the worker's share.
- `max_storage_gb`: total size of everything under `data_dir` (finished outputs and temporary files). Checked before
  each file starts, at every batch written, and after the filter step.

`build-db` gives DuckDB half of `max_memory_gb` (6.5 GB) and 4 threads, spills to `data/…/duckdb_spill/` (counted
toward storage), and deduplicates in passes of about 500k rows. The full TX build takes about 14 minutes and ends at
2.5 GB. If it hits a limit, the partial database is deleted and the previous `rates.duckdb` is left untouched.

A file that goes over either limit is **stopped**: its partial output (`<file_id>.tmp/`) is deleted, it isn't retried,
and it's reported as failed. Other files carry on, and later files won't start if storage is already over the limit.
Memory is measured between items, so one enormous item could overshoot briefly before it's caught. That's why the
per-worker share should leave headroom.

## Pipeline status
| Step | Command | Status |
|---|---|---|
| 1. Index → files, plans, plan↔file links | `index` | built, tested |
| 2–3. Extract rates + provider groups per file | `extract` | built, tested |
| 3b. Profile (which rate types appear) | `profile` | built, tested |
| 4. Build DuckDB (one row per NPI, dedup) | `build-db` | built, tested |
| 5. NPPES names / specialty / addresses | `nppes` | built, tested |
| Trace a row back to the raw JSON | `trace` | built, tested |
| Site database for the website | `build-site` | built, tested |
| Upload to Cloudflare R2 + restart Render | `publish-site` | built, tested (against a local folder; needs R2 keys for real) |
| Website backend (all queries, filters, loader) | `backend` package, `check-backend` | built, tested |
| Website frontend (NiceGUI page) | `site` | built, tested |

## Files in this repo
**Configuration**
- `.env.example`: template for the secrets file `.env` (git-ignored).
- `configs/entity_tags_tx.csv`: platform and health-system tags per TIN.
- `configs/tx.toml`: per-state settings: index URL/path, in-state filename marker, the billing codes to extract,
  code type, scope (`in_state`/`all`), output folder, retries. Copy it for another state or payer.

**Package `src/rate_visualizer/`**
- `cli.py`: the `rate-visualizer` command. Chooses which files to process, runs them in parallel (`--workers`),
  retries network failures, prints a summary.
- `config.py`: loads and validates a config TOML (rejects missing or unknown keys).
- `io.py`: shared file access: `download()`, `open_local()`, `open_source()` (streams a URL or local file, gunzips,
  and computes the SHA-256 of the bytes), `file_key()` (URL without `?signature`), and Parquet writing.
- `jsonstream.py`: walks a huge top-level JSON object one piece at a time with ijson, whatever order the keys are in.
  Numbers stay `Decimal`, so the exact text is preserved. It can skip list items without building them (used by
  `trace`).
- `index.py`: step 1. Parses the index into `index_meta`, `index_files`, `index_plans`, `plan_files`, `anomalies`.
- `extract.py`: steps 2–3. Parses one in-network file in a single pass into `rates` (per-visit prices for the
  configured codes), `capitation` + `capitation_covered_services` (fixed per-member payments and the codes they
  cover), `provider_groups`, `file_meta` and `anomalies`. It also enforces the memory and storage limits.
- `profile.py`: step 3b. Counts rate types, arrangements, places of service, modifiers, zero rates, anomalies and
  capitation across all finished files, and saves `profile.txt`.
- `build_db.py`: step 4. Loads everything into `rates.duckdb` and builds `rates_npi` (one row per NPI),
  `rates_npi_dedup` and `capitation_npi`, within the memory and storage limits.
- `nppes.py`: step 5. Finds the newest NPPES monthly file and NUCC taxonomy, streams the NPPES CSV out of the zip
  for the NPIs in the database, picks each NPI's primary specialty by a fixed rule, and adds `nppes`, `taxonomy` and
  the `*_named` views to `rates.duckdb`.
- `trace.py`: re-streams a source file and prints the raw JSON at given paths, or verifies `rates_npi` rows for an
  NPI against the raw file (values and SHA-256).
- `__init__.py`: exposes `main` for the command-line entry point.

**Website data, laptop side: `src/rate_visualizer/sitebuild/`**
- `build_site.py`: `build-site`. Turns `rates.duckdb` + `configs/entity_tags_tx.csv` + map data into
  `site.duckdb` (rates with provider type, location, tags and ghost-rule `scope_flag`; providers with map points;
  TINs with display names; sources; data-quality tables), then checks it against last month.
- `geo.py`: downloads and loads the Census ZCTA gazetteer and HRSA ZIP-to-ZCTA crosswalk.
- `publish.py`: `publish-site`. Uploads `site.duckdb` to R2, writes `latest.json` last, keeps the newest months,
  calls the Render deploy hook.

**Website frontend: `src/rate_visualizer/frontend/`** (imports only `rate_visualizer.backend`)
- `app.py`: startup (loads the data in the background), `/healthz`, the page (sticky header, nav, snapshot selector,
  dark mode) and the `site` command.
- `state.py`: each visitor's filters, pinned benchmark entities, debounced refresh, drill-down and navigation.
- `filterbar.py`: the global filter bar (type-to-search multi-selects, search, toggles, counting unit).
- `theme.py`: teal accent, Inter font, light/dark CSS, number formatting.
- `sections/`: `summary.py` (Code Summary + histograms, Benchmarks), `tables.py` (Rate Explorer, Percentage Rates),
  `map_view.py` (Map), `composition.py` (Rate Type Composition), `profiles.py` (Provider and Billing Entity
  profiles), `quality.py` (Data Quality).

**Website backend: `src/rate_visualizer/backend/`** (the only package the frontend may import; see
[docs/backend_api.md](docs/backend_api.md))
- `schema.py`: shared contract: table names, provider-type and ghost-rule SQL, labels for codes and places of service.
- `filters.py`: `Filters`, every filter and toggle as one immutable object that renders parameterised SQL.
- `queries.py`: `Backend`, one method per page section; returns plain data; cached.
- `settings.py`, `storage.py`, `loader.py`: environment variables / `.env`, R2 or local-folder storage, and the
  startup download with SHA-256 check.

**Tests `tests/`**
- `test_index.py`: step 1 on a synthetic index (edge cases) and the real 2026-08-20 BCBSTX index (expected counts).
- `test_extract.py`: steps 2–3 on the real fixture, compared row by row with an independent `json.load`
  implementation; synthetic edge cases (key order, inline groups, undefined/remote references, unknown keys,
  non-CPT codes); SHA-256 and re-run behaviour; capitation; memory and storage limits.
- `test_nppes.py`: step 5. File discovery rules, the primary-taxonomy rule, and the full step on the fixture
  database with a small hand-built NPPES zip and NUCC CSV (including survival across a rebuild).
- `test_trace.py`: path parsing, skipping unneeded items, raw-path lookup with SHA-256 check, row verification,
  and detection of a changed file.
- `test_site.py`: build-site, every backend method and filter (checked against direct SQL), CSV export, source
  tracing, publish to a local folder, download verification, month retention, settings.
- `test_frontend.py`: front/back boundary rules, and the real `site` command serving the page and `/healthz`.
- `test_build.py`: steps 3b and 4. Profile and build on the fixtures (expected counts), dedup across files and
  networks, and the storage limit.
- `fixtures/2026-08-17_…_Blue-Essentials-295430_in-network-rates.json.gz`: a real 1.6 MB BCBSTX file that contains
  all 9 codes. `fixtures/2026-08-13_…_TX-Kelsey-Cap-Table-10_in-network-rates.json.gz`: a real 0.2 MB capitation
  file. Both are committed because BCBSTX deletes old files every month.

**Docs `docs/`**
- `PLAN.md`: the approved algorithm, decisions and test expectations.
- `OUTPUTS.md`: what every output file and column contains, and how to join and trace them.
- `backend_api.md`: the website backend's API for the frontend.
- `rates_tool_plan.md`, `rates_tool_hosting_plan.md`, `entity_matching.md`: the product plan, hosting/setup plan,
  and how platforms and health systems were matched.

**Generated, git-ignored**
- `mrf_data/`: downloaded index file(s).
- `data/<state>/<index_date>/`: all pipeline outputs (see OUTPUTS.md).

**Earlier material (unchanged)**
- `bcbstx_mrf.py`, `testing.py`: the original exploration scripts.
- `2026-08-20_Blue-Cross-and-Blue-Shield-of-Texas_index.json`: the index file (a copy also lives in `mrf_data/`).
- `CMS-Transparency-in-Coverage-9915F.pdf`: the CMS final rule. `mrf-download-instructions-tx.pdf`: the BCBSTX download guide.
