"""build-site: rates.duckdb (+ entity tags + map data) -> site.duckdb, the website's only data file.

Tables and columns follow backend/schema.py. Filter columns (provider type, location, tag, scope flag) are copied onto
every rate row so the website's filters never need a join. Built as site.duckdb.tmp and renamed on success.
"""
import datetime, os, time
import duckdb
from ..backend import schema as S
from ..backend.filters import Filters
from ..extract import dir_size, git_state
from ..io import run_dir
from .geo import ensure_geo_files, load_geo


class SiteBuildError(SystemExit):
    pass


def _q(v):
    return "'" + str(v).replace("'", "''") + "'"


def _in(values):
    return "(" + ", ".join(_q(v) for v in values) + ")"


PROVIDERS_SQL = f"""
CREATE TABLE {S.T_PROVIDERS} AS
WITH ids AS (
    SELECT DISTINCT npi FROM src.rates_npi_dedup WHERE is_valid_npi
    UNION SELECT DISTINCT npi FROM src.capitation_npi WHERE is_valid_npi
)
SELECT ids.npi,
       n.npi IS NOT NULL AS in_nppes,
       n.entity_type_code, n.entity_type, n.provider_name, n.credential,
       n.primary_taxonomy_code, n.specialty_grouping, n.specialty_classification, n.specialty_specialization,
       n.specialty_display_name,
       CASE WHEN n.npi IS NULL THEN 'Other'
            ELSE {S.PROVIDER_TYPE_SQL.format(e='n.entity_type_code', t='n.primary_taxonomy_code')} END
           AS provider_type_group,
       coalesce({S.PRESCRIBER_TYPE_SQL.format(t='n.primary_taxonomy_code')}, false) AS is_prescriber_type,
       n.practice_address_1, n.practice_address_2, n.practice_city, n.practice_state,
       left(n.practice_zip, 5) AS practice_zip5, n.practice_phone,
       left(n.mailing_zip, 5) AS mailing_zip5, n.deactivation_date,
       coalesce(gp.lat, gm.lat) AS lat, coalesce(gp.lon, gm.lon) AS lon,
       CASE WHEN gp.how = 'direct' THEN 'practice_zip' WHEN gp.how = 'crosswalk' THEN 'practice_zip_crosswalk'
            WHEN gm.how = 'direct' THEN 'mailing_zip' WHEN gm.how = 'crosswalk' THEN 'mailing_zip_crosswalk'
       END AS geo_source
FROM ids
LEFT JOIN src.nppes n USING (npi)
LEFT JOIN geo_point gp ON gp.zip5 = left(n.practice_zip, 5)
LEFT JOIN geo_point gm ON gm.zip5 = left(n.mailing_zip, 5)
ORDER BY ids.npi
"""

TINS_SQL = f"""
CREATE TABLE {S.T_TINS} AS
WITH biz AS (
    SELECT tin_value AS tin, any_value(tin_type) AS tin_type, tin_business_name AS name, count(*) AS n
    FROM src.provider_groups WHERE tin_business_name IS NOT NULL GROUP BY tin_value, tin_business_name
), orgs AS (
    SELECT g.tin_value AS tin, list(DISTINCT n.org_name ORDER BY n.org_name) AS org_names
    FROM src.provider_groups g JOIN src.nppes n USING (npi)
    WHERE n.entity_type_code = '2' AND n.org_name IS NOT NULL GROUP BY 1
), base AS (
    SELECT tin_value AS tin, any_value(tin_type) AS tin_type, count(DISTINCT npi) AS n_npis
    FROM src.provider_groups GROUP BY 1
), names AS (
    SELECT tin, first(name ORDER BY n DESC, name) AS top_name, list(name ORDER BY n DESC, name) AS name_variants
    FROM biz GROUP BY 1
)
SELECT b.tin, b.tin_type, b.n_npis,
       coalesce(CASE WHEN b.tin_type = 'npi' THEN np.provider_name END, nm.top_name, b.tin) AS display_name,
       CASE WHEN b.tin_type = 'npi' AND np.provider_name IS NOT NULL THEN 'nppes_npi_name'
            WHEN nm.top_name IS NOT NULL THEN 'bcbstx_most_common_name' ELSE 'tin' END AS name_source,
       coalesce(nm.name_variants, []) AS name_variants, coalesce(o.org_names, []) AS org_names,
       t.tag_type, t.tag_name, t.confidence AS tag_confidence, t.relationship AS tag_relationship,
       upper(concat_ws(' | ', b.tin, np.provider_name, array_to_string(nm.name_variants, ' | '),
                       array_to_string(o.org_names, ' | '), t.tag_name)) AS search_text
FROM base b
LEFT JOIN names nm USING (tin)
LEFT JOIN orgs o USING (tin)
LEFT JOIN src.nppes np ON b.tin_type = 'npi' AND np.npi = b.tin
LEFT JOIN {S.T_ENTITY_TAGS} t ON t.tin = b.tin
ORDER BY b.tin
"""

