# Output files

All outputs are Parquet (zstd). You can open any of them with DuckDB, e.g.
`duckdb -c "select * from 'data/TX/2026-08-20/files/*/rates.parquet' limit 10"`, or with pandas/pyarrow.

```
data/<state>/<index_date>/                 e.g. data/TX/2026-08-20/  (index_date = the index's last_updated_on)
  index/                                   step 1
    index_meta.parquet
    index_files.parquet
    index_plans.parquet
    plan_files.parquet
    anomalies.parquet
  files/<file_id>/                         steps 2-3, one folder per in-network file
    rates.parquet
    capitation.parquet
    capitation_covered_services.parquet
    provider_groups.parquet
    file_meta.parquet
    anomalies.parquet
    _SUCCESS                               empty; written last. No _SUCCESS = incomplete (it is redone)
  files/<file_id>.tmp/                     only exists while a file is being parsed (or after a crash)
  profile.txt                              step 3b report
  rates.duckdb                             step 4 database (+ step 5 tables and views)
  nppes/                                   step 5
    nppes.parquet, taxonomy.parquet, nppes_meta.parquet, anomalies.parquet
data/nppes/                                downloaded NPPES zip + NUCC CSV (shared by all runs)
```

**Values are copied verbatim.** Strings are never cleaned or renamed. Numbers are stored as text where exactness
matters (`negotiated_rate_raw`), and IDs (NPI, TIN, provider_group_id, plan_id) are stored as strings.

**Position columns** (`s`, `q`, `f`, `i`, `j`, `k`, `r`, `m`, `n`, `p`) are 0-based list positions in the source JSON.
Together with the file's `sha256` they identify exactly where a value came from (see "Tracing a number" below).

---

## Step 1: `index/`

### index_meta.parquet (1 row)
| Column | Meaning |
|---|---|
| state | from the config (e.g. `TX`) |
| index_source | path of the index file that was parsed |
| index_sha256, index_bytes | SHA-256 and size of that index file |
| config_source | config file used |
| reporting_entity_name, reporting_entity_type, version, last_updated_on | top-level values from the index |
| key_order | the index's top-level keys, in file order |
| n_reporting_structures, n_plan_rows, n_file_references, n_unique_files, n_anomalies | counts |

### index_files.parquet (1 row per unique file)
Key: `file_id`.
| Column | Meaning |
|---|---|
| file_id | first 12 hex characters of SHA-1(`file_key`). Used as the folder name in `files/` |
| file_key | the file's URL without the `?…` query string (signatures and expiry change; the file doesn't) |
| url | full URL including the query string, from its first appearance in the index (needed to download signed files) |
| file_name | last part of the URL path |
| host | URL host |
| file_kind | `in_network` or `allowed_amount` |
| descriptions | every distinct `description` the index gives this file, sorted (38 out-of-state files have 2) |
| is_in_state_file | `true` if `file_name` contains the config's `in_state_filename_marker` |
| n_references | how many times the index lists this file |
| state, index_date | from the config / index |

### index_plans.parquet (1 row per plan entry)
Key: `(s, q)`.
| Column | Meaning |
|---|---|
| s | position in `reporting_structure` |
| q | position in that structure's `reporting_plans` |
| plan_name, issuer_name, plan_id_type, plan_id, plan_market_type, plan_sponsor_name | verbatim; missing → null |

### plan_files.parquet (1 row per file reference in the index)
Joins plans to files: `index_plans.s = plan_files.s`, `plan_files.file_id = index_files.file_id`.
| Column | Meaning |
|---|---|
| s | position in `reporting_structure` |
| f | position in that structure's `in_network_files` / `allowed_amount_file` list |
| file_kind | `in_network` or `allowed_amount` |
| file_id, file_key | the file |
| description | the description on this particular reference |

### anomalies.parquet
Same columns as the per-file anomalies (below), with `step = "index"`. Anomaly types in step 1:
`unknown_key`, `blank_location` (a file entry with an empty location; not added to `index_files`),
`file_kind_conflict` (the same file listed as both in-network and allowed-amount).

