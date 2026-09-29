"""Step 4: load the index + every finished file into one DuckDB database and build the query tables.

<run_dir>/rates.duckdb:
  index_meta, index_files, index_plans, plan_files        step 1, as-is
  file_meta, provider_groups, rates, capitation,
  capitation_covered_services                             steps 2-3, all finished files stacked
  anomalies                                               index + extract anomalies
  rates_npi          one row per NPI: network X, code Y, NPI Z under TIN W gets this price under these conditions
  rates_npi_dedup    rates_npi with identical rows merged (network not in the key, lists compared sorted);
                     every source kept in `sources`
  capitation_npi     capitation joined to its payee NPIs
  build_meta         when/how this database was built, row counts
"""
import datetime, os, shutil, time
import duckdb
from .extract import dir_size, git_state
from .io import run_dir
from .profile import EXTRACT_TABLES, done_dirs, table_paths

INDEX_TABLES = ("index_meta", "index_files", "index_plans", "plan_files")

# Columns that must all be identical for two rates_npi rows to be merged in rates_npi_dedup.
DEDUP_KEY = ["billing_code", "billing_code_type", "billing_code_type_version", "negotiation_arrangement", "name",
             "description", "bundled_codes", "covered_services", "tin_type", "tin_value", "tin_business_name", "npi",
             "is_valid_npi", "negotiated_type", "negotiated_rate_raw", "rate_unit", "is_zero_rate", "billing_class",
             "setting", "expiration_date", "additional_information"]
SORTED_LIST_KEY = ["service_code", "billing_code_modifier"]  # compared as sorted sets

VALID_NPI = "coalesce(regexp_full_match(g.npi, '[0-9]{10}'), false)"

RATES_NPI_SQL = f"""
CREATE TABLE rates_npi AS
SELECT '{{state}}' AS state, r.file_id, f.file_name, f.is_in_state_file, f.descriptions AS file_descriptions,
       g.network_name,
       r.billing_code, r.billing_code_type, r.billing_code_type_version, r.name, r.description,
       r.negotiation_arrangement, r.bundled_codes, r.covered_services,
       g.tin_type, g.tin_value, g.tin_business_name, g.npi, {VALID_NPI} AS is_valid_npi,
       r.negotiated_type, r.negotiated_rate, r.negotiated_rate_raw, r.rate_unit, r.is_zero_rate,
       r.billing_class, r.setting, r.service_code, r.billing_code_modifier, r.expiration_date,
       r.additional_information,
       r.provider_group_id, r.i, r.j, r.k, r.r, g.m, g.n, g.p,
       m.sha256 AS file_sha256, m.last_updated_on AS file_last_updated_on
FROM rates r
JOIN provider_groups g USING (file_id, provider_group_id)
JOIN file_meta m USING (file_id)
LEFT JOIN index_files f USING (file_id)
ORDER BY r.billing_code, r.rate_unit, r.billing_class, r.negotiated_rate, g.npi
"""

# Run once per (billing_code, NPI-hash bucket). Both billing_code and npi are part of the key, so rows in different
# pieces can never merge: the result is identical to one big GROUP BY, but each pass only holds about
# DEDUP_ROWS_PER_PASS rows in memory (list aggregates can't spill to disk).
DEDUP_ROWS_PER_PASS = 500_000
BUCKET = "hash(coalesce(npi, '')) % $buckets"
DEDUP_SQL = f"""
INSERT INTO rates_npi_dedup
SELECT {", ".join(DEDUP_KEY)},
       {", ".join(f"list_sort({c}) AS {c}" for c in SORTED_LIST_KEY)},
       any_value(negotiated_rate) AS negotiated_rate,
       list_sort(list_distinct(flatten(list(network_name) FILTER (WHERE network_name IS NOT NULL)))) AS networks,
       bool_or(is_in_state_file) AS is_in_state_any,
       bool_and(is_in_state_file) AS is_in_state_all,
       count(*) AS n_sources,
       list({{'file_id': file_id, 'network_name': network_name, 'provider_group_id': provider_group_id,
              'i': i, 'j': j, 'k': k, 'r': r, 'm': m, 'n': n, 'p': p,
              'service_code': service_code, 'billing_code_modifier': billing_code_modifier}}
            ORDER BY file_id, i, j, k, r, m, n, p) AS sources
FROM rates_npi
WHERE billing_code IS NOT DISTINCT FROM $code AND {BUCKET} = $bucket
GROUP BY {", ".join(DEDUP_KEY)}, {", ".join(f"list_sort({c})" for c in SORTED_LIST_KEY)}
ORDER BY billing_code, rate_unit, billing_class, negotiated_rate, npi
"""

CAPITATION_NPI_SQL = f"""
CREATE TABLE capitation_npi AS
SELECT '{{state}}' AS state, c.file_id, f.file_name, f.is_in_state_file, f.descriptions AS file_descriptions,
       g.network_name, c.billing_code, c.billing_code_type, c.negotiation_arrangement, c.name, c.description,
       c.covered_target_codes, c.n_covered_services,
       g.tin_type, g.tin_value, g.tin_business_name, g.npi, {VALID_NPI} AS is_valid_npi,
       c.negotiated_type, c.negotiated_rate, c.negotiated_rate_raw, c.rate_unit, c.billing_class, c.setting,
       c.service_code, c.billing_code_modifier, c.expiration_date, c.additional_information,
       c.provider_group_id, c.i, c.j, c.k, c.r, g.m, g.n, g.p,
       m.sha256 AS file_sha256, m.last_updated_on AS file_last_updated_on
FROM capitation c
JOIN provider_groups g USING (file_id, provider_group_id)
JOIN file_meta m USING (file_id)
LEFT JOIN index_files f USING (file_id)
ORDER BY c.file_id, c.i, c.j, c.k, c.r, g.m, g.n, g.p
"""


