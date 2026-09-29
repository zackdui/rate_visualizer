# Rates data pipeline: exact algorithm (page 1 of "Notes on Rates Tool")

## Status and findings (updated as steps are built)
| Step | Status |
|---|---|
| 1. Index | Built and tested. All Test 1 numbers reproduced on the real index |
| 2–3. Extract | Built and tested. The fixture matches the independent `json.load` implementation row for row. Streaming from the URL gives the same SHA-256 and identical rows as the local copy |
| 2–3. Full TX run | Done (index 2026-08-20). All 36 files OK in 2,126 s with 4 workers, 12.1 GB streamed, max peak RAM 1.3 GB per worker, output 100 MB |
| 3b. Profile | Built and tested. `profile.txt` for the full TX run: 0 unfinished files |
| 4. Build DuckDB | Built and tested. `rates.duckdb` 2.5 GB in 823 s: rates_npi 35,397,412; rates_npi_dedup 16,898,337; capitation_npi 36; rates without a group 0 |
| 5, trace | Not built yet |

**Profile findings (full TX):** every price is `ffs` and `outpatient`, and none has a `billing_code_modifier`. The
7,651 prices with no `service_code` are all institutional (the CMS schema only requires it for professional).

**Build performance lessons:** (1) One `GROUP BY` over all 35 M rows with a `sources` list ran out of memory (list
aggregates can't spill). (2) Per-code passes worked, but with DuckDB allowed 13 GB on a 17 GB Mac, macOS thrashed and
the build took 7 h. (3) The final version gives DuckDB half of `max_memory_gb` (6.5 GB), 4 threads, and dedups in
passes of about 500k rows (split by billing_code and a hash of npi, both key columns, so the result is exact): 823 s,
peak 6.6 GB. The row checksums of every table matched the 7-hour build exactly.

**Full TX run totals:** 33,823 prices → 5,500,403 rate rows → 35,397,412 one-row-per-NPI rows (263,525 NPIs,
50,621 TINs). Prices by unit and class: dollars/professional 25,996; percent/institutional 4,247;
dollars/institutional 3,377; percent/professional 176; dollars-per-day/institutional 27. 27 capitation rows (26 Kelsey +
1 Sanitas), all covering 99205/99214/99215, $9.64–$225. **The only anomaly type is NPI `0` (173,993 occurrences). 1,861
of them are in provider groups the extracted rates use (1,861 groups each list one `0`).** No unknown keys, undefined
groups or unknown rate types in any file.

Findings while building (the tests below reflect them):
- **Allowed-amount files:** 6 unique, not 7. The 7th "distinct" location was the blank entry, which is an anomaly.
- **NPI `0` placeholders:** the fixture lists NPI `0` in 3 provider groups (e.g. group 400867 = `[0, 1720480627]`).
  They are kept verbatim and flagged as `npi_not_10_digits`, so the fixture has **3 anomalies, not 0**.
- **Scale test (MyBlue HMO, 475 MB streamed):** 20,816 items and 241,917 provider references (matches the earlier
  independent scan); 2,807 prices → 477,489 rate rows → 2,841,691 one-row-per-NPI rows (128,084 NPIs, 20,684 TINs);
  414 anomalies, all NPI `0`; output 4.5 MB. First version: 332 s. After storing temp provider groups as NPI lists
  (39.4 M NPI rows → one row per group): 282 s, identical output (0 differing rows), peak RAM ~1.1 GB.
  `extract --workers N` runs N files in parallel.
- Rate types seen in MyBlue (price level): negotiated/professional 2,421; percentage/institutional 210;
  negotiated/institutional 156; percentage/**professional** 17; per diem/institutional 3.
- **Added after review:** a `rate_unit` column (fixed mapping from `negotiated_type`) so percentages separate cleanly
  from dollars, and memory/storage limits (`max_memory_gb`, `max_storage_gb`, both 13 GB). Rates are now written in
  batches: MyBlue peak RAM went from 1.1 GB to 576 MB, 230 s, identical output.
- **Capitation files (27 TX):** 26 Kelsey "Cap Table" files (tables 1, 5, 6, 8–30) plus 1 Sanitas file. Each has one
  item `CAP`/`LOCAL`/`capitation` with one rate: Kelsey-Seybold Medical Group PA (TIN 76-0386391, org NPI
  1013915255), $80–$225 in $5 steps by table; Sanitas → Innovista Provider Group Texas PA (TIN 84-2579710, 10 NPIs),
  $9.64. Kelsey tables are linked to Blue Essentials HMO plans (1–55 plans each), Sanitas to 7 MyBlue Health plans.
- **Capitation decision (built):** every item with `negotiation_arrangement == "capitation"` goes, automatically and
  whatever its code, to its own `capitation.parquet` (never to `rates`). Each row has `covered_target_codes` (the
  configured codes in its `covered_services` with the configured type) and `n_covered_services`. The full coverage list
  is in `capitation_covered_services.parquet`, and payees are in `provider_groups`. Tested on the real Kelsey table 10
  file ($125.00 raw, covers 99205/99214/99215 of 12,428 services, payee NPI 1013915255).
- Output files and columns are documented in [OUTPUTS.md](OUTPUTS.md). Code files are described in [../README.md](../README.md).

## Context
Build the data layer for an internal rate-visualizer: extract BCBSTX in-network rates for 9 CPT codes into
queryable tables, with every number traceable to its exact source position. It must be modular (other states and HCSC
plans later), fully deterministic (no human or LLM judgment at runtime), and testable one step at a time. The frontend and
output tables (Tables 1–4, benchmarks, map) are a later phase. This plan only creates the data they need.

## Decisions already made (from Q&A)
| Topic | Decision |
|---|---|
| Scope, first build | 36 TX files only. The code also supports out-of-state files (`--all`) |
| Raw files | Streamed, not kept. SHA-256 of the streamed bytes is recorded |
| Codes | `billing_code` ∈ {99205, 99214, 99215, 90833, 90836, 90838, 90832, 90834, 90837} **and** `billing_code_type == "CPT"` |
| Row filters | None: every negotiated_type and billing_class kept. `$0` rows kept and flagged |
| Tables | Group-level tables plus a **one-row-per-NPI** table: network X, code Y, NPI Z under TIN W, price, conditions |
| Dedup | Second step. Merge rows whose values are all identical (network **not** in key, lists compared as sorted sets). Every source kept in a list |
| Network | Both, read verbatim: `network_name` (from the file) and index `description`(s). No cleanup |
| Out-of-state toggle | By source file (`is_in_state_file`) |
| Anomalies | Record to an anomalies table and continue. Tests assert 0 for TX |
| Provider groups kept | Only those referenced by the 9 codes. The total count is recorded |
| Storage | Parquet per source file → one DuckDB database |
| Index input | Per-state config: URL or local path |
| NPPES | Separate step. Auto-finds the latest monthly full file and the latest NUCC taxonomy file. Adds name, entity type (Type 1/2), specialty, and both addresses (mailing and practice location), each labelled |

## Facts the plan relies on (verified this session)
- Index `2026-08-20`: 63 `reporting_structure`, 142,006 plan rows, 7,079 in-network refs → **528 unique** (key = URL before `?`).
  **36** have `Blue-Cross-and-Blue-Shield-of-Texas_` in the filename. **38** files (all out-of-state) appear under >1 description.
  `allowed_amount_file` is a **list** here (the schema says object): 63 refs, 7 unique, 1 with a blank location.
- In-network files: always one object with one `provider_references` list and one `in_network` list. Versions seen: 1.2.0,
  2.0.0, 2.1.0, `3.5.5 <hash>` (TX). Key order varies. `provider_references` came first in every file checked, but the code
  must not depend on that. No inline `provider_groups` or remote provider references seen.
- `provider_group_id` is only unique **within a file**. The key is always `(file_id, provider_group_id)`.
- MRF files contain no NPI entity type. `tin.type` is `ein` or `npi`. Entity type must come from NPPES.

## Code layout (new package `src/rate_visualizer/`)
Reuse `download()` and `open_local()` from [bcbstx_mrf.py](bcbstx_mrf.py) (move them into `io.py`; `bcbstx_mrf.py` keeps working).
```
src/rate_visualizer/
  config.py      load configs/<state>.toml (tomllib)
  io.py          download(), open_local(), stream_url() with SHA-256 + byte-count wrapper, retries
  index.py       step 1
  extract.py     steps 2–3 (one file, single streaming pass)
  profile.py     step 3b report
  build_db.py    step 4 (DuckDB tables + NPI table + dedup)
  nppes.py       step 5 (separate)
  trace.py       re-stream a file and print the raw JSON at a stored path; checks SHA-256
  cli.py         `rate-visualizer <step>`; pyproject script already points at rate_visualizer:main
configs/tx.toml
tests/  fixtures/Blue-Essentials-295430.json.gz (1.6 MB, committed; BCBSTX will delete it upstream)
        fixtures/synthetic_*.json (tiny hand-built edge-case files)
data/   (git-ignored) data/<state>/<index_date>/...
```
New deps: `pyarrow`, `duckdb`; dev: `pytest`.

`configs/tx.toml` keys: `state="TX"`, `index_url`, `index_path` (optional, wins if set), `in_state_filename_marker="Blue-Cross-and-Blue-Shield-of-Texas_"`,
`codes=[…9…]`, `code_type="CPT"`, `scope="in_state"` (or `"all"`), `data_dir="data"`, `retries=3`.

---

## Step 1: Index → `index_files`, `index_plans`, `plan_files`
Command: `rate-visualizer index --config configs/tx.toml`
1. Get the index (path, or `download(index_url)` into `mrf_data/`). Stream `reporting_structure.item` with ijson.
   Read the top-level `reporting_entity_name`, `reporting_entity_type`, `version`, `last_updated_on` → `index_date`.
2. For each `reporting_structure[s]`:
   - Each `reporting_plans[q]` → **index_plans** row: `s, q, plan_name, issuer_name, plan_id_type, plan_id, plan_market_type`
     (plus `plan_sponsor_name` if present). Unknown keys → anomaly.
   - Each `in_network_files[f]` (`description`, `location`) → `file_key = location.split("?")[0]`,
     **plan_files** row: `s, f, file_kind="in_network", file_key, description`.
   - Each `allowed_amount_file` entry (accept object *or* list) → same, with `file_kind="allowed_amount"`. Blank location → anomaly.
3. **index_files** (one row per unique `file_key`): `file_id = sha1(file_key)[:12]`, `file_key`, `url` (full, including query),
   `file_name` (last path segment), `host`, `file_kind`, `descriptions` (sorted distinct list), `is_in_state_file`
   (= marker in `file_name`), `n_references`, `state`, `index_date`.
4. Write `data/TX/2026-08-20/index/{index_files,index_plans,plan_files,anomalies}.parquet`.

**Test 1:** 63 structures; 142,006 plan rows; 7,079 in-network refs → 528 unique; 36 `is_in_state_file`; 38 files with >1
description; allowed-amount 63 refs → 7 unique (1 blank → 1 anomaly). Only allowed-amount files are listed; they aren't parsed.

## Steps 2–3: Extract one file (single streaming pass)
Command: `rate-visualizer extract --config … [--file-id X | --in-state | --all] [--force]`. The file list comes from `index_files`.
Local-file mode for tests: `--local tests/fixtures/…gz`.

1. `stream_url(url)`: HTTP stream → SHA-256 + byte-count wrapper (on compressed bytes) → gzip → `ijson.parse`.
   On a network error: discard partial output and restart the file (up to `retries`).
2. One pass over events, using `ijson.ObjectBuilder` to build each object:
   - **Top-level scalars** → `file_meta`: `reporting_entity_name, reporting_entity_type, last_updated_on, version`, plus the
     order of top-level keys.
   - **Each `provider_references[m]`** (built fully): for each `provider_groups[n]`, for each `npi[p]` → append to a *temp*
     Parquet: `file_id, m, provider_group_id, network_name (list, verbatim), n, tin_type, tin_value, tin_business_name, p, npi`.
     A `location` key (remote reference) → anomaly (not fetched).
   - **Each `in_network[i]`** (built fully, because the code may come after the rates in key order): if `billing_code ∈ codes` and
     `billing_code_type == code_type`, then for each `negotiated_rates[j]`, for each `negotiated_prices[k]`, for each
     `provider_references[r]` → **rates** row (one per price × group reference):
     `file_id, i, billing_code, billing_code_type, billing_code_type_version, name, description, negotiation_arrangement,
     bundled_codes/covered_services (JSON text, if present), j, k, r, provider_group_id, negotiated_type,
     negotiated_rate (DOUBLE), negotiated_rate_raw (exact decimal text), expiration_date, billing_class, setting,
     service_code (list, original order), billing_code_modifier (list, original order), additional_information,
     is_zero_rate`. Add each referenced `provider_group_id` to a set.
     Inline `negotiated_rates[j].provider_groups[n]` (schema-valid, not yet seen) → written to provider groups with
     `provider_group_id = "inline:i:j:n"`.
   - Every non-matching item is still counted.
3. After the pass: keep the temp provider-group rows whose `provider_group_id` is in the referenced set → **provider_groups**.
   Referenced but undefined IDs → anomaly per ID.
4. Unknown keys at any level (checked against a known-key list per level) → anomaly `(type, key, json_path)`.
5. **file_meta** row: `file_id, file_key, url, sha256, compressed_bytes, last_updated_on, version, key_order,
   n_provider_references, n_provider_group_npi_rows_total, n_in_network_items, n_items_matched, n_rate_rows,
   n_zero_rates, n_groups_referenced, n_anomalies, parser_git_commit, started_at, finished_at`.
6. Write to `data/TX/<index_date>/files/<file_id>.tmp/`, then rename to `<file_id>/` and add `_SUCCESS`. Existing
   `_SUCCESS` → skip unless `--force` (lets a run resume).

Trace fields on every row give the paths `in_network[i].negotiated_rates[j].negotiated_prices[k]` (+ `.provider_references[r]`)
and `provider_references[m].provider_groups[n].npi[p]`, plus `file_key`, `sha256` and `last_updated_on`.

**Test 2–3 (fixture, compared with an independent `json.load` implementation):** 316 price rows; 1,903 rate rows
(price × group); 164 of 2,036 groups referenced; 726 unique NPIs; 65 TINs; 7 zero rates; `n_in_network_items` = 12,763;
0 anomalies; the set of extracted tuples equals the independent implementation's exactly.
**Synthetic tests:** `in_network` before `provider_references`; shuffled key order; inline `provider_groups`; undefined
group ID; remote `location`; unknown key; code with the right number but a non-CPT type (excluded). Each gives the expected
rows and anomalies.

## Step 3b: Profile (what types actually appear)
Command: `rate-visualizer profile`. Reads the Parquet and prints counts by `negotiated_type × billing_class × billing_code`,
`negotiation_arrangement`, `setting`, `service_code` sets, zero rates, and anomalies by type. It is used to decide per-type
handling before building the frontend tables.
**Test:** the fixture gives `negotiated/professional = 307` and `percentage/institutional = 9`.

## Step 4: Build DuckDB
Command: `rate-visualizer build-db` → `data/TX/<index_date>/rates.duckdb`
- Load `index_*`, `file_meta`, `anomalies`, `provider_groups`, and `rates` from all `_SUCCESS` files.
- **rates_npi** (one row per NPI; the answer to "network X, code Y, NPI Z under TIN W gets $…"): `rates ⋈ provider_groups ON
  (file_id, provider_group_id)`, adding `state`, `is_in_state_file`, `descriptions`, `network_name`, and all trace fields.
- **rates_npi_dedup**: `GROUP BY` the value key = `billing_code, billing_code_type, billing_code_type_version,
  negotiation_arrangement, name, description, tin_type, tin_value, tin_business_name, npi, negotiated_type,
  negotiated_rate_raw, billing_class, setting, sorted(service_code), sorted(billing_code_modifier), expiration_date,
  additional_information`. Aggregates: `networks` (distinct network_names), `is_in_state_any`, `n_sources`, and
  `sources` = list of struct(file_id, file_key, network_name, descriptions, i, j, k, r, m, n, p).

**Test 4:** fixture → `rates_npi` = 9,276 rows (per code: 90832 1,286; 90833 945; 90834 925; 90836 1,111; 90837 1,284;
90838 743; 99205 992; 99214 998; 99215 992); `rates_npi_dedup` = 9,276. Synthetic: the same row from 2 files in 2 networks
→ 1 row, `n_sources = 2`, both networks listed.

## Step 5: NPPES (separate command, adds to the same DuckDB)
Command: `rate-visualizer nppes`
1. Auto-find the file: fetch CMS's NPPES download page and regex-select the newest `NPPES_Data_Dissemination_<Month>_<Year>_V2.zip`
   (monthly full, not weekly) by parsed month/year. Download it to `data/nppes/` (zip needs random access, ~1 GB).
   Auto-find the newest NUCC taxonomy CSV the same way. Record the chosen file names and SHA-256 in `nppes_meta`.
2. Stream the `npidata_pfile_*.csv` member (not `_fileheader`). Check the required headers exist; fail loudly if any are missing.
   Keep only NPIs found in `provider_groups.npi` or in `tin_value` where `tin_type='npi'`.
3. **nppes** table: `npi, entity_type (1/2), org_name, last_name, first_name, middle_name, credential,
   mailing_address_1/2, mailing_city, mailing_state, mailing_zip, mailing_phone,
   practice_address_1/2, practice_city, practice_state, practice_zip, practice_phone,
   primary_taxonomy_code, specialty_grouping, specialty_classification, specialty_specialization, specialty_display,
   deactivation_date, last_update_date`.
   Primary taxonomy rule (deterministic): the one taxonomy with switch `Y`. Otherwise, if exactly one taxonomy is listed,
   use it. Otherwise NULL plus an anomaly.
4. View **rates_npi_named** = `rates_npi LEFT JOIN nppes USING (npi)`. NPIs not found → NULLs, counted in `nppes_meta`.

**Test 5:** header check passes; match rate reported; 5 random NPIs match the public NPI Registry API (name, entity type,
practice state).

## Run order and verification
1. `uv run pytest` (steps 1–4 on fixture + synthetic; offline).
2. `rate-visualizer index` → check the Test 1 numbers.
3. `extract --local tests/fixtures/…` → `profile` → `build-db` (the fixture DB); query `rates_npi` in DuckDB by hand.
4. Scale test: `extract --file-id <MyBlue HMO>` (475 MB). Expect `n_in_network_items` = 20,816 and
   `n_provider_references` = 241,917 (matches the earlier full scan), 0 anomalies. Record runtime, peak memory and output size.
5. `extract --in-state` (36 files, ~35–40 min), then `profile`, `build-db`, `nppes`.
6. `rate-visualizer trace <file_id> <path>` on a few random `rates_npi` rows. The printed raw JSON matches the row and the
   SHA-256 matches, while the month's files are still online.