---

## Steps 2–3: `files/<file_id>/`

### rates.parquet
One row per **price × provider-group reference** for the configured codes:
`in_network[i].negotiated_rates[j].negotiated_prices[k]` × `…negotiated_rates[j].provider_references[r]`.
Join to NPIs with `provider_groups` on `(file_id, provider_group_id)`.

| Column | Meaning |
|---|---|
| file_id | source file |
| i | position in `in_network` |
| billing_code, billing_code_type, billing_code_type_version | e.g. `90837`, `CPT`, `2026` |
| name, description | the file's short name and plain-language description of the code |
| negotiation_arrangement | `ffs` (fee for service), `bundle`, `capitation`, … |
| bundled_codes, covered_services | JSON text if present (bundles/capitation), else null |
| j | position in `negotiated_rates` |
| k | position in `negotiated_prices` |
| r | position in that rate's `provider_references` list; null for inline provider groups |
| provider_group_id | the referenced group ID as text; inline groups get `inline:<i>:<j>:<n>` |
| negotiated_type | `negotiated` (dollars), `percentage` (percent of billed charges, **not dollars**), `per diem`, `fee schedule`, `derived` |
| negotiated_rate | the number as a DOUBLE (for maths) |
| negotiated_rate_raw | the number exactly as written in the file (use for exact comparison and dedup) |
| rate_unit | what the number means, from a fixed mapping of `negotiated_type`: `negotiated`/`derived`/`fee schedule` → `dollars`; `percentage` → `percent_of_billed_charges` (e.g. 68.0 = 68% of the provider's billed charge, **not $68**); `per diem` → `dollars_per_day`; anything else → `unknown` (+ anomaly). Filter on this to separate percentages from dollar amounts |
| expiration_date | contract expiration (`2999-12-31` = no end date) |
| billing_class | `professional` (physician fee) or `institutional` (facility fee) |
| setting | `outpatient`, `inpatient`, `both` |
| service_code | list of place-of-service codes, in file order (e.g. `11` office, `02`/`10` telehealth) |
| billing_code_modifier | list of modifiers, in file order |
| additional_information | free text, if present |
| is_zero_rate | `true` when the rate is 0 (kept, flagged) |

Items with `negotiation_arrangement = "capitation"` are **never** in `rates` (even if their own billing code is one of
the configured codes); they go to `capitation.parquet`.

### capitation.parquet
Capitation = a fixed payment to a provider group per enrolled member (the CMS rule describes it as per member per
month), covering a list of services, whether or not the member uses them. It is **not a per-visit price** and
shouldn't be mixed into rate statistics. Every `in_network` item with `negotiation_arrangement = "capitation"` is
extracted automatically, whatever its billing code. One row per price × provider-group reference, like `rates`.

Columns: the same as `rates` (`file_id, i, billing_code, …, j, k, r, provider_group_id, negotiated_type,
negotiated_rate, negotiated_rate_raw, rate_unit, …, is_zero_rate`) **except** `covered_services`, plus:
| Column | Meaning |
|---|---|
| covered_target_codes | the configured codes (e.g. 99205, 99214, 99215) that appear in the item's `covered_services` with the configured `code_type`; sorted; `[]` if none |
| n_covered_services | total number of services the payment covers |

In BCBSTX files the item is `billing_code "CAP"`, `billing_code_type "LOCAL"`. Join to the payee with
`provider_groups` on `(file_id, provider_group_id)`, and to plans with `index/plan_files` on `file_id`.

### capitation_covered_services.parquet
One row per entry of a capitation item's `covered_services` (path `in_network[i].covered_services[c]`).
| Column | Meaning |
|---|---|
| file_id, i | the capitation item (join to `capitation` on `file_id, i`) |
| c | position in `covered_services` |
| billing_code_type, billing_code_type_version, billing_code, description | verbatim |
| is_target_code | `true` if this entry is one of the configured codes with the configured `code_type` |

### provider_groups.parquet
One row per **NPI** of every provider group that `rates` or `capitation` references (unreferenced groups are dropped;
their total count is in `file_meta`). Path: `provider_references[m].provider_groups[n].npi[p]`.
| Column | Meaning |
|---|---|
| file_id | source file |
| m | position in the top-level `provider_references` (null for inline groups) |
| provider_group_id | group ID as text (unique only **within this file**) |
| network_name | list of network names on that provider reference, verbatim (e.g. `["Blue Essentials"]`) |
| n | position in that reference's `provider_groups` |
| tin_type | `ein` or `npi` |
| tin_value | the TIN (billing entity), verbatim |
| tin_business_name | business name on the TIN, if given |
| p | position in that group's `npi` list |
| npi | the NPI as text. Non-10-digit values (e.g. `0` placeholders) are kept and flagged in anomalies |

### file_meta.parquet (1 row)
| Column | Meaning |
|---|---|
| file_id, file_key, url | identity (`url` is null for a local run) |
| source | the URL or absolute local path actually read |
| sha256, compressed_bytes | SHA-256 and size of the exact bytes read (the .gz). Proves which version was parsed |
| reporting_entity_name, reporting_entity_type, last_updated_on, version | the file's header values |
| key_order | the file's top-level keys, in file order |
| codes, code_type | the filter used |
| n_provider_references | entries in `provider_references` |
| n_provider_group_npi_rows_total | NPI rows across **all** groups, before dropping unreferenced ones |
| n_in_network_items, n_items_matched | all `in_network` entries / entries matching the codes |
| n_price_rows | matching `negotiated_prices` entries (before × group references) |
| n_zero_price_rows | of those, how many have rate 0 |
| n_rate_rows | rows in `rates.parquet` |
| n_groups_referenced | distinct provider_group_ids referenced by those rates |
| n_provider_group_npi_rows_kept | rows in `provider_groups.parquet` |
| n_capitation_items | capitation items in the file |
| n_capitation_items_with_target_codes | of those, how many cover at least one configured code |
| n_capitation_rows, n_capitation_covered_services | rows in `capitation.parquet` / `capitation_covered_services.parquet` |
| n_anomalies | rows in `anomalies.parquet` |
| peak_rss_mb | peak memory used by the process that parsed this file |
| memory_limit_mb, storage_limit_gb | the limits in force for this run (see "Safety limits" in the README) |
| parser_git_commit, parser_git_dirty | code version that produced the output (dirty = uncommitted changes) |
| started_at, finished_at | UTC timestamps |

### anomalies.parquet
| Column | Meaning |
|---|---|
| step | `index` or `extract` |
| file_id | source file (null in step 1) |
| type | see below |
| key | the key or value involved |
| json_path | where in the source JSON |
| detail | extra context (the offending value, truncated) |

Anomaly types in steps 2–3: `unknown_key` (a field not in the expected schema), `not_an_object`, `not_a_list`,
`missing_tin`, `missing_npi`, `npi_not_10_digits`, `missing_provider_group_id`, `remote_provider_reference` (a
provider reference given as a URL; not fetched), `undefined_provider_group` (a rate references a group ID the file
never defines), `capitation_without_covered_services`, `rate_without_providers`, `rate_without_prices`, `rate_not_a_number`, `unknown_negotiated_type`
(a `negotiated_type` outside the CMS list; its `rate_unit` is `unknown`).

---

---

## Step 3b: `profile.txt`
Plain-text report written by `rate-visualizer profile` to `data/<state>/<index_date>/profile.txt`. It counts prices
(one `negotiated_prices` entry = `file_id, i, j, k`) by `negotiated_type × rate_unit × billing_class`, by billing
code, arrangement, setting, place-of-service set and modifier set, plus zero rates, anomalies by type, non-10-digit
NPIs in used groups, a capitation summary and per-file counts. It also lists in-scope files that haven't finished.

---

## Step 4: `rates.duckdb`
`rate-visualizer build-db` writes `data/<state>/<index_date>/rates.duckdb` (built as `rates.duckdb.tmp` and renamed
only on success). It loads only folders with `_SUCCESS`. Open it with `duckdb data/TX/2026-08-20/rates.duckdb`.

| Table | Rows are | Built from |
|---|---|---|
| index_meta, index_files, index_plans, plan_files | as in step 1 | `index/*.parquet` |
| file_meta, provider_groups, rates, capitation, capitation_covered_services | as in steps 2–3, all files stacked | `files/*/…parquet` |
| anomalies | index + extract anomalies (`step` column tells which) | both |
| **rates_npi** | one row per NPI: network X, code Y, NPI Z under TIN W gets this price under these conditions | `rates ⋈ provider_groups ⋈ file_meta ⟕ index_files` |
| **rates_npi_dedup** | `rates_npi` with identical rows merged, keeping every source | `rates_npi` |
| **capitation_npi** | capitation payments joined to their payee NPIs | `capitation ⋈ provider_groups ⋈ file_meta ⟕ index_files` |
| build_meta | 1 row: state, index_date, n_files, started/finished, code version | build |

### rates_npi columns
`state, file_id, file_name, is_in_state_file, file_descriptions, network_name` (source file and network),
`billing_code, billing_code_type, billing_code_type_version, name, description, negotiation_arrangement,
bundled_codes, covered_services` (the code), `tin_type, tin_value, tin_business_name, npi, is_valid_npi` (who;
`is_valid_npi` is false for placeholders like `0`, which are kept), `negotiated_type, negotiated_rate,
negotiated_rate_raw, rate_unit, is_zero_rate, billing_class, setting, service_code, billing_code_modifier,
expiration_date, additional_information` (the price and conditions), and trace columns `provider_group_id, i, j, k,
r, m, n, p, file_sha256, file_last_updated_on`. Sorted by `billing_code, rate_unit, billing_class,
negotiated_rate, npi`, so filters on those are fast.

### rates_npi_dedup
Rows of `rates_npi` are merged when **all** of these are identical: `billing_code, billing_code_type,
billing_code_type_version, negotiation_arrangement, name, description, bundled_codes, covered_services, tin_type,
tin_value, tin_business_name, npi, is_valid_npi, negotiated_type, negotiated_rate_raw, rate_unit, is_zero_rate,
billing_class, setting, expiration_date, additional_information`, and `service_code` / `billing_code_modifier`
compared as sorted sets. The network and source file are **not** part of the key. (It's built in passes split by
`billing_code` and a hash of `npi`. Both are key columns, so rows in different passes can never merge, and the
result is the same as one big `GROUP BY`.) Extra columns:
| Column | Meaning |
|---|---|
| service_code, billing_code_modifier | the sorted lists |
| negotiated_rate | the number (identical for all merged rows, since `negotiated_rate_raw` is in the key) |
| networks | every distinct `network_name` among the merged rows, sorted |
| is_in_state_any / is_in_state_all | whether any / all merged rows came from in-state files |
| n_sources | how many `rates_npi` rows were merged |
| sources | list of {file_id, network_name, provider_group_id, i, j, k, r, m, n, p, service_code, billing_code_modifier (original order)}: every source, traceable. File descriptions come from `index_files` via `file_id` |

---

## Step 5: `nppes/` and the `*_named` views
`rate-visualizer nppes` finds and downloads (into `data/nppes/`) the newest monthly full NPPES file and the newest
NUCC taxonomy CSV. It streams the NPPES CSV straight out of the zip and keeps only NPIs that appear in the database:
`provider_groups.npi`, plus `tin_value` where `tin_type = 'npi'`. It writes `data/<state>/<index_date>/nppes/*.parquet`
and loads them into `rates.duckdb`. `build-db` reloads them automatically, so a rebuild keeps them.

### nppes (table; one row per NPI found)
| Column | Meaning |
|---|---|
| npi | the NPI |
| entity_type_code, entity_type | `1` = `individual`, `2` = `organization` |
| provider_name | organisation name for type 2; "first middle last suffix" for type 1 |
| org_name, last_name, first_name, middle_name, name_prefix, name_suffix, credential | verbatim NPPES fields |
| mailing_address_1/2, mailing_city, mailing_state, mailing_zip, mailing_phone | **mailing** address (often a billing office) |
| practice_address_1/2, practice_city, practice_state, practice_zip, practice_phone | **primary practice location** (where care is given; use for maps) |
| enumeration_date, last_update_date, deactivation_date, reactivation_date | NPPES dates (text as published) |
| taxonomy_codes | all listed taxonomy codes (up to 15) |
| primary_taxonomy_code | chosen by a fixed rule: the one code with primary switch `Y`; otherwise, if exactly one code is listed, that one; otherwise null |
| primary_taxonomy_rule | which case applied: `switch_Y`, `only_code`, `ambiguous` (null + anomaly) or `none` |
| specialty_grouping, specialty_classification, specialty_specialization, specialty_display_name | NUCC description of the primary taxonomy (e.g. `Allopathic & Osteopathic Physicians` / `Psychiatry & Neurology` / `Psychiatry`) |

### taxonomy (table)
The whole NUCC code set used: `code, grouping, classification, specialization, display_name, section`.

### nppes_meta (table, 1 row)
Which files were used and what happened: `nppes_url, nppes_file, nppes_sha256, nppes_bytes, csv_member, n_csv_rows,
nucc_url, nucc_file, nucc_version, nucc_sha256, n_npis_requested, n_found, n_not_found, n_deactivated,
n_without_specialty, started_at, finished_at`.

### nppes_anomalies (table)
`step, npi, type, detail`. Types: `npi_not_in_nppes`, `ambiguous_primary_taxonomy`, `taxonomy_code_not_in_nucc`,
`wrong_field_count`.

### Views: rates_npi_named, rates_npi_dedup_named, capitation_npi_named
The base table (all its columns) `LEFT JOIN nppes` on `npi`, adding `entity_type_code, entity_type, provider_name,
credential, primary_taxonomy_code, specialty_*, practice_*, mailing_*, deactivation_date`, plus `in_nppes` (false
when the NPI wasn't found) and `tin_npi_provider_name` (the NPPES name of the TIN when `tin_type = 'npi'`). Row counts
are identical to the base tables.

## Common joins
```sql
-- one row per NPI: network X, code Y, NPI Z under TIN W gets this price under these conditions
SELECT r.billing_code, g.network_name, g.npi, g.tin_type, g.tin_value, g.tin_business_name,
       r.negotiated_type, r.negotiated_rate, r.billing_class, r.service_code, r.billing_code_modifier,
       f.is_in_state_file, r.file_id, r.i, r.j, r.k, r.r, g.m, g.n, g.p
FROM 'data/TX/2026-08-20/files/*/rates.parquet' r
JOIN 'data/TX/2026-08-20/files/*/provider_groups.parquet' g USING (file_id, provider_group_id)
JOIN 'data/TX/2026-08-20/index/index_files.parquet' f USING (file_id);
```
(`build-db` materialises this as `rates_npi` in `rates.duckdb`, plus the deduplicated `rates_npi_dedup`.)

## Tracing a number
A row in `rates` joined to `provider_groups` gives two paths into the source file identified by
`file_meta.url` + `file_meta.sha256`:
- the price: `in_network[i].negotiated_rates[j].negotiated_prices[k]` (group reference: `…negotiated_rates[j].provider_references[r]`)
- the NPI: `provider_references[m].provider_groups[n].npi[p]`

Re-download the file, check its SHA-256 equals `file_meta.sha256`, and read those paths. (A `trace` command will
automate this. BCBSTX replaces files monthly, so this only works while the month's file is online.)
