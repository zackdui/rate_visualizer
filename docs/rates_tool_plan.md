# BCBSTX Behavioral Health Rates Tool: Product Plan

## 1. Goal
A single-page website to search, filter, and benchmark BCBSTX negotiated rates for outpatient mental health codes. Every number on the page can be traced back to the rows and source file it came from. It should have a very clean and modern layout. Please use any python library that would be helpful in making a well designed modern website. Streamlit is a good option or niceGUI or any other library that you think is good. You are not limited to these. 

**Important** Everything should work in real time there should not be long delays when filtering or searching or loading.

## 2. Scope (v1)
- **Payer:** BCBSTX (HCSC). Parser and site stay modular so other HCSC states and insurers can be added later.
- **Codes:** 99205, 99214, 99215, 90832, 90834, 90837, 90833, 90836, 90838 (CPT only).
- **Billing class:** professional (institutional kept in data, hidden by default).
- **Snapshot:** latest monthly index. Every row carries its snapshot date.

## 3. Tech stack
| Layer | Choice |
|---|---|
| Data store | DuckDB: `rates.duckdb` built by the pipeline, slimmed into `site.duckdb` for the site |
| Web app | NiceGUI (Python): sticky header, AG Grid tables (sort, filter, group), ECharts charts, Leaflet map |
| Hosting | Render web service (Standard), site data in Cloudflare R2. Details: `rates_tool_hosting_plan.md` |

## 4. Data model (what the site reads)
| Table | Grain | Key columns |
|---|---|---|
| `rates` | one row per NPI × TIN × code × network × place of service × modifier × price | billing_code, billing_code_type, npi, tin_type, tin, network, service_code, modifier, billing_class, negotiated_type, negotiated_rate, arrangement, expiration_date, source_file_id, source_position, snapshot_date, is_texas_file |
| `providers` | NPI (from NPPES) | npi, entity_type (1/2), name, taxonomy_code, provider_type_group, practice_city, practice_state, practice_zip, lat, lon, geo_source (see 8.5) |
| `tins` | TIN | tin_type, tin, business_name, name_source |
| `entity_tags` | TIN | tin, tag_type (platform / health_system), tag_name, match_method, source |
| `files` | source file | source_file_id, url, description, network, schema_version, last_updated_on, is_texas_file |
| `dq_stats` | pipeline run | counts for the Data Quality section |

`provider_type_group` values: Psychiatrist, Psych NP, Psychologist, LCSW, LPC, LMFT, Other.

**Verified on our data**: all seven groups exist. Mapping from the NPI's primary NUCC taxonomy code (Type 2
organisation NPIs get `Organization`):

| Group | NUCC taxonomy codes | Texas NPIs in our data |
|---|---|---|
| Psychiatrist | 2084P0800X Psychiatry; 2084P0804X Child & Adolescent; 2084P0805X Geriatric; 2084P0802X Addiction; 2084F0202X Forensic; 2084P0015X Psychosomatic Medicine; 2084B0040X Behavioral Neurology & Neuropsychiatry | 2,541 |
| Psych NP | 363LP0808X Psychiatric/Mental Health NP; 364SP08xx Psychiatric/Mental Health Clinical Nurse Specialists | 3,302 |
| Psychologist | 103T* Psychologist (all specialisations); 103G00000X Clinical Neuropsychologist | 3,232 |
| LCSW | 1041C0700X Social Worker, Clinical; 104100000X Social Worker | 7,306 |
| LPC | 101YP2500X Professional; 101YM0800X Mental Health; 101Y00000X Counselor (not school, pastoral or addiction) | 18,070 |
| LMFT | 106H00000X Marriage & Family Therapist | 1,248 |
| Other | everything else (incl. addiction counselors, behavior analysts, and all non-mental-health clinicians) | 141,598 |
| Organization | Type 2 NPIs | 36,945 |

## 5. Page layout
- One page with a **sticky top nav bar** that jumps to each section.
- A **global filter bar** under the nav, also sticky or collapsible.
- Sections in order:
  1. Code Summary
  2. Benchmarks
  3. Rate Explorer
  4. Percentage Rates
  5. Map
  6. Rate Type Composition
  7. Provider Profile
  8. Billing Entity Profile
  9. Data Quality
