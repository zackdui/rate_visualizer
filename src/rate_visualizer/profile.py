"""Step 3b: profile the extracted Parquet (what rate types, arrangements, settings, anomalies actually appear).

Reads files/<file_id>/ folders that have _SUCCESS. Prints a report and saves it to <run_dir>/profile.txt.
"""
import os
import duckdb
import pyarrow.parquet as pq
from .io import run_dir

EXTRACT_TABLES = ("rates", "capitation", "capitation_covered_services", "provider_groups", "file_meta", "anomalies")


def done_dirs(files_root):
    """Folders of finished files (have _SUCCESS), sorted by file_id."""
    if not os.path.isdir(files_root):
        return []
    return sorted(os.path.join(files_root, d) for d in os.listdir(files_root)
                  if not d.endswith(".tmp") and os.path.exists(os.path.join(files_root, d, "_SUCCESS")))


def table_paths(dirs, name):
    paths = [os.path.join(d, f"{name}.parquet") for d in dirs]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise SystemExit(f"{len(missing)} finished folder(s) lack {name}.parquet (made by an older version); "
                         f"re-extract them with --force, e.g. {missing[0]}")
    return paths


def register(con, dirs):
    for name in EXTRACT_TABLES:
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet({table_paths(dirs, name)!r}, union_by_name=true)")


# Price level = one negotiated_prices entry (file_id, i, j, k), before multiplying by provider-group references.
PRICES = "(SELECT DISTINCT file_id, i, j, k, billing_code, negotiated_type, rate_unit, billing_class, setting, " \
         "negotiation_arrangement, service_code, billing_code_modifier, is_zero_rate FROM rates)"

SECTIONS = [
    ("Prices by negotiated_type x rate_unit x billing_class",
     f"SELECT negotiated_type, rate_unit, billing_class, count(*) AS prices FROM {PRICES} GROUP BY ALL ORDER BY prices DESC"),
    ("Prices by billing_code x rate_unit x billing_class",
     f"SELECT billing_code, rate_unit, billing_class, count(*) AS prices FROM {PRICES} GROUP BY ALL ORDER BY ALL"),
    ("Prices by negotiation_arrangement",
     f"SELECT negotiation_arrangement, count(*) AS prices FROM {PRICES} GROUP BY ALL ORDER BY prices DESC"),
    ("Prices by setting", f"SELECT setting, count(*) AS prices FROM {PRICES} GROUP BY ALL ORDER BY prices DESC"),
    ("Top place-of-service sets (service_code, sorted)",
     f"SELECT list_sort(service_code) AS service_code, count(*) AS prices FROM {PRICES} GROUP BY ALL "
     f"ORDER BY prices DESC LIMIT 15"),
    ("Top modifier sets (billing_code_modifier, sorted)",
     f"SELECT list_sort(billing_code_modifier) AS modifiers, count(*) AS prices FROM {PRICES} GROUP BY ALL "
     f"ORDER BY prices DESC LIMIT 15"),
    ("Zero-rate prices", f"SELECT rate_unit, billing_class, count(*) AS zero_prices FROM {PRICES} "
                         f"WHERE is_zero_rate GROUP BY ALL ORDER BY zero_prices DESC"),
    ("Anomalies by type", "SELECT step, type, count(*) AS n, any_value(detail) AS example FROM anomalies "
                          "GROUP BY ALL ORDER BY n DESC"),
    ("NPI values that are not 10 digits, in provider groups the rates/capitation use",
     "SELECT npi, count(*) AS rows FROM provider_groups WHERE NOT regexp_full_match(coalesce(npi, ''), '[0-9]{10}') "
     "GROUP BY ALL ORDER BY rows DESC"),
    ("Capitation", "SELECT covered_target_codes, rate_unit, count(*) AS rows, min(negotiated_rate) AS min_rate, "
                   "max(negotiated_rate) AS max_rate FROM capitation GROUP BY ALL ORDER BY rows DESC"),
    ("Per-file counts", "SELECT file_id, version, last_updated_on, n_items_matched, n_price_rows, n_rate_rows, "
                        "n_capitation_rows, n_anomalies, round(peak_rss_mb) AS peak_rss_mb FROM file_meta ORDER BY file_id"),
]


def format_table(columns, rows):
    """Plain fixed-width table with every row (DuckDB's own printer truncates long results)."""
    cells = [[str(c) for c in columns]] + [["" if v is None else str(v) for v in r] for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(columns))]
    fmt = lambda row: "  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip()
    return "\n".join([fmt(cells[0]), fmt(["-" * w for w in widths])] + [fmt(r) for r in cells[1:]])


def profile(files_root, expected_file_ids=None):
    """Returns (report text, {section title: rows}). expected_file_ids lists files that should be finished."""
    dirs = done_dirs(files_root)
    if not dirs:
        raise SystemExit(f"no finished files under {files_root}/ - run `rate-visualizer extract` first")
    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    register(con, dirs)
    done = {os.path.basename(d) for d in dirs}
    lines = [f"Profile of {len(dirs)} finished file(s) in {files_root}/"]
    if expected_file_ids is not None:
        missing = sorted(set(expected_file_ids) - done)
        lines.append(f"In-scope files not finished: {len(missing)}" + (f"  {missing}" if missing else ""))
    results = {}
    for title, sql in SECTIONS:
        rel = con.sql(sql)
        results[title] = rel.fetchall()
        lines += ["", f"== {title} ==", format_table(rel.columns, results[title]) if results[title] else "(none)"]
    con.close()
    return "\n".join(lines), results


def run(cfg, index_date):
    base = run_dir(cfg, index_date)
    files = pq.read_table(os.path.join(base, "index", "index_files.parquet")).to_pylist()
    expected = [f["file_id"] for f in files if f["file_kind"] == "in_network"
                and (cfg.scope == "all" or f["is_in_state_file"])]
    text, results = profile(os.path.join(base, "files"), expected)
    out = os.path.join(base, "profile.txt")
    with open(out, "w") as fh:
        fh.write(text + "\n")
    return text, out