SCOPE_SQL = f"""CASE
    WHEN p.npi IS NULL OR NOT p.in_nppes THEN 'not_in_nppes'
    WHEN p.provider_type_group = 'Organization' THEN 'organization'
    WHEN d.billing_code IN {_in([c for c, g in S.CODE_GROUPS.items() if g == 'psychotherapy'])}
         AND p.provider_type_group IN {_in(S.BH_TYPES)} THEN 'expected'
    WHEN d.billing_code NOT IN {_in([c for c, g in S.CODE_GROUPS.items() if g == 'psychotherapy'])}
         AND p.provider_type_group IN {_in(S.PRESCRIBER_BH_TYPES)} THEN 'expected'
    WHEN p.is_prescriber_type THEN 'non_bh_clinician'
    ELSE 'ghost_candidate' END"""

# One billing code at a time: stage rows with their sources, then split into rates and rate_sources.
STAGE_SQL = f"""
CREATE OR REPLACE TEMP TABLE stage AS
SELECT $offset + row_number() OVER (ORDER BY d.tin_value, d.npi, d.negotiated_rate, d.negotiated_type,
                                             d.billing_class, d.service_code, d.expiration_date) AS rate_id,
       d.*, p.npi IS NOT NULL AS has_provider, p.provider_type_group, p.practice_state, p.practice_city,
       p.practice_zip5, p.in_nppes, t.tag_type, t.tag_name, t.tag_confidence, {SCOPE_SQL} AS scope_flag
FROM src.rates_npi_dedup d
LEFT JOIN {S.T_PROVIDERS} p ON p.npi = d.npi
LEFT JOIN {S.T_TINS} t ON t.tin = d.tin_value
WHERE d.billing_code = $code
"""

RATES_INSERT = f"""
INSERT INTO {S.T_RATES}
SELECT rate_id, $snapshot::DATE AS snapshot_date,
       billing_code, billing_code_type, negotiation_arrangement,
       negotiated_type, rate_unit, negotiated_rate, negotiated_rate_raw, is_zero_rate,
       billing_class, setting, ps.pos_set_id, {S.POS_GROUP_SQL.format(s='stage.service_code')} AS pos_group,
       billing_code_modifier, expiration_date,
       coalesce(try_cast(expiration_date AS DATE) < $snapshot::DATE, false) AS is_expired,
       npi, is_valid_npi, tin_type, tin_value AS tin, networks, coalesce(is_in_state_any, false) AS is_texas_file,
       n_sources,
       coalesce(provider_type_group, 'Other') AS provider_type_group, practice_state, practice_city, practice_zip5,
       scope_flag, tag_type, tag_name, tag_confidence
FROM stage LEFT JOIN pos_sets ps ON ps.service_code IS NOT DISTINCT FROM stage.service_code
ORDER BY rate_id
"""

SOURCES_INSERT = f"""
INSERT INTO {S.T_RATE_SOURCES}
SELECT rate_id, f.file_idx, s.i::INTEGER AS i, s.j::INTEGER AS j, s.k::INTEGER AS k, s.r::INTEGER AS r,
       s.m::INTEGER AS m, s.n::INTEGER AS n, s.p::INTEGER AS p
FROM (SELECT rate_id, unnest(sources) AS s FROM stage) x JOIN files f ON f.file_id = x.s.file_id
ORDER BY rate_id
"""