- Clean, modern style: light/dark theme, generous spacing, one accent color.

## 6. Global filters (apply to every section)
Every filter is a **type-to-search multi-select** whose options come from the data (type "9083" and see 90832/90833/…).

| Filter | Source |
|---|---|
| Code | `rates.billing_code` |
| Provider type | `providers.provider_type_group` |
| Network | `rates.network` |
| Location (city / ZIP / state) | `providers.practice_*` |
| Place of service | `rates.service_code` (office 11, telehealth 02/10, …) |
| Billing entity / NPI / name | contains-match text search |
| Entity tag | platform, health system, none |

| Toggle | Default |
|---|---|
| Exclude $0 rates | **ON** |
| Exclude NPIs equal to 0 | **ON** |
| Texas files only | ON |
| Dollar rates only (negotiated, fee schedule) | ON |
| Exclude expired rates | ON |
| Exclude platforms from market stats | OFF |
| Behavioral health providers only (`scope_flag = expected`, see 8.9) | **ON** (recommended: ghost rates are very common, see 8.9) |

**Counting unit:** per **TIN** by default, switchable to per NPI or per row. Shown on every section.

## 7. Aggregation control
A **"Summarize by" control** on any filtered table collapses rows to one per **TIN × code** or **NPI × code**, showing the average rate and row count. Clicking any aggregated value opens the underlying rows in the Rate Explorer. We should also always be able to aggregate purely by code and see the average per code.

## 8. Sections

### 8.1 Code Summary
- One row per code, optionally broken down by provider type, network, or place of service.
- Columns: # TINs, # NPIs, min, P10, P25, P50, P75, P90, max, mean, % on percentage rates.
- A histogram per code, with benchmark markers pinned on it.

### 8.2 Benchmarks
- For each code: each tagged entity's rate(s) next to the market distribution for the same filters.
- Percentile rank shown as both "% below" and "% at or below", with the sample size (n TINs).
- One line per distinct rate when an entity has several (by network, place of service, or provider type).
- Markers stay pinned on the Code Summary histograms and the Map under any filters.

### 8.3 Rate Explorer
- Row-level table: code, rate, negotiated type, network, place of service, modifier, billing class, expiration, NPI, provider name, provider type, TIN, business name, tag, location.
- Sort, filter, and group by any column. Export to CSV.
- Each row has a **source link**: file URL, snapshot date, position in file.
- With "Dollar rates only" ON (the default), this is the dollar-only table (spec item 7).

### 8.4 Percentage Rates
- The same columns as the Rate Explorer, percentage rows only.
- Plus an empty `billed_charge` column and a computed `estimated_dollars` column = rate% × billed_charge.

### 8.5 Map
- Providers at their NPPES practice ZIP centroid (Census ZCTA).
- Color = percentile rank for the selected code.
- Distinct marker shapes for platforms and health systems.
- Clustered at low zoom. Responds to all global filters.

**Map data (researched; we have everything needed):**
| Need | Source | Status |
|---|---|---|
| Provider ZIP | NPPES practice location ZIP (fallback: mailing ZIP) | Already in `rates.duckdb` (`nppes`) |
| ZIP → map point | **US Census 2025 Gazetteer ZCTA file**: one internal point (`INTPTLAT`, `INTPTLONG`) per ZIP code tabulation area, 33,791 areas. `https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_zcta_national.zip` | Downloaded to `data/geo/` |
| ZIPs with no Census area (PO boxes, "unique" ZIPs of large organisations, e.g. 76508 Baylor Scott & White Temple, 79430 Texas Tech HSC Lubbock) | **HRSA ZIP Code to ZCTA Crosswalk** (successor to the retired UDS Mapper crosswalk; maps each ZIP to its ZCTA). `https://data.hrsa.gov/DataDownload/GeoCareNavigator/ZIP%20Code%20to%20ZCTA%20Crosswalk.xlsx` | Downloaded to `data/geo/` |
| Background map tiles | OpenStreetMap tiles (NiceGUI `ui.leaflet` default) with attribution; fine for a small site. Switch to a tile provider's free tier if traffic grows | Nothing to download |

