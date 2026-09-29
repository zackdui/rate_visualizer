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
```
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
| 5. NPPES names / specialty / addresses | `nppes` | not built yet |
| Trace a row back to the raw JSON | `trace` | not built yet |

## Files in this repo
**Configuration**
- `configs/tx.toml`: per-state settings: index URL/path, in-state filename marker, the billing codes to extract,
  code type, scope (`in_state`/`all`), output folder, retries. Copy it for another state or payer.

**Package `src/rate_visualizer/`**
- `cli.py`: the `rate-visualizer` command. Chooses which files to process, runs them in parallel (`--workers`),
  retries network failures, prints a summary.
- `config.py`: loads and validates a config TOML (rejects missing or unknown keys).
- `io.py`: shared file access: `download()`, `open_local()`, `open_source()` (streams a URL or local file, gunzips,
  and computes the SHA-256 of the bytes), `file_key()` (URL without `?signature`), and Parquet writing.
- `jsonstream.py`: walks a huge top-level JSON object one piece at a time with ijson, whatever order the keys are in.
  Numbers stay `Decimal`, so the exact text is preserved.
- `index.py`: step 1. Parses the index into `index_meta`, `index_files`, `index_plans`, `plan_files`, `anomalies`.
- `extract.py`: steps 2–3. Parses one in-network file in a single pass into `rates` (per-visit prices for the
  configured codes), `capitation` + `capitation_covered_services` (fixed per-member payments and the codes they
  cover), `provider_groups`, `file_meta` and `anomalies`. It also enforces the memory and storage limits.
- `profile.py`: step 3b. Counts rate types, arrangements, places of service, modifiers, zero rates, anomalies and
  capitation across all finished files, and saves `profile.txt`.
- `build_db.py`: step 4. Loads everything into `rates.duckdb` and builds `rates_npi` (one row per NPI),
  `rates_npi_dedup` and `capitation_npi`, within the memory and storage limits.
- `__init__.py`: exposes `main` for the command-line entry point.

**Tests `tests/`**
- `test_index.py`: step 1 on a synthetic index (edge cases) and the real 2026-08-20 BCBSTX index (expected counts).
- `test_extract.py`: steps 2–3 on the real fixture, compared row by row with an independent `json.load`
  implementation; synthetic edge cases (key order, inline groups, undefined/remote references, unknown keys,
  non-CPT codes); SHA-256 and re-run behaviour; capitation; memory and storage limits.
- `test_build.py`: steps 3b and 4. Profile and build on the fixtures (expected counts), dedup across files and
  networks, and the storage limit.
- `fixtures/2026-08-17_…_Blue-Essentials-295430_in-network-rates.json.gz`: a real 1.6 MB BCBSTX file that contains
  all 9 codes. `fixtures/2026-08-13_…_TX-Kelsey-Cap-Table-10_in-network-rates.json.gz`: a real 0.2 MB capitation
  file. Both are committed because BCBSTX deletes old files every month.

**Docs `docs/`**
- `PLAN.md`: the approved algorithm, decisions and test expectations.
- `OUTPUTS.md`: what every output file and column contains, and how to join and trace them.

**Generated, git-ignored**
- `mrf_data/`: downloaded index file(s).
- `data/<state>/<index_date>/`: all pipeline outputs (see OUTPUTS.md).

**Earlier material (unchanged)**
- `bcbstx_mrf.py`, `testing.py`: the original exploration scripts.
- `2026-08-20_Blue-Cross-and-Blue-Shield-of-Texas_index.json`: the index file (a copy also lives in `mrf_data/`).
- `CMS-Transparency-in-Coverage-9915F.pdf`: the CMS final rule. `mrf-download-instructions-tx.pdf`: the BCBSTX download guide.