class StorageLimit(SystemExit):
    pass


def build(cfg, index_date, db_name="rates.duckdb", log=lambda msg: None):
    """Build <run_dir>/<db_name>. Returns (path, counts dict). The old database is replaced only on success."""
    base = run_dir(cfg, index_date)
    dirs = done_dirs(os.path.join(base, "files"))
    if not dirs:
        raise SystemExit(f"no finished files under {base}/files/ - run `rate-visualizer extract` first")
    final, tmp = os.path.join(base, db_name), os.path.join(base, db_name + ".tmp")
    spill = os.path.join(base, "duckdb_spill")
    limit = cfg.max_storage_gb * 2**30
    for p in (tmp, tmp + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    if dir_size(cfg.data_dir) > limit:
        raise StorageLimit(f"storage limit already exceeded: {cfg.data_dir} > {cfg.max_storage_gb} GB")
    os.makedirs(spill, exist_ok=True)
    started = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    con = duckdb.connect(tmp)
    try:
        con.execute("SET enable_progress_bar = false")
        # Half of max_memory_gb: DuckDB's limit covers its buffers, not the whole process, and giving it (nearly) all
        # of a 17 GB machine made macOS thrash (a 7-hour build). 4 threads keeps per-thread memory reasonable.
        con.execute(f"SET memory_limit = '{cfg.max_memory_gb / 2:g}GB'")
        con.execute("SET threads = 4")
        con.execute(f"SET temp_directory = '{spill}'")
        con.execute("SET preserve_insertion_order = false")
        t0 = time.time()

        def stage(name):
            log(f"  {name:32s} done at {time.time() - t0:7,.0f}s")

        for name in INDEX_TABLES:
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM read_parquet('{base}/index/{name}.parquet')")
        for name in EXTRACT_TABLES:
            if name == "anomalies":
                continue
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM "
                        f"read_parquet({table_paths(dirs, name)!r}, union_by_name=true)")
        con.execute(f"""CREATE TABLE anomalies AS
            SELECT * FROM read_parquet('{base}/index/anomalies.parquet')
            UNION ALL BY NAME
            SELECT * FROM read_parquet({table_paths(dirs, 'anomalies')!r}, union_by_name=true)""")
        stage("load tables")
        _check_storage(cfg, limit, "after loading")
        con.execute(RATES_NPI_SQL.replace("{state}", cfg.state))
        stage("rates_npi")
        _check_storage(cfg, limit, "after rates_npi")
        empty = DEDUP_SQL.replace("INSERT INTO rates_npi_dedup", "CREATE TABLE rates_npi_dedup AS") \
                         .replace(f"WHERE billing_code IS NOT DISTINCT FROM $code AND {BUCKET} = $bucket", "WHERE false")
        con.execute(empty)  # creates the table with the right columns and no rows
        code_rows = con.execute("SELECT billing_code, count(*) FROM rates_npi GROUP BY 1 ORDER BY 1").fetchall()
        for code, n in code_rows:
            buckets = max(1, -(-n // DEDUP_ROWS_PER_PASS))
            for b in range(buckets):
                con.execute(DEDUP_SQL, {"code": code, "buckets": buckets, "bucket": b})
            stage(f"rates_npi_dedup {code} ({buckets} passes)")
            _check_storage(cfg, limit, f"after rates_npi_dedup for {code}")
        con.execute(CAPITATION_NPI_SQL.replace("{state}", cfg.state))
        stage("capitation_npi")
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
                  ("rates", "provider_groups", "rates_npi", "rates_npi_dedup", "capitation", "capitation_npi",
                   "anomalies", "file_meta")}
        # rates rows whose provider group is missing would silently drop out of the join; make that visible
        counts["rates_without_group"] = con.execute(
            "SELECT count(*) FROM rates r ANTI JOIN provider_groups g USING (file_id, provider_group_id)").fetchone()[0]
        commit, dirty = git_state()
        con.execute("CREATE TABLE build_meta AS SELECT * FROM (VALUES (?, ?, ?, ?, ?, ?, ?)) "
                    "t(state, index_date, n_files, started_at, finished_at, parser_git_commit, parser_git_dirty)",
                    [cfg.state, index_date, len(dirs), started,
                     datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), commit, dirty])
        con.execute("CHECKPOINT")
        stage("checkpoint")
    except BaseException:
        con.close()
        for p in (tmp, tmp + ".wal"):
            if os.path.exists(p):
                os.remove(p)
        raise
    finally:
        shutil.rmtree(spill, ignore_errors=True)
    con.close()
    try:
        _check_storage(cfg, limit, "finished database")
    except StorageLimit:
        os.remove(tmp)
        raise
    os.replace(tmp, final)
    counts["db_mb"] = round(os.path.getsize(final) / 2**20, 1)
    return final, counts


def _check_storage(cfg, limit, where):
    used = dir_size(cfg.data_dir)
    if used > limit:
        raise StorageLimit(f"storage limit hit {where}: {cfg.data_dir} is {used / 2**30:.2f} GB > "
                           f"{cfg.max_storage_gb} GB; the partial database was deleted")