Coverage on our data: **214,234 of 214,243 Texas NPIs (99.996%)** get a map point, 99.88% of all NPIs, and 99.85%
of deduplicated rate rows. Order of lookup: practice ZIP → practice ZIP via crosswalk → mailing ZIP → mailing ZIP via
crosswalk; each provider records which (`geo_source`). The rest are military APO addresses or have no ZIP; they're
listed, not mapped. The crosswalk has a few duplicate ZIPs and must be reduced to one row per ZIP.

Points are ZIP centroids, so all providers in a ZIP share one point. The map shows **one marker per ZIP** (count,
median rate, percentile colour) with its providers listed on click, rather than stacking identical points.

### 8.6 Rate Type Composition
- Counts of negotiated / fee schedule / percentage / derived / per diem / other, by code and network.
- Shown as # TINs, # NPIs, and # rows.

### 8.7 Provider Profile (NPI)
- Search an NPI or name to open the profile.
- Shows every TIN they bill under, networks, rate and percentile rank per code, and a **rate index** (the average of rate ÷ market median across codes).
- Unique-NPI list view with the rate index, sortable.

### 8.8 Billing Entity Profile (TIN)
- Shows business name, tag, # NPIs, provider-type mix, rates per code, and **uniform vs. split** (whether all NPIs get the same rate per code).
- Unique-TIN list view, sortable.

### 8.9 Data Quality
- Rows removed by deduplication.
- $0 rates.
- Keys with conflicting prices.
- NPIs not in NPPES.
- Ghost-rate candidates (code billed by a provider with a non-mental-health taxonomy).

**Ghost-rate rule (decided).** Each rate row gets a `scope_flag` from the code and the NPI's provider type (section 4),
based on who may bill each code:
- Psychotherapy codes (90832/90834/90837) can be billed by all licensed mental-health providers, including LCSWs,
  LPCs and LMFTs (Medicare has covered MFTs and mental health counselors since 2024).
- The add-on codes (90833/90836/90838) are only billed together with an E/M visit, so they need a prescriber who can
  bill E/M. Psychologists, LCSWs, LPCs and LMFTs can't bill E/M, so they can't bill these add-ons either.