def build(cfg, index_date, force=False, geo_paths=None, log=print):
    """Build <run_dir>/site.duckdb. Returns (path, counts)."""
    base = run_dir(cfg, index_date)
    src = os.path.join(base, "rates.duckdb")
    if not os.path.exists(src):
        raise SiteBuildError(f"{src} not found - run build-db and nppes first")
    final, tmp = os.path.join(base, "site.duckdb"), os.path.join(base, "site.duckdb.tmp")
    for p in (tmp, tmp + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    zcta, xwalk = geo_paths or ensure_geo_files(cfg, log)
    t0 = time.time()
    stage = lambda name: log(f"  {name:28s} done at {time.time() - t0:6,.0f}s")
    con = duckdb.connect(tmp)
    try:
        con.execute("SET enable_progress_bar = false")
        con.execute(f"SET memory_limit = '{cfg.max_memory_gb / 2:g}GB'")
        con.execute("SET threads = 4")
        con.execute("SET preserve_insertion_order = true")
        con.execute(f"SET temp_directory = '{os.path.join(base, 'duckdb_spill')}'")
        con.execute(f"ATTACH '{src}' AS src (READ_ONLY)")
        tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'src'").fetchall()}
        if "nppes" not in tables:
            raise SiteBuildError("rates.duckdb has no nppes table - run `rate-visualizer nppes` first")

        load_geo(con, zcta, xwalk)
        con.execute(f"""CREATE TABLE {S.T_ENTITY_TAGS} AS
            SELECT * FROM read_csv('{cfg.entity_tags_path}', header = true, all_varchar = true)""")
        dup = con.execute(f"SELECT tin FROM {S.T_ENTITY_TAGS} GROUP BY 1 HAVING count(*) > 1").fetchall()
        if dup:
            raise SiteBuildError(f"entity tags list a TIN more than once: {[d[0] for d in dup][:5]}")
        stage("entity tags + geo")
        con.execute(PROVIDERS_SQL)
        stage("providers")
        con.execute(TINS_SQL)
        stage("tins")

        _files(con)  # before rates: rate_sources refer to files by file_idx
        con.execute(f"""CREATE TABLE {S.T_POS_SETS} AS
            SELECT row_number() OVER (ORDER BY service_code)::USMALLINT AS pos_set_id, service_code
            FROM (SELECT DISTINCT service_code FROM src.rates_npi_dedup WHERE service_code IS NOT NULL)""")
        con.execute(STAGE_SQL.replace("WHERE d.billing_code = $code", "WHERE false"), {"offset": 0})
        con.execute(RATES_INSERT.replace(f"INSERT INTO {S.T_RATES}", f"CREATE TABLE {S.T_RATES} AS")
                    .replace("\nORDER BY rate_id", "\nWHERE false"), {"snapshot": index_date})
        con.execute(SOURCES_INSERT.replace(f"INSERT INTO {S.T_RATE_SOURCES}", f"CREATE TABLE {S.T_RATE_SOURCES} AS")
                    .replace("FROM stage) x", "FROM stage WHERE false) x"))
        codes = [r[0] for r in con.execute("SELECT DISTINCT billing_code FROM src.rates_npi_dedup ORDER BY 1").fetchall()]
        offset = 0
        for code in codes:
            con.execute(STAGE_SQL, {"offset": offset, "code": code})
            con.execute(RATES_INSERT, {"snapshot": index_date})
            con.execute(SOURCES_INSERT)
            offset = con.execute(f"SELECT coalesce(max(rate_id), 0) FROM {S.T_RATES}").fetchone()[0]
            stage(f"rates {code}")
        con.execute("DROP TABLE stage")
        # rows allowed by the default toggles, from the same Filters code the backend uses (one source of truth)
        core_where, core_params = Filters(dollar_rates_only=False).where("r")
        con.execute(f"CREATE TABLE {S.T_RATES_CORE} AS SELECT * FROM {S.T_RATES} r WHERE {core_where} ORDER BY rate_id",
                    core_params)
        stage("rates_core")

        con.execute(f"""CREATE TABLE {S.T_CAPITATION} AS
            SELECT c.file_id, c.billing_code, c.billing_code_type, c.name, c.covered_target_codes, c.n_covered_services,
                   c.npi, c.tin_type, c.tin_value AS tin, c.tin_business_name, c.network_name,
                   c.negotiated_type, c.negotiated_rate, c.negotiated_rate_raw, c.rate_unit, c.billing_class,
                   c.service_code, c.expiration_date, c.is_in_state_file AS is_texas_file,
                   p.provider_name, p.practice_city, t.tag_type, t.tag_name, c.i, c.j, c.k, c.m, c.n, c.p
            FROM src.capitation_npi c LEFT JOIN {S.T_PROVIDERS} p USING (npi) LEFT JOIN {S.T_TINS} t ON t.tin = c.tin_value""")
        stage("files + capitation")
        _data_quality(con, codes)
        stage("data quality")
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
                  (S.T_RATES, S.T_RATES_CORE, S.T_RATE_SOURCES, S.T_PROVIDERS, S.T_TINS, S.T_ENTITY_TAGS, S.T_FILES, S.T_CAPITATION)}
        per_code = dict(con.execute(f"SELECT billing_code, count(*) FROM {S.T_RATES} GROUP BY 1").fetchall())
        commit, dirty = git_state()
        con.execute(f"""CREATE TABLE {S.T_SITE_META} AS SELECT ? AS state, ?::DATE AS snapshot_date, ? AS schema_version,
                        ? AS built_at, ? AS parser_git_commit, ? AS parser_git_dirty, ? AS row_counts, ? AS rows_per_code""",
                    [cfg.state, index_date, S.SCHEMA_VERSION,
                     datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), commit, dirty,
                     str(counts), str(per_code)])
        con.execute("DETACH src")
        con.execute("CHECKPOINT")
        stage("checkpoint")
        _check(con, cfg, index_date, per_code, force)
    except BaseException:
        con.close()
        for p in (tmp, tmp + ".wal"):
            if os.path.exists(p):
                os.remove(p)
        raise
    con.close()
    if dir_size(cfg.data_dir) > cfg.max_storage_gb * 2**30:
        os.remove(tmp)
        raise SiteBuildError(f"storage limit: {cfg.data_dir} would exceed {cfg.max_storage_gb} GB")
    os.replace(tmp, final)
    counts["site_mb"] = round(os.path.getsize(final) / 2**20, 1)
    return final, counts