| Code group | `expected` | `non_bh_clinician` | `ghost_candidate` |
|---|---|---|---|
| Psychotherapy 90832, 90834, 90837 | Psychiatrist, Psych NP, Psychologist, LCSW, LPC, LMFT | Other physicians (taxonomy 20*), NPs (363L*), PAs (363A*), CNSs (364S*): legally allowed, not mental-health specialists | Everyone else (e.g. physical therapists, speech pathologists, nurse anesthetists, chiropractors) |
| Add-on 90833, 90836, 90838 | Psychiatrist, Psych NP | Other physicians, NPs, PAs, CNSs | Psychologist, LCSW, LPC, LMFT (can't bill E/M), and everyone else |
| E/M 99205, 99214, 99215 | Psychiatrist, Psych NP | Other physicians, NPs, PAs, CNSs (real E/M billers, but not behavioral health) | Psychologist, LCSW, LPC, LMFT, and everyone else |

Also: `organization` for Type 2 NPIs, and `not_in_nppes`. On our data (professional dollar rows, deduplicated),
ghost rates are very common. BCBSTX contracts attach rates for these codes to every NPI in a group: for
psychotherapy, 49,169 NPIs are `expected`, but 118,502 are `non_bh_clinician` (including radiologists and
anesthesiologists) and 49,040 are `ghost_candidate` (physical therapists alone: 9,149). So market statistics default to
`expected` rows only (toggle in section 6), and Data Quality shows the other flags with the top taxonomies behind them.

Sources: [CMS Billing and Coding: Psychiatry and Psychology Services (A57480)](https://www.cms.gov/medicare-coverage-database/view/article.aspx?articleId=57480);
[CPT 90833 billing eligibility](https://www.psychiatrybillers.com/resources/codes/cpt-code-90833).
- Row counts by schema version and source file.
- Each count opens its rows.

## 9. Traceability rule
Every aggregate (count, percentile, benchmark, rank) is a query over `rates` plus lookup tables. Clicking it opens the exact filtered rows. Every row links to `files` (URL, snapshot, schema version) and its position in the file.

## 10. Tagged entities (`entity_tags`)
We want to pin the negotiated rates of certain platforms and health systems on the Code Summary histograms and the Map, and show them in the Benchmarks section. The `entity_tags` table contains these tags.
- **Platforms:** Headway, Talkiatry, Alma, Grow Therapy, Rula, SonderMind, LifeStance Health, Thriveworks, Brightside, Cerebral.
- **Health systems:**
  - Baylor Scott & White (incl. HealthTexas Provider Network)
  - Texas Health Resources (incl. Texas Health Physicians Group)
  - Memorial Hermann
  - Houston Methodist
  - Methodist Health System (Dallas)
  - CHRISTUS Health
  - Ascension Seton
  - HCA Healthcare (Medical City, HCA Houston, St. David's, Methodist Healthcare San Antonio)
  - Tenet (incl. Baptist Health System)
  - Covenant Health
- **Matching:** loose name match on `business_name` and on NPPES Type 2 organization names, then manual TIN confirmation. `match_method` records which.

### 10.1 Platform research results (BCBSTX data, index 2026-08-20)
**Name matching alone doesn't work.** The TIN `business_name` in BCBSTX files is unreliable: the same TIN often
carries an individual clinician's name in some files (e.g. Talkiatry's TIN shows "MATTHEW KOLLER K"). And platforms
contract through legal entities with other names (Headway's Texas entity is "New York Medical Behavioral Health
Services, P.C."). The method that worked:
1. Find each platform's organisation NPIs in the **full NPPES file**, including its separate "other organization
   name" (DBA) file (`othername_pfile`), e.g. `MCCD FL PSYCHIATRY SERVICES PA` with DBA `Talkiatry`.
2. Find the TINs in our data whose provider groups contain those organisation NPIs.
3. Where the platform publishes it, use its own list (Headway publishes group name, NPI and TIN per state).

| Platform | TIN(s) in our data | NPIs under TIN | Evidence | Confidence |
|---|---|---|---|---|
| **Headway** | **83-2675429** | 15,838 | Headway's published Texas group: "New York Medical Behavioral Health Services, P.C.", NPI 1235600834, TIN 832675429 ([Headway help center](https://help.headway.co/hc/en-us/articles/21556524888596-Calling-your-insurance-company)) | Confirmed |
| **Talkiatry** | **88-2977235** | 176 | Group contains NPI 1659003457, `MCCD FL PSYCHIATRY SERVICES PA`, NPPES DBA "Talkiatry" | Confirmed |
| **Talkiatry** | **84-3213629** | 34 | Group contains NPI 1033750823, `MCCD PSYCHIATRY SERVICES PLLC`, NPPES DBA "Talkiatry" | Confirmed |
| Grow Therapy | 85-2938829 | 3,577 | NPI 1245845932 `GROW HEALTHCARE GROUP PA` (TIN business name matches too) | High |
| SonderMind | 47-5025949 | 2,622 | NPI 1760854442 `SONDERMIND PROVIDER NETWORK LLC` | High |
| SonderMind | 99-4151402 | 3 | NPI 1003646977 `SONDERMIND PC` | High (tiny) |
| Rula | 86-2493019 | 2,592 | TIN name `MENTAL HEALTH SPECIALTY GROUP PA`; group contains NPI 1801472634, NPPES DBA "Rula Health" | High |
| LifeStance Health | 26-4621796 | 929 | Group contains NPI 1659567238, NPPES DBA "LifeStance Health" | High |
| Thriveworks | 26-3447487 | 479 | Group contains NPI 1215181300, NPPES DBA "Thriveworks" | High |
| Thriveworks (location) | 47-1744442 | 27 | `ROBERSON COUNSELING CENTER`, NPPES DBA "Thriveworks North Central Austin" (franchise-style location) | Medium; decide whether to count it |
| Brightside | 83-2104126 | 488 | Group contains NPI 1801355482 `BRIGHTSIDE MEDICAL, P.C.` | High |
| Cerebral | 83-4662816 | 190 | Group contains NPI 1992355879 `CEREBRAL MEDICAL GROUP, A PROFESSIONAL CORPORATION` | High |
| Alma | – | – | 16 Alma-named NPPES entities, none appears as a TIN in our data (Alma clinicians likely bill under their own TINs) | Not found |

False positives from loose name matching (exclude): 27-0287093 "Southwestern Cerebral Circulatory Dynamics",
87-3879576 "Ready Set Grow Therapy", and many individuals named Alma.

Headway and Talkiatry both have dollar rates for **all 9 codes in 5 networks** (Blue Advantage HMO, Blue Choice PPO,
Blue Essentials, Blue Premier, MyBlue Health HMO), with several distinct rates per code by network and conditions
(e.g. Headway 90837: $76.71–$99.51; Talkiatry 90837: $100.92–$129.60 across its TINs). Enough for the Benchmarks
section.

**Done:** the seed file `configs/entity_tags_tx.csv` (250 TINs: 12 platform, 238 health system; columns tin, tag_type,
tag_name, relationship, confidence, match_method, evidence, n_npis_2026_08_20) covers the platforms above and all
ten health systems. How each was matched, the confidence rules, and every exclusion are in
**[entity_matching.md](entity_matching.md)**. The small Thriveworks TIN (47-1744442) is tagged Thriveworks with
`relationship = affiliated_location`. Re-check each month: platforms and systems add entities.

## 11. Open questions
1. **Hosting:** Vercel can't run Streamlit or NiceGUI, which need an always-on server. Options:
   - **(a)** Keep Python (NiceGUI) and host on Render, Railway, or Fly.io.
   - **(b)** Stay on Vercel with a static site (e.g. Next.js + DuckDB-WASM reading Parquet). No Python server, but the front end isn't Python.
   - **(c)** Streamlit on Streamlit Community Cloud (easiest, but a sticky nav needs custom CSS hacks).
   Which do you prefer?
2. **Access:** public site or password-protected? Right now public
3. **Snapshots:** latest month only, or a snapshot selector for monthly history? Right now just the latest month but include a snapshot selector in the design so it can be added later.
4. **Texas filter:** file origin only, or also a separate "Texas practice address" toggle? Just origin file
5. **E/M + add-on combos:** include virtual codes like 99214+90833 in Code Summary and Benchmarks? No, only the 9 codes listed in Scope (v1)
6. **Provider type groups:** are the seven groups in section 4 right, or do you want finer taxonomy detail? I don't want finer but if those groups don't exist don't use them. Check to make sure they exist. **Checked: all seven exist; mapping and counts in section 4.**
7. **Ghost-rate rule:** which taxonomies count as "mental health" for each code? **Decided: see 8.9.**

**Resolved:** Q1 hosting is NiceGUI on Render with data in Cloudflare R2 (see `rates_tool_hosting_plan.md`). The
site stays public with no custom domain for now; a password and a domain can be added later.

## 12. Decision log
| Date | Decision | Why | Where |
|---|---|---|---|
| 2026-09-29 | **"Behavioral health providers only" toggle, ON by default** for market statistics (rows with `scope_flag = expected`) | Ghost rates are very common: BCBSTX attaches psychotherapy rates to whole groups. For 90832/90834/90837 (professional dollar rates, deduplicated): 49,169 NPIs are expected mental-health providers, 118,502 are other clinicians (incl. radiologists, anesthesiologists) and 49,040 are clear ghosts (9,149 physical therapists alone). Without the toggle, market percentiles would mostly reflect non-mental-health providers | §6 (toggle), §8.9 (rule) |
| 2026-09-29 | Ghost-rate rule by code group (psychotherapy vs add-on vs E/M) | CMS billing rules: add-ons and E/M need a prescriber | §8.9 |
| 2026-09-29 | Thriveworks location TIN 47-1744442 tagged Thriveworks (`affiliated_location`) | Public DBA "Thriveworks North Central Austin"; own TIN and contract | `entity_matching.md` §4 |
| 2026-09-29 | Hosting: NiceGUI on Render Standard, data in Cloudflare R2; public, no custom domain yet | Simplest setup at ~$25/month | `rates_tool_hosting_plan.md` |