def _files(con):
    con.execute(f"""CREATE TABLE {S.T_FILES} AS
        WITH nets AS (SELECT file_id, list(DISTINCT n ORDER BY n) AS network_names
                      FROM (SELECT file_id, unnest(network_name) AS n FROM src.provider_groups) GROUP BY 1)
        SELECT row_number() OVER (ORDER BY f.file_name)::USMALLINT AS file_idx, f.file_id, f.file_name, f.url,
               f.descriptions, f.is_in_state_file AS is_texas_file, m.version AS schema_version, m.last_updated_on,
               m.sha256, m.compressed_bytes, coalesce(nets.network_names, []) AS network_names
        FROM src.index_files f JOIN src.file_meta m USING (file_id) LEFT JOIN nets USING (file_id)
        ORDER BY f.file_name""")


def _data_quality(con, codes):
    con.execute(f"""CREATE TABLE {S.T_DQ_FILE_ROWS} AS
        SELECT r.file_id, f.file_name, m.version AS schema_version, count(*) AS rows_before_dedup
        FROM src.rates_npi r JOIN src.index_files f USING (file_id) JOIN src.file_meta m USING (file_id)
        GROUP BY ALL ORDER BY rows_before_dedup DESC""")
    # a conflict: the same NPI x TIN x code x class x place of service x modifier x type x network with >1 price
    con.execute(f"""CREATE TABLE {S.T_DQ_CONFLICTS} (npi VARCHAR, tin VARCHAR, billing_code VARCHAR,
        billing_class VARCHAR, pos_set_id USMALLINT, billing_code_modifier VARCHAR[], negotiated_type VARCHAR,
        network VARCHAR, n_prices BIGINT, prices DOUBLE[], rate_ids BIGINT[])""")
    for code in codes:
        con.execute(f"""INSERT INTO {S.T_DQ_CONFLICTS}
            SELECT npi, tin, billing_code, billing_class, pos_set_id, billing_code_modifier, negotiated_type, network,
                   count(DISTINCT negotiated_rate_raw), list(DISTINCT negotiated_rate ORDER BY negotiated_rate),
                   list(rate_id ORDER BY rate_id)
            FROM (SELECT *, unnest(networks) AS network FROM {S.T_RATES} WHERE billing_code = ?)
            GROUP BY npi, tin, billing_code, billing_class, pos_set_id, billing_code_modifier, negotiated_type, network
            HAVING count(DISTINCT negotiated_rate_raw) > 1""", [code])
    stats = [
        ("rows_before_dedup", "SELECT count(*) FROM src.rates_npi", "one row per NPI per source rate, all files"),
        ("rows_after_dedup", f"SELECT count(*) FROM {S.T_RATES}", "identical rows merged across files/networks"),
        ("rows_removed_by_dedup", f"SELECT (SELECT count(*) FROM src.rates_npi) - (SELECT count(*) FROM {S.T_RATES})", ""),
        ("zero_rate_rows", f"SELECT count(*) FROM {S.T_RATES} WHERE is_zero_rate", "rate exactly 0"),
        ("invalid_npi_rows", f"SELECT count(*) FROM {S.T_RATES} WHERE NOT is_valid_npi", "NPI not 10 digits (e.g. 0)"),
        ("npis_not_in_nppes", f"SELECT count(*) FROM {S.T_PROVIDERS} WHERE NOT in_nppes", ""),
        ("rows_not_in_nppes", f"SELECT count(*) FROM {S.T_RATES} WHERE scope_flag = 'not_in_nppes' AND is_valid_npi", ""),
        ("conflicting_keys", f"SELECT count(*) FROM {S.T_DQ_CONFLICTS}", "same key and network, more than one price"),
        ("expired_rows", f"SELECT count(*) FROM {S.T_RATES} WHERE is_expired", "expiration date before the snapshot"),
    ] + [(f"scope_{f}_rows", f"SELECT count(*) FROM {S.T_RATES} WHERE scope_flag = '{f}'", S.SCOPE_LABELS[f])
         for f in S.SCOPE_FLAGS]
    con.execute(f"""CREATE TABLE {S.T_DQ_SCOPE} AS
        SELECT billing_code, scope_flag, count(DISTINCT npi) AS n_npis, count(*) AS n_rows FROM {S.T_RATES}
        WHERE billing_class = 'professional' AND rate_unit = 'dollars' AND is_valid_npi GROUP BY 1, 2 ORDER BY 1, 2""")
    con.execute(f"""CREATE TABLE {S.T_DQ_TAXONOMIES} AS
        SELECT scope_flag, specialty, n_npis FROM (
            SELECT r.scope_flag, coalesce(p.specialty_display_name, p.specialty_classification) AS specialty,
                   count(DISTINCT r.npi) AS n_npis,
                   row_number() OVER (PARTITION BY r.scope_flag ORDER BY count(DISTINCT r.npi) DESC) AS rk
            FROM {S.T_RATES} r JOIN {S.T_PROVIDERS} p ON p.npi = r.npi
            WHERE r.scope_flag IN ('non_bh_clinician', 'ghost_candidate') AND r.billing_class = 'professional'
            GROUP BY 1, 2) WHERE rk <= 10 ORDER BY scope_flag, n_npis DESC""")
    con.execute(f"CREATE TABLE {S.T_DQ_STATS} (metric VARCHAR, value BIGINT, note VARCHAR)")
    for metric, sql, note in stats:
        con.execute(f"INSERT INTO {S.T_DQ_STATS} SELECT ?, ({sql}), ?", [metric, note])
    con.execute(f"""INSERT INTO {S.T_DQ_STATS}
        SELECT 'anomalies_' || step || '_' || type, count(*), any_value(detail) FROM src.anomalies GROUP BY step, type""")
    tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'src'").fetchall()}
    if "nppes_anomalies" in tables:
        con.execute(f"""INSERT INTO {S.T_DQ_STATS}
            SELECT 'anomalies_nppes_' || type, count(*), any_value(detail) FROM src.nppes_anomalies GROUP BY type""")


def _check(con, cfg, index_date, per_code, force):
    """Stop if a code's row count moved more than site_max_change vs the previous month's site.duckdb."""
    root = os.path.join(cfg.data_dir, cfg.state)
    prev = sorted(d for d in os.listdir(root) if d < index_date and os.path.exists(os.path.join(root, d, "site.duckdb")))
    missing = [c for c in S.CODES if c in cfg.codes and c not in per_code]
    if missing and not force:
        raise SiteBuildError(f"no rates for code(s) {missing}; use --force to build anyway")
    if not prev:
        return
    with duckdb.connect(os.path.join(root, prev[-1], "site.duckdb"), read_only=True) as old:
        before = dict(old.execute(f"SELECT billing_code, count(*) FROM {S.T_RATES} GROUP BY 1").fetchall())
    moved = {c: (before.get(c, 0), n) for c, n in per_code.items()
             if before.get(c) and abs(n - before[c]) / before[c] > cfg.site_max_change}
    if moved and not force:
        raise SiteBuildError(f"row counts changed more than {cfg.site_max_change:.0%} vs {prev[-1]}: {moved}; "
                             "check the data or use --force")
